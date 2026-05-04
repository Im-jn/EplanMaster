from __future__ import annotations

import argparse
import base64
import json
import math
import re
import zlib
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
INDIRECT_OBJECT_START_RE = re.compile(rb"(?m)(?<!\d)(\d+)\s+(\d+)\s+obj\b")
ENDOBJ_RE = re.compile(rb"\bendobj\b")
STARTXREF_RE = re.compile(rb"\bstartxref\b")
CLASSIC_XREF_RE = re.compile(rb"(?m)(^|[\r\n])xref[\r\n\s]")
STREAM_RE = re.compile(rb"\bstream\r?\n")
ENDSTREAM_RE = re.compile(rb"\r?\n?endstream\b")
FILTER_RE = re.compile(rb"/Filter\s*(\[[^\]]+\]|/[A-Za-z0-9]+)")
DECODE_PARMS_RE = re.compile(rb"/DecodeParms\s*<<(.*?)>>", re.S)
NAME_RE = re.compile(rb"/[A-Za-z0-9]+")
OBJSTM_FIRST_RE = re.compile(rb"/First\s+(\d+)")
OBJSTM_COUNT_RE = re.compile(rb"/N\s+(\d+)")
REF_RE = re.compile(rb"(\d+)\s+(\d+)\s+R\b")
PAGE_TYPE_RE = re.compile(rb"/Type\s*/Page\b")
PAGES_TYPE_RE = re.compile(rb"/Type\s*/Pages\b")
CONTENTS_RE = re.compile(rb"/Contents\s+(\[[^\]]+\]|\d+\s+\d+\s+R)", re.S)


def decode_source(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16-be", "utf-16-le"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if text_quality(text) >= 0.65:
            return text
    return data.decode("latin-1", errors="replace")


def text_quality(text: str) -> float:
    if not text:
        return 1.0
    good = 0
    for char in text:
        codepoint = ord(char)
        if char in "\t\r\n":
            good += 1
        elif 32 <= codepoint <= 126:
            good += 1
        elif "\u4e00" <= char <= "\u9fff":
            good += 1
        elif char in "\u00a0，。；：？！、（）【】《》“”‘’":
            good += 1
    return good / len(text)


def is_text_like(data: bytes) -> bool:
    if not data:
        return True
    sample = data[:8192]
    printable = sum(1 for byte in sample if byte in b"\t\r\n" or 32 <= byte <= 126)
    controls = sum(1 for byte in sample if byte < 32 and byte not in b"\t\r\n")
    high_bytes = sum(1 for byte in sample if byte >= 127)
    if printable / len(sample) >= 0.55 and high_bytes / len(sample) <= 0.2:
        return True
    if controls / len(sample) > 0.25:
        return False
    return text_quality(decode_source(sample)) >= 0.75


def parse_pdf_names(value: bytes) -> list[str]:
    return [item.decode("ascii", errors="replace") for item in NAME_RE.findall(value)]


def stream_filters(header: bytes) -> list[str]:
    match = FILTER_RE.search(header)
    if match is None:
        return []
    return parse_pdf_names(match.group(1))


def decode_params(header: bytes) -> dict[str, int]:
    match = DECODE_PARMS_RE.search(header)
    if match is None:
        return {}
    params: dict[str, int] = {}
    for name, value in re.findall(rb"/([A-Za-z0-9]+)\s+(-?\d+)", match.group(1)):
        params["/" + name.decode("ascii", errors="replace")] = int(value)
    return params


def ascii_hex_decode(data: bytes) -> bytes:
    cleaned = re.sub(rb"\s+", b"", data).rstrip(b">")
    if len(cleaned) % 2 == 1:
        cleaned += b"0"
    return bytes.fromhex(cleaned.decode("ascii", errors="ignore"))


def run_length_decode(data: bytes) -> bytes:
    output = bytearray()
    pos = 0
    while pos < len(data):
        length = data[pos]
        pos += 1
        if length == 128:
            break
        if length < 128:
            output.extend(data[pos : pos + length + 1])
            pos += length + 1
        else:
            count = 257 - length
            if pos >= len(data):
                break
            output.extend(data[pos : pos + 1] * count)
            pos += 1
    return bytes(output)


def paeth_predictor(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    up_left_distance = abs(estimate - up_left)
    if left_distance <= up_distance and left_distance <= up_left_distance:
        return left
    if up_distance <= up_left_distance:
        return up
    return up_left


def apply_png_predictor(data: bytes, params: dict[str, int]) -> bytes:
    predictor = params.get("/Predictor", 1)
    if predictor < 10:
        return data

    colors = params.get("/Colors", 1)
    columns = params.get("/Columns", 1)
    bits_per_component = params.get("/BitsPerComponent", 8)
    bytes_per_pixel = max(1, math.ceil(colors * bits_per_component / 8))
    row_size = math.ceil(colors * columns * bits_per_component / 8)
    output = bytearray()
    previous = bytearray(row_size)
    pos = 0

    while pos < len(data):
        filter_type = data[pos]
        pos += 1
        row = bytearray(data[pos : pos + row_size])
        pos += row_size
        if len(row) < row_size:
            break

        if filter_type == 1:
            for index in range(row_size):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                row[index] = (row[index] + left) & 0xFF
        elif filter_type == 2:
            for index in range(row_size):
                row[index] = (row[index] + previous[index]) & 0xFF
        elif filter_type == 3:
            for index in range(row_size):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                row[index] = (row[index] + ((left + previous[index]) // 2)) & 0xFF
        elif filter_type == 4:
            for index in range(row_size):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                up = previous[index]
                up_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                row[index] = (row[index] + paeth_predictor(left, up, up_left)) & 0xFF
        output.extend(row)
        previous = row

    return bytes(output)


def decode_stream_data(raw_stream: bytes, header: bytes) -> tuple[bytes | None, str | None, list[str]]:
    filters = stream_filters(header)
    params = decode_params(header)
    decoded = raw_stream
    try:
        for filter_name in filters:
            if filter_name == "/FlateDecode":
                decoded = zlib.decompress(decoded)
                decoded = apply_png_predictor(decoded, params)
            elif filter_name == "/ASCIIHexDecode":
                decoded = ascii_hex_decode(decoded)
            elif filter_name == "/ASCII85Decode":
                decoded = base64.a85decode(decoded, adobe=True)
            elif filter_name == "/RunLengthDecode":
                decoded = run_length_decode(decoded)
            else:
                return None, f"Unsupported stream filter: {filter_name}", filters
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}", filters
    return decoded, None, filters


def decoded_stream_source(decoded: bytes, filters: list[str]) -> str:
    if is_text_like(decoded):
        return decode_source(decoded)
    preview = base64.b64encode(decoded[:4096]).decode("ascii")
    suffix = "" if len(decoded) <= 4096 else "\n... [base64 preview truncated]"
    return (
        f"[decoded binary stream; filters={filters or ['none']}; bytes={len(decoded)}; "
        "base64_preview]\n"
        f"{preview}{suffix}"
    )


def decode_streams_in_object_source(chunk: bytes) -> tuple[str, list[dict[str, Any]]]:
    parts: list[str] = []
    metadata: list[dict[str, Any]] = []
    cursor = 0

    while True:
        stream_match = STREAM_RE.search(chunk, cursor)
        if stream_match is None:
            parts.append(decode_source(chunk[cursor:]))
            break
        end_match = ENDSTREAM_RE.search(chunk, stream_match.end())
        if end_match is None:
            parts.append(decode_source(chunk[cursor:]))
            break

        header = chunk[cursor : stream_match.start()]
        raw_stream = chunk[stream_match.end() : end_match.start()]
        decoded, error, filters = decode_stream_data(raw_stream, header)

        parts.append(decode_source(chunk[cursor : stream_match.end()]))
        if decoded is None:
            parts.append(
                f"[raw stream kept; decode_error={error}; filters={filters or ['none']}; bytes={len(raw_stream)}]\n"
                + base64.b64encode(raw_stream[:4096]).decode("ascii")
            )
            metadata.append(
                {
                    "filters": filters,
                    "decoded": False,
                    "decode_error": error,
                    "raw_length": len(raw_stream),
                }
            )
        else:
            parts.append(decoded_stream_source(decoded, filters))
            metadata.append(
                {
                    "filters": filters,
                    "decoded": True,
                    "raw_length": len(raw_stream),
                    "decoded_length": len(decoded),
                    "decoded_as_text": is_text_like(decoded),
                }
            )
        parts.append(decode_source(chunk[end_match.start() : end_match.end()]))
        cursor = end_match.end()

    return "".join(parts), metadata


def raw_streams_in_object(chunk: bytes) -> list[tuple[bytes, bytes]]:
    streams: list[tuple[bytes, bytes]] = []
    cursor = 0
    while True:
        stream_match = STREAM_RE.search(chunk, cursor)
        if stream_match is None:
            return streams
        end_match = ENDSTREAM_RE.search(chunk, stream_match.end())
        if end_match is None:
            return streams
        header = chunk[cursor : stream_match.start()]
        raw_stream = chunk[stream_match.end() : end_match.start()]
        streams.append((header, raw_stream))
        cursor = end_match.end()


def line_end(data: bytes, start: int) -> int:
    cr = data.find(b"\r", start)
    lf = data.find(b"\n", start)
    candidates = [value for value in (cr, lf) if value >= 0]
    if not candidates:
        return len(data)
    pos = min(candidates)
    if data[pos : pos + 2] == b"\r\n":
        return pos + 2
    return pos + 1


def make_segment(data: bytes, index: int, kind: str, start: int, end: int, label: str | None = None) -> dict[str, Any]:
    chunk = data[start:end]
    if kind == "indirect_object":
        source, stream_metadata = decode_streams_in_object_source(chunk)
    else:
        source = decode_source(chunk)
        stream_metadata = []
    segment: dict[str, Any] = {
        "index": index,
        "type": kind,
        "offset": start,
        "end_offset": end,
        "length": len(chunk),
        "source": source,
    }
    if label is not None:
        segment["label"] = label
        object_ref = object_ref_from_label(label)
        if object_ref is not None:
            segment["object_ref"] = object_ref
    if stream_metadata:
        segment["streams"] = stream_metadata
    return segment


def object_ref_from_label(label: str) -> str | None:
    match = re.match(r"^(\d+)\s+(\d+)\s+obj$", label)
    if match is None:
        return None
    return f"{match.group(1)} {match.group(2)} R"


def indirect_object_spans(data: bytes) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    pos = 0
    while True:
        match = INDIRECT_OBJECT_START_RE.search(data, pos)
        if match is None:
            break

        end_match = ENDOBJ_RE.search(data, match.end())
        if end_match is None:
            pos = match.end()
            continue

        start = match.start()
        end = end_match.end()
        label = f"{int(match.group(1))} {int(match.group(2))} obj"
        spans.append((start, end, label))
        pos = end

    return spans


def indirect_object_map(data: bytes) -> dict[str, dict[str, Any]]:
    objects: dict[str, dict[str, Any]] = {}
    for start, end, label in indirect_object_spans(data):
        object_ref = object_ref_from_label(label)
        if object_ref is None:
            continue
        chunk = data[start:end]
        source, stream_metadata = decode_streams_in_object_source(chunk)
        entry: dict[str, Any] = {
            "object_ref": object_ref,
            "label": label,
            "offset": start,
            "end_offset": end,
            "length": end - start,
            "source": source,
            "raw": chunk,
        }
        if stream_metadata:
            entry["streams"] = stream_metadata
        objects[object_ref] = entry

    objects.update(object_stream_member_map(objects))
    return objects


def object_stream_member_map(objects: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    members_by_ref: dict[str, dict[str, Any]] = {}
    for container in objects.values():
        raw = container["raw"]
        if b"/ObjStm" not in raw:
            continue

        first_match = OBJSTM_FIRST_RE.search(raw)
        count_match = OBJSTM_COUNT_RE.search(raw)
        if first_match is None or count_match is None:
            continue

        raw_streams = raw_streams_in_object(raw)
        if not raw_streams:
            continue

        header, raw_stream = raw_streams[0]
        decoded, _error, _filters = decode_stream_data(raw_stream, header)
        if decoded is None:
            continue

        first = int(first_match.group(1))
        count = int(count_match.group(1))
        table = decoded[:first]
        numbers = [int(item) for item in re.findall(rb"\d+", table)]
        if len(numbers) < count * 2:
            continue

        offsets = [(numbers[index * 2], numbers[index * 2 + 1]) for index in range(count)]
        for index, (obj_num, relative_offset) in enumerate(offsets):
            start = first + relative_offset
            end = first + offsets[index + 1][1] if index + 1 < len(offsets) else len(decoded)
            raw_member = decoded[start:end].strip()
            object_ref = f"{obj_num} 0 R"
            members_by_ref[object_ref] = {
                "object_ref": object_ref,
                "label": f"{obj_num} 0 obj",
                "offset": None,
                "end_offset": None,
                "length": len(raw_member),
                "source": decode_source(raw_member),
                "raw": raw_member,
                "container": container["object_ref"],
                "container_offset": relative_offset,
            }
    return members_by_ref


def content_refs_from_page(raw_page_object: bytes) -> list[str]:
    match = CONTENTS_RE.search(raw_page_object)
    if match is None:
        return []
    return [f"{int(obj)} {int(gen)} R" for obj, gen in REF_RE.findall(match.group(1))]


def build_page_entries(data: bytes) -> tuple[list[dict[str, Any]], set[str]]:
    objects = indirect_object_map(data)
    page_objects = [
        item
        for item in objects.values()
        if PAGE_TYPE_RE.search(item["raw"]) is not None and PAGES_TYPE_RE.search(item["raw"]) is None
    ]
    page_objects.sort(key=lambda item: (item["offset"] is None, item["offset"] or 0, item["object_ref"]))

    page_entries: list[dict[str, Any]] = []
    displayed_object_refs: set[str] = set()
    output_index = 1

    for page_number, page_object in enumerate(page_objects, start=1):
        page_ref = page_object["object_ref"]
        displayed_object_refs.add(page_ref)
        page_entries.append(page_item(output_index, "page_object", page_number, page_object, page_ref))
        output_index += 1

        for content_ref in content_refs_from_page(page_object["raw"]):
            content_object = objects.get(content_ref)
            if content_object is None:
                page_entries.append(
                    {
                        "index": output_index,
                        "type": "missing_page_content_stream",
                        "page_number": page_number,
                        "page_object_ref": page_ref,
                        "object_ref": content_ref,
                        "source": "",
                    }
                )
                output_index += 1
                continue

            displayed_object_refs.add(content_ref)
            page_entries.append(page_item(output_index, "page_content_stream", page_number, content_object, page_ref))
            output_index += 1

    return page_entries, displayed_object_refs


def page_item(
    index: int,
    kind: str,
    page_number: int,
    source_object: dict[str, Any],
    page_ref: str,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "index": index,
        "type": kind,
        "page_number": page_number,
        "page_object_ref": page_ref,
        "object_ref": source_object["object_ref"],
        "offset": source_object["offset"],
        "end_offset": source_object["end_offset"],
        "length": source_object["length"],
        "source": source_object["source"],
    }
    if "container" in source_object:
        item["container"] = source_object["container"]
        item["container_offset"] = source_object["container_offset"]
    if "streams" in source_object:
        item["streams"] = source_object["streams"]
    return item


def spans_overlap(start: int, end: int, spans: list[tuple[int, int, str]]) -> bool:
    for span_start, span_end, _label in spans:
        if start < span_end and end > span_start:
            return True
    return False


def classic_xref_spans(data: bytes, object_spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in CLASSIC_XREF_RE.finditer(data):
        start = match.start()
        if data[start : start + 1] in {b"\r", b"\n"}:
            start += 1
        if spans_overlap(start, match.end(), object_spans):
            continue

        next_startxref = STARTXREF_RE.search(data, match.end())
        end = next_startxref.start() if next_startxref else len(data)
        spans.append((start, end, "xref"))
    return spans


def startxref_span(data: bytes, occupied_spans: list[tuple[int, int, str]]) -> tuple[int, int, str] | None:
    matches = list(STARTXREF_RE.finditer(data))
    if not matches:
        return None

    start = matches[-1].start()
    if spans_overlap(start, matches[-1].end(), occupied_spans):
        return None
    return (start, len(data), "startxref")


def build_segments(data: bytes, excluded_object_refs: set[str] | None = None) -> list[dict[str, Any]]:
    excluded_object_refs = excluded_object_refs or set()
    raw_spans: list[tuple[int, int, str, str]] = []

    header_end = line_end(data, 0)
    if header_end > 0:
        raw_spans.append((0, header_end, "header", "PDF header"))

    object_spans = indirect_object_spans(data)
    raw_spans.extend((start, end, "indirect_object", label) for start, end, label in object_spans)

    xref_spans = classic_xref_spans(data, object_spans)
    raw_spans.extend((start, end, "xref_and_trailer", label) for start, end, label in xref_spans)

    occupied = [(start, end, label) for start, end, _kind, label in raw_spans]
    final_startxref = startxref_span(data, occupied)
    if final_startxref is not None:
        start, end, label = final_startxref
        raw_spans.append((start, end, "startxref_and_eof", label))

    raw_spans.sort(key=lambda item: (item[0], item[1]))

    segments: list[dict[str, Any]] = []
    cursor = 0
    index = 1
    for start, end, kind, label in raw_spans:
        if start < cursor:
            continue
        if start > cursor:
            segments.append(make_segment(data, index, "unclassified", cursor, start))
            index += 1
        segment = make_segment(data, index, kind, start, end, label)
        if segment.get("object_ref") not in excluded_object_refs:
            segments.append(segment)
            index += 1
        cursor = end

    if cursor < len(data):
        segments.append(make_segment(data, index, "unclassified", cursor, len(data)))

    return segments


def build_step1_json(pdf_path: Path) -> dict[str, Any]:
    data = pdf_path.read_bytes()
    pages, displayed_object_refs = build_page_entries(data)
    segments = build_segments(data, excluded_object_refs=displayed_object_refs)
    counts: dict[str, int] = {}
    for segment in segments:
        kind = segment["type"]
        counts[kind] = counts.get(kind, 0) + 1

    return {
        "pdf_file": {
            "path": str(pdf_path),
            "name": pdf_path.name,
            "size_bytes": len(data),
        },
        "source_encoding": "decoded text when possible; binary streams use base64 preview",
        "page_count": len({item["page_number"] for item in pages}),
        "page_item_count": len(pages),
        "segment_count": len(segments),
        "segment_type_counts": counts,
        "pages": pages,
        "segments": segments,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split a PDF into coarse raw source segments.")
    parser.add_argument("pdf", nargs="?", help="Path to the source PDF file.")
    parser.add_argument("--input", dest="input_path", help="Path to the source PDF file.")
    parser.add_argument("--output", dest="output_path", help="Output JSON path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_value = args.input_path or args.pdf
    if not input_value:
        raise SystemExit("Please provide a PDF path, for example: python -m pdf_parser data/eplans/demo.pdf")

    pdf_path = Path(input_value)
    if not pdf_path.is_absolute():
        pdf_path = (REPO_ROOT / pdf_path).resolve()
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    if args.output_path:
        output_path = Path(args.output_path)
        if not output_path.is_absolute():
            output_path = (REPO_ROOT / output_path).resolve()
    else:
        output_path = REPO_ROOT / "output" / "pdf_parser_step1" / f"{pdf_path.stem}.json"

    write_json(output_path, build_step1_json(pdf_path))
    print(f"Raw PDF source segments written to: {output_path}")


if __name__ == "__main__":
    main()
