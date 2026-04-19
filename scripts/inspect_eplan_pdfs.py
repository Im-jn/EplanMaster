from __future__ import annotations

import argparse
import base64
import json
import math
import re
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WHITESPACE = b"\x00\t\n\f\r "
DELIMITERS = b"()<>[]{}/%"
INDIRECT_OBJECT_RE = re.compile(rb"(?<!\d)(\d+)\s+(\d+)\s+obj\b", re.M)
STARTXREF_RE = re.compile(rb"startxref\s+(\d+)\s+%%EOF", re.S)


@dataclass(frozen=True)
class PDFRef:
    obj_num: int
    gen_num: int = 0

    def label(self) -> str:
        return f"{self.obj_num} {self.gen_num} R"


@dataclass(frozen=True)
class PDFName:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class PDFString:
    text: str
    raw: bytes
    kind: str

    def __str__(self) -> str:
        return self.text


@dataclass
class ParsedIndirectObject:
    obj_num: int
    gen_num: int
    value: Any
    raw_bytes: bytes
    offset: int | None
    source: str
    stream_raw: bytes | None = None
    decoded_stream: bytes | None = None
    decode_error: str | None = None
    container: str | None = None

    @property
    def object_id(self) -> str:
        return f"{self.obj_num}_{self.gen_num}"

    @property
    def ref(self) -> PDFRef:
        return PDFRef(self.obj_num, self.gen_num)


def is_whitespace(byte_value: int) -> bool:
    return byte_value in WHITESPACE


def is_delimiter(byte_value: int) -> bool:
    return byte_value in DELIMITERS


class PDFValueParser:
    def __init__(self, data: bytes, start: int = 0) -> None:
        self.data = data
        self.pos = start

    def skip_ws_and_comments(self) -> None:
        while self.pos < len(self.data):
            current = self.data[self.pos]
            if is_whitespace(current):
                self.pos += 1
                continue
            if current == ord("%"):
                self.pos += 1
                while self.pos < len(self.data) and self.data[self.pos] not in b"\r\n":
                    self.pos += 1
                continue
            break

    def parse_value(self, allow_operator: bool = False) -> Any:
        self.skip_ws_and_comments()
        if self.pos >= len(self.data):
            raise ValueError("Unexpected end of data while parsing PDF value")

        if self.data.startswith(b"<<", self.pos):
            return self.parse_dictionary()
        if self.data[self.pos : self.pos + 1] == b"[":
            return self.parse_array()
        if self.data[self.pos : self.pos + 1] == b"/":
            return self.parse_name()
        if self.data[self.pos : self.pos + 1] == b"(":
            return self.parse_literal_string()
        if self.data[self.pos : self.pos + 1] == b"<":
            return self.parse_hex_string()

        if self.data[self.pos : self.pos + 1] in b"+-.0123456789":
            return self.parse_number_or_ref()

        token = self.read_keyword()
        if token == b"true":
            return True
        if token == b"false":
            return False
        if token == b"null":
            return None
        if allow_operator:
            return token.decode("latin-1", errors="replace")
        raise ValueError(f"Unexpected token {token!r}")

    def parse_number_or_ref(self) -> Any:
        value = self.parse_number()
        saved_pos = self.pos
        if not isinstance(value, int):
            return value

        self.skip_ws_and_comments()
        if self.pos >= len(self.data) or self.data[self.pos : self.pos + 1] not in b"+-0123456789":
            return value

        try:
            second_value = self.parse_number()
        except ValueError:
            self.pos = saved_pos
            return value

        if not isinstance(second_value, int):
            self.pos = saved_pos
            return value

        self.skip_ws_and_comments()
        if self.data[self.pos : self.pos + 1] == b"R":
            self.pos += 1
            return PDFRef(value, second_value)

        self.pos = saved_pos
        return value

    def parse_number(self) -> int | float:
        start = self.pos
        while self.pos < len(self.data) and self.data[self.pos : self.pos + 1] in b"+-.0123456789":
            self.pos += 1
        if start == self.pos:
            raise ValueError("Expected number")
        raw = self.data[start : self.pos]
        if b"." in raw or raw in {b"+", b"-", b".", b"+.", b"-."}:
            return float(raw)
        return int(raw)

    def parse_name(self) -> PDFName:
        if self.data[self.pos : self.pos + 1] != b"/":
            raise ValueError("Expected name")
        self.pos += 1
        chunks = bytearray()
        while self.pos < len(self.data):
            byte_value = self.data[self.pos]
            if is_whitespace(byte_value) or is_delimiter(byte_value):
                break
            if byte_value == ord("#") and self.pos + 2 < len(self.data):
                candidate = self.data[self.pos + 1 : self.pos + 3]
                try:
                    chunks.append(int(candidate, 16))
                    self.pos += 3
                    continue
                except ValueError:
                    pass
            chunks.append(byte_value)
            self.pos += 1
        return PDFName("/" + chunks.decode("latin-1", errors="replace"))

    def parse_array(self) -> list[Any]:
        self.pos += 1
        values: list[Any] = []
        while True:
            self.skip_ws_and_comments()
            if self.pos >= len(self.data):
                raise ValueError("Unexpected EOF inside array")
            if self.data[self.pos : self.pos + 1] == b"]":
                self.pos += 1
                return values
            values.append(self.parse_value())

    def parse_dictionary(self) -> dict[str, Any]:
        self.pos += 2
        result: dict[str, Any] = {}
        while True:
            self.skip_ws_and_comments()
            if self.pos >= len(self.data):
                raise ValueError("Unexpected EOF inside dictionary")
            if self.data.startswith(b">>", self.pos):
                self.pos += 2
                return result
            key = self.parse_name()
            value = self.parse_value()
            result[key.value] = value

    def parse_literal_string(self) -> PDFString:
        self.pos += 1
        depth = 1
        result = bytearray()
        while self.pos < len(self.data):
            byte_value = self.data[self.pos]
            if byte_value == ord("\\"):
                self.pos += 1
                if self.pos >= len(self.data):
                    break
                esc = self.data[self.pos]
                mapping = {
                    ord("n"): b"\n",
                    ord("r"): b"\r",
                    ord("t"): b"\t",
                    ord("b"): b"\b",
                    ord("f"): b"\f",
                    ord("("): b"(",
                    ord(")"): b")",
                    ord("\\"): b"\\",
                }
                if esc in mapping:
                    result.extend(mapping[esc])
                    self.pos += 1
                    continue
                if esc in b"\r\n":
                    if esc == ord("\r") and self.pos + 1 < len(self.data) and self.data[self.pos + 1] == ord("\n"):
                        self.pos += 2
                    else:
                        self.pos += 1
                    continue
                if chr(esc).isdigit():
                    octal = bytes([esc])
                    self.pos += 1
                    for _ in range(2):
                        if self.pos < len(self.data) and chr(self.data[self.pos]).isdigit():
                            octal += bytes([self.data[self.pos]])
                            self.pos += 1
                        else:
                            break
                    result.append(int(octal, 8))
                    continue
                result.append(esc)
                self.pos += 1
                continue
            if byte_value == ord("("):
                depth += 1
                result.append(byte_value)
                self.pos += 1
                continue
            if byte_value == ord(")"):
                depth -= 1
                self.pos += 1
                if depth == 0:
                    return PDFString(result.decode("latin-1", errors="replace"), bytes(result), "literal")
                result.append(byte_value)
                continue
            result.append(byte_value)
            self.pos += 1
        raise ValueError("Unterminated literal string")

    def parse_hex_string(self) -> PDFString:
        self.pos += 1
        start = self.pos
        while self.pos < len(self.data) and self.data[self.pos : self.pos + 1] != b">":
            self.pos += 1
        if self.pos >= len(self.data):
            raise ValueError("Unterminated hex string")
        raw = re.sub(rb"\s+", b"", self.data[start : self.pos])
        self.pos += 1
        if len(raw) % 2 == 1:
            raw += b"0"
        decoded = bytes.fromhex(raw.decode("ascii", errors="ignore"))
        return PDFString(decoded.decode("latin-1", errors="replace"), decoded, "hex")

    def read_keyword(self) -> bytes:
        start = self.pos
        while self.pos < len(self.data):
            byte_value = self.data[self.pos]
            if is_whitespace(byte_value) or is_delimiter(byte_value):
                break
            self.pos += 1
        if start == self.pos:
            raise ValueError("Expected keyword")
        return self.data[start : self.pos]


def parse_indirect_object(data: bytes, offset: int) -> ParsedIndirectObject:
    header_match = INDIRECT_OBJECT_RE.match(data, offset)
    if not header_match:
        raise ValueError(f"No indirect object at offset {offset}")

    obj_num = int(header_match.group(1))
    gen_num = int(header_match.group(2))
    parser = PDFValueParser(data, header_match.end())
    value = parser.parse_value()
    parser.skip_ws_and_comments()

    stream_raw: bytes | None = None
    if isinstance(value, dict) and data.startswith(b"stream", parser.pos):
        stream_start = parser.pos + len(b"stream")
        if data.startswith(b"\r\n", stream_start):
            stream_start += 2
        elif data[stream_start : stream_start + 1] in {b"\r", b"\n"}:
            stream_start += 1

        declared_length = value.get("/Length")
        stream_end_marker = -1
        if isinstance(declared_length, int):
            candidate_end = stream_start + declared_length
            for marker in (candidate_end, candidate_end + 1, candidate_end + 2):
                lookahead = data[marker : marker + 11]
                if lookahead.startswith(b"endstream"):
                    stream_end_marker = marker
                    stream_raw = data[stream_start:marker]
                    break
                if lookahead.startswith(b"\nendstream"):
                    stream_end_marker = marker + 1
                    stream_raw = data[stream_start:marker]
                    break
                if lookahead.startswith(b"\r\nendstream"):
                    stream_end_marker = marker + 2
                    stream_raw = data[stream_start:marker]
                    break
        if stream_raw is None:
            stream_end_marker = data.find(b"endstream", stream_start)
            if stream_end_marker < 0:
                raise ValueError(f"Could not find endstream for object {obj_num} {gen_num}")
            stream_raw = data[stream_start:stream_end_marker]
            while stream_raw.endswith((b"\r", b"\n")):
                stream_raw = stream_raw[:-1]

        parser.pos = stream_end_marker + len(b"endstream")
        parser.skip_ws_and_comments()

    end_start = parser.pos
    if not data.startswith(b"endobj", end_start):
        end_start = data.find(b"endobj", parser.pos)
        if end_start < 0:
            raise ValueError(f"Could not find endobj for object {obj_num} {gen_num}")
    raw_bytes = data[offset : end_start + len(b"endobj")]
    return ParsedIndirectObject(
        obj_num=obj_num,
        gen_num=gen_num,
        value=value,
        raw_bytes=raw_bytes,
        offset=offset,
        source="file",
        stream_raw=stream_raw,
    )


def normalize_filter_list(filter_value: Any) -> list[str]:
    if filter_value is None:
        return []
    if isinstance(filter_value, PDFName):
        return [filter_value.value]
    if isinstance(filter_value, list):
        result: list[str] = []
        for item in filter_value:
            result.append(item.value if isinstance(item, PDFName) else str(item))
        return result
    return [str(filter_value)]


def normalize_decode_params(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def ascii_hex_decode(data: bytes) -> bytes:
    cleaned = re.sub(rb"\s+", b"", data)
    cleaned = cleaned.rstrip(b">")
    if len(cleaned) % 2 == 1:
        cleaned += b"0"
    return bytes.fromhex(cleaned.decode("ascii", errors="ignore"))


def run_length_decode(data: bytes) -> bytes:
    result = bytearray()
    index = 0
    while index < len(data):
        length = data[index]
        index += 1
        if length == 128:
            break
        if length < 128:
            chunk = data[index : index + length + 1]
            result.extend(chunk)
            index += length + 1
        else:
            count = 257 - length
            if index >= len(data):
                break
            result.extend(data[index : index + 1] * count)
            index += 1
    return bytes(result)


def paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def apply_png_predictor(data: bytes, params: Any) -> bytes:
    if not isinstance(params, dict):
        return data

    predictor = params.get("/Predictor", 1)
    if predictor in {1, None} or predictor < 10:
        return data

    colors = int(params.get("/Colors", 1))
    columns = int(params.get("/Columns", 1))
    bits_per_component = int(params.get("/BitsPerComponent", 8))
    bytes_per_pixel = max(1, math.ceil(colors * bits_per_component / 8))
    row_size = math.ceil(colors * columns * bits_per_component / 8)

    output = bytearray()
    offset = 0
    previous_row = bytearray(row_size)
    while offset < len(data):
        filter_type = data[offset]
        offset += 1
        row = bytearray(data[offset : offset + row_size])
        offset += row_size
        if len(row) < row_size:
            break

        if filter_type == 1:
            for i in range(row_size):
                left = row[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                row[i] = (row[i] + left) & 0xFF
        elif filter_type == 2:
            for i in range(row_size):
                row[i] = (row[i] + previous_row[i]) & 0xFF
        elif filter_type == 3:
            for i in range(row_size):
                left = row[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                up = previous_row[i]
                row[i] = (row[i] + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:
            for i in range(row_size):
                left = row[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                up = previous_row[i]
                up_left = previous_row[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                row[i] = (row[i] + paeth_predictor(left, up, up_left)) & 0xFF
        output.extend(row)
        previous_row = row

    return bytes(output)


def decode_stream_bytes(raw_stream: bytes, stream_dict: dict[str, Any]) -> tuple[bytes | None, str | None]:
    filters = normalize_filter_list(stream_dict.get("/Filter"))
    params = normalize_decode_params(stream_dict.get("/DecodeParms"))
    decoded = raw_stream

    if not filters:
        return decoded, None

    try:
        for index, filter_name in enumerate(filters):
            param = params[index] if index < len(params) else None
            if filter_name == "/FlateDecode":
                decoded = zlib.decompress(decoded)
                decoded = apply_png_predictor(decoded, param)
            elif filter_name == "/ASCII85Decode":
                decoded = base64.a85decode(decoded, adobe=True)
            elif filter_name == "/ASCIIHexDecode":
                decoded = ascii_hex_decode(decoded)
            elif filter_name == "/RunLengthDecode":
                decoded = run_length_decode(decoded)
            else:
                return None, f"Unsupported filter {filter_name}"
        return decoded, None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def is_name(value: Any, expected: str) -> bool:
    return isinstance(value, PDFName) and value.value == expected


def value_to_serializable(value: Any) -> Any:
    if isinstance(value, PDFRef):
        return {"type": "ref", "value": value.label()}
    if isinstance(value, PDFName):
        return value.value
    if isinstance(value, PDFString):
        return value.text
    if isinstance(value, dict):
        return {key: value_to_serializable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [value_to_serializable(item) for item in value]
    return value


def pdf_value_to_source(value: Any) -> str:
    if isinstance(value, PDFRef):
        return value.label()
    if isinstance(value, PDFName):
        return value.value
    if isinstance(value, PDFString):
        if value.kind == "hex":
            return "<" + value.raw.hex() + ">"
        escaped = (
            value.raw.replace(b"\\", b"\\\\")
            .replace(b"(", b"\\(")
            .replace(b")", b"\\)")
            .decode("latin-1", errors="replace")
        )
        return f"({escaped})"
    if isinstance(value, list):
        return "[" + " ".join(pdf_value_to_source(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = ["<<"]
        for key, item in value.items():
            parts.append(f"{key} {pdf_value_to_source(item)}")
        parts.append(">>")
        return "\n".join(parts)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def decode_text_for_view(data: bytes, limit: int | None = None) -> str:
    text = data.decode("latin-1", errors="replace")
    if limit is not None and len(text) > limit:
        return text[:limit] + "\n... [truncated]"
    return text


def safe_slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return slug.strip("._") or "document"


def looks_text_like(data: bytes) -> bool:
    if not data:
        return True
    sample = data[:4096]
    printable = sum(1 for byte in sample if byte in b"\t\n\r" or 32 <= byte <= 126)
    return printable / len(sample) >= 0.82


def matrix_multiply(
    left: tuple[float, float, float, float, float, float],
    right: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    a1, b1, c1, d1, e1, f1 = left
    a2, b2, c2, d2, e2, f2 = right
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def matrix_from_array(value: Any) -> tuple[float, float, float, float, float, float] | None:
    if not isinstance(value, list) or len(value) != 6:
        return None
    try:
        return tuple(float(item) for item in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def transform_point(matrix: tuple[float, float, float, float, float, float], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return (a * x + c * y + e, b * x + d * y + f)


def bbox_from_matrix(matrix: tuple[float, float, float, float, float, float]) -> list[float]:
    points = [
        transform_point(matrix, 0.0, 0.0),
        transform_point(matrix, 1.0, 0.0),
        transform_point(matrix, 0.0, 1.0),
        transform_point(matrix, 1.0, 1.0),
    ]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def decode_pdf_text_bytes(raw: bytes) -> str:
    if not raw:
        return ""

    candidates: list[str] = []
    if raw.startswith(b"\xfe\xff"):
        candidates.append("utf-16-be")
        raw = raw[2:]
    elif raw.startswith(b"\xff\xfe"):
        candidates.append("utf-16-le")
        raw = raw[2:]

    if b"\x00" in raw:
        even_zeros = sum(1 for item in raw[0::2] if item == 0)
        odd_zeros = sum(1 for item in raw[1::2] if item == 0)
        if even_zeros > odd_zeros:
            candidates.extend(["utf-16-be", "utf-16-le"])
        elif odd_zeros > even_zeros:
            candidates.extend(["utf-16-le", "utf-16-be"])
        else:
            candidates.extend(["utf-16-be", "utf-16-le"])

    candidates.extend(["utf-8", "latin-1"])

    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def sanitize_text(text: str) -> str:
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def readable_string(value: Any) -> str:
    if isinstance(value, PDFString):
        return sanitize_text(decode_pdf_text_bytes(value.raw))
    if isinstance(value, str):
        return sanitize_text(value)
    return sanitize_text(str(value))


def join_text_array(values: Any) -> str:
    if not isinstance(values, list):
        return ""
    parts: list[str] = []
    for item in values:
        if isinstance(item, PDFString):
            text = readable_string(item)
            if text:
                parts.append(text)
    return sanitize_text("".join(parts))


def numeric_list(values: Any) -> list[float] | None:
    if not isinstance(values, list):
        return None
    result: list[float] = []
    for item in values:
        if not isinstance(item, (int, float)):
            return None
        result.append(float(item))
    return result


def rect_to_readable(rect: Any) -> dict[str, float] | None:
    numbers = numeric_list(rect)
    if numbers is None or len(numbers) != 4:
        return None
    x0, y0, x1, y1 = numbers
    return {
        "x0": round(x0, 3),
        "y0": round(y0, 3),
        "x1": round(x1, 3),
        "y1": round(y1, 3),
        "width": round(x1 - x0, 3),
        "height": round(y1 - y0, 3),
    }


def destination_to_readable(dest: Any) -> Any:
    if isinstance(dest, list) and dest:
        result: dict[str, Any] = {}
        first = dest[0]
        if isinstance(first, dict) and first.get("type") == "ref":
            result["page_ref"] = first.get("value")
        elif isinstance(first, str):
            result["named_destination"] = first
        if len(dest) > 1 and isinstance(dest[1], str):
            result["fit_mode"] = dest[1]
        if len(dest) > 2:
            result["params"] = dest[2:]
        return result
    return dest


def action_to_readable(action: Any) -> Any:
    if not isinstance(action, dict):
        return action
    result = {
        "type": action.get("/S"),
    }
    if "/D" in action:
        result["destination"] = destination_to_readable(action.get("/D"))
    if "/URI" in action:
        result["uri"] = action.get("/URI")
    next_action = action.get("/Next")
    if isinstance(next_action, dict) and next_action.get("type") == "ref":
        result["next_action_ref"] = next_action.get("value")
    elif next_action is not None:
        result["next_action"] = next_action
    return result


def decode_cmap_target(hex_text: str) -> str:
    raw = bytes.fromhex(hex_text)
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def parse_tounicode_cmap(data: bytes) -> dict[bytes, str]:
    text = data.decode("latin-1", errors="replace")
    mapping: dict[bytes, str] = {}

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, flags=re.S):
        for source_hex, target_hex in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            mapping[bytes.fromhex(source_hex)] = decode_cmap_target(target_hex)

    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, flags=re.S):
        array_pattern = re.compile(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", re.S)
        for source_start_hex, source_end_hex, target_array in array_pattern.findall(block):
            targets = re.findall(r"<([0-9A-Fa-f]+)>", target_array)
            source_start = int(source_start_hex, 16)
            source_end = int(source_end_hex, 16)
            code_len = len(source_start_hex) // 2
            for offset, source_code in enumerate(range(source_start, source_end + 1)):
                if offset >= len(targets):
                    break
                mapping[source_code.to_bytes(code_len, "big")] = decode_cmap_target(targets[offset])

        simple_range_pattern = re.compile(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
        for source_start_hex, source_end_hex, target_start_hex in simple_range_pattern.findall(block):
            source_start = int(source_start_hex, 16)
            source_end = int(source_end_hex, 16)
            target_start = int(target_start_hex, 16)
            code_len = len(source_start_hex) // 2
            target_len = len(target_start_hex) // 2
            for offset, source_code in enumerate(range(source_start, source_end + 1)):
                target_code = target_start + offset
                mapping[source_code.to_bytes(code_len, "big")] = decode_cmap_target(
                    target_code.to_bytes(target_len, "big").hex()
                )

    return mapping


def decode_with_cmap(raw: bytes, cmap: dict[bytes, str]) -> str:
    if not raw or not cmap:
        return ""

    lengths = sorted({len(key) for key in cmap if key}, reverse=True)
    if not lengths:
        return ""

    parts: list[str] = []
    pos = 0
    while pos < len(raw):
        matched = False
        for length in lengths:
            chunk = raw[pos : pos + length]
            if chunk in cmap:
                parts.append(cmap[chunk])
                pos += length
                matched = True
                break
        if matched:
            continue
        pos += lengths[-1]

    return sanitize_text("".join(parts))


class ContentTokenizer:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.parser = PDFValueParser(data)

    def skip_inline_image(self) -> None:
        id_match = re.search(rb"\sID[\r\n ]", self.data[self.parser.pos :])
        if not id_match:
            self.parser.pos = len(self.data)
            return

        self.parser.pos += id_match.end()
        end_match = re.search(rb"(?s)\sEI(?=\s|$)", self.data[self.parser.pos :])
        if not end_match:
            self.parser.pos = len(self.data)
            return
        self.parser.pos += end_match.end()

    def next_token(self) -> Any | None:
        self.parser.skip_ws_and_comments()
        if self.parser.pos >= len(self.data):
            return None

        current = self.data[self.parser.pos : self.parser.pos + 1]
        if current in {b"/", b"[", b"(", b"<"} or current in b"+-.0123456789":
            return self.parser.parse_value()
        return self.parser.parse_value(allow_operator=True)


class PDFInspector:
    def __init__(self, pdf_path: Path) -> None:
        self.pdf_path = pdf_path
        self.data = pdf_path.read_bytes()
        self.objects: dict[tuple[int, int], ParsedIndirectObject] = {}
        self.top_level_objects: list[ParsedIndirectObject] = []
        self.font_cmap_cache: dict[tuple[int, int], dict[bytes, str]] = {}
        self.startxref: int | None = None
        self.header = self._read_header()
        self._scan_top_level_objects()
        self._decode_stream_objects()
        self._extract_object_stream_members()

    def _read_header(self) -> str:
        match = re.match(rb"%PDF-[^\r\n]+", self.data)
        return match.group(0).decode("latin-1") if match else "Unknown"

    def _scan_top_level_objects(self) -> None:
        pos = 0
        while True:
            match = INDIRECT_OBJECT_RE.search(self.data, pos)
            if not match:
                break
            offset = match.start()
            try:
                parsed = parse_indirect_object(self.data, offset)
            except Exception:  # noqa: BLE001
                pos = match.end()
                continue
            self.top_level_objects.append(parsed)
            self.objects[(parsed.obj_num, parsed.gen_num)] = parsed
            pos = offset + len(parsed.raw_bytes)

        startxref_match = STARTXREF_RE.search(self.data[-8192:])
        if startxref_match:
            self.startxref = int(startxref_match.group(1))

    def _decode_stream_objects(self) -> None:
        for parsed in self.top_level_objects:
            if parsed.stream_raw is None or not isinstance(parsed.value, dict):
                continue
            decoded, error = decode_stream_bytes(parsed.stream_raw, parsed.value)
            parsed.decoded_stream = decoded
            parsed.decode_error = error

    def _extract_object_stream_members(self) -> None:
        containers = [
            obj
            for obj in self.top_level_objects
            if isinstance(obj.value, dict) and is_name(obj.value.get("/Type"), "/ObjStm")
        ]
        for container in containers:
            if container.decoded_stream is None:
                continue
            first = container.value.get("/First")
            count = container.value.get("/N")
            if not isinstance(first, int) or not isinstance(count, int):
                continue

            header_bytes = container.decoded_stream[:first]
            body = container.decoded_stream
            numbers = [int(item) for item in re.findall(rb"\d+", header_bytes)]
            if len(numbers) < count * 2:
                continue

            members: list[tuple[int, int]] = []
            for index in range(count):
                members.append((numbers[index * 2], numbers[index * 2 + 1]))

            for index, (obj_num, rel_offset) in enumerate(members):
                start = first + rel_offset
                end = first + members[index + 1][1] if index + 1 < len(members) else len(body)
                raw_member = body[start:end].rstrip()
                parser = PDFValueParser(body, start)
                try:
                    value = parser.parse_value()
                except Exception:  # noqa: BLE001
                    continue
                member = ParsedIndirectObject(
                    obj_num=obj_num,
                    gen_num=0,
                    value=value,
                    raw_bytes=raw_member,
                    offset=None,
                    source="object_stream",
                    container=container.ref.label(),
                )
                self.objects[(member.obj_num, member.gen_num)] = member

    def get_object(self, ref: PDFRef | tuple[int, int] | None) -> ParsedIndirectObject | None:
        if ref is None:
            return None
        if isinstance(ref, tuple):
            return self.objects.get(ref)
        if isinstance(ref, PDFRef):
            obj = self.objects.get((ref.obj_num, ref.gen_num))
            if obj is not None:
                return obj
            return self.objects.get((ref.obj_num, 0))
        return None

    def resolve(self, value: Any, limit: int = 25) -> Any:
        current = value
        depth = 0
        while isinstance(current, PDFRef) and depth < limit:
            target = self.get_object(current)
            if target is None:
                return current
            current = target.value
            depth += 1
        return current

    def xref_object(self) -> ParsedIndirectObject | None:
        if self.startxref is None:
            return None
        for obj in self.top_level_objects:
            if obj.offset == self.startxref:
                return obj
        return None

    def catalog_object(self) -> ParsedIndirectObject | None:
        root_from_xref = None
        xref_obj = self.xref_object()
        if xref_obj and isinstance(xref_obj.value, dict):
            root_candidate = xref_obj.value.get("/Root")
            if isinstance(root_candidate, PDFRef):
                root_from_xref = self.get_object(root_candidate)
        if root_from_xref is not None:
            return root_from_xref

        for obj in self.objects.values():
            if isinstance(obj.value, dict) and is_name(obj.value.get("/Type"), "/Catalog"):
                return obj
        return None

    def info_object(self) -> ParsedIndirectObject | None:
        xref_obj = self.xref_object()
        if xref_obj and isinstance(xref_obj.value, dict):
            info_ref = xref_obj.value.get("/Info")
            if isinstance(info_ref, PDFRef):
                return self.get_object(info_ref)
        return None

    def metadata_object(self) -> ParsedIndirectObject | None:
        catalog = self.catalog_object()
        if catalog is None or not isinstance(catalog.value, dict):
            return None
        metadata_ref = catalog.value.get("/Metadata")
        if isinstance(metadata_ref, PDFRef):
            return self.get_object(metadata_ref)
        return None

    def iter_pages(self) -> list[dict[str, Any]]:
        catalog = self.catalog_object()
        if catalog is None or not isinstance(catalog.value, dict):
            return []
        pages_ref = catalog.value.get("/Pages")
        if pages_ref is None:
            return []

        pages: list[dict[str, Any]] = []

        def walk(node_token: Any, inherited: dict[str, Any]) -> None:
            node_value = self.resolve(node_token)
            node_obj = self.get_object(node_token) if isinstance(node_token, PDFRef) else None
            if not isinstance(node_value, dict):
                return

            next_inherited = dict(inherited)
            for key in ("/Resources", "/MediaBox", "/CropBox", "/Rotate"):
                if key in node_value:
                    next_inherited[key] = node_value[key]

            if is_name(node_value.get("/Type"), "/Pages") or "/Kids" in node_value:
                kids = self.resolve(node_value.get("/Kids", []))
                if isinstance(kids, list):
                    for kid in kids:
                        walk(kid, next_inherited)
                return

            pages.append(
                {
                    "index": len(pages) + 1,
                    "object": node_obj,
                    "value": node_value,
                    "inherited": next_inherited,
                }
            )

        walk(pages_ref, {})
        return pages

    def content_stream_objects_for_page(self, page_entry: dict[str, Any]) -> list[ParsedIndirectObject]:
        page_value = page_entry["value"]
        contents = page_value.get("/Contents")
        if contents is None:
            return []

        if isinstance(contents, PDFRef):
            resolved = self.resolve(contents)
            if isinstance(resolved, list):
                refs = resolved
            else:
                stream_obj = self.get_object(contents)
                return [stream_obj] if stream_obj is not None else []
        else:
            refs = contents if isinstance(contents, list) else []

        result: list[ParsedIndirectObject] = []
        for item in refs:
            if isinstance(item, PDFRef):
                obj = self.get_object(item)
                if obj is not None:
                    result.append(obj)
        return result

    def resolve_resources_dict(self, token: Any) -> dict[str, Any]:
        value = self.resolve(token)
        return value if isinstance(value, dict) else {}

    def resolve_xobject(self, resources_token: Any, xobject_name: str) -> ParsedIndirectObject | None:
        resources = self.resolve_resources_dict(resources_token)
        xobjects = self.resolve(resources.get("/XObject"))
        if not isinstance(xobjects, dict):
            return None
        target = xobjects.get(xobject_name)
        if isinstance(target, PDFRef):
            return self.get_object(target)
        return None

    def resolve_font(self, resources_token: Any, font_name: str) -> ParsedIndirectObject | None:
        resources = self.resolve_resources_dict(resources_token)
        fonts = self.resolve(resources.get("/Font"))
        if not isinstance(fonts, dict):
            return None
        target = fonts.get(font_name)
        if isinstance(target, PDFRef):
            return self.get_object(target)
        return None

    def resolve_font_cmap(self, resources_token: Any, font_name: str) -> dict[bytes, str]:
        font_obj = self.resolve_font(resources_token, font_name)
        if font_obj is None or not isinstance(font_obj.value, dict):
            return {}
        cache_key = (font_obj.obj_num, font_obj.gen_num)
        if cache_key in self.font_cmap_cache:
            return self.font_cmap_cache[cache_key]

        to_unicode_ref = font_obj.value.get("/ToUnicode")
        if not isinstance(to_unicode_ref, PDFRef):
            self.font_cmap_cache[cache_key] = {}
            return {}

        cmap_obj = self.get_object(to_unicode_ref)
        if cmap_obj is None or cmap_obj.stream_raw is None:
            self.font_cmap_cache[cache_key] = {}
            return {}
        if cmap_obj.decoded_stream is None and isinstance(cmap_obj.value, dict):
            decoded, error = decode_stream_bytes(cmap_obj.stream_raw, cmap_obj.value)
            cmap_obj.decoded_stream = decoded
            cmap_obj.decode_error = error
        if cmap_obj.decoded_stream is None:
            self.font_cmap_cache[cache_key] = {}
            return {}

        cmap = parse_tounicode_cmap(cmap_obj.decoded_stream)
        self.font_cmap_cache[cache_key] = cmap
        return cmap

    def extract_links_for_page(self, page_entry: dict[str, Any]) -> list[dict[str, Any]]:
        page_value = page_entry["value"]
        annots_token = page_value.get("/Annots")
        annots_value = self.resolve(annots_token)
        if not isinstance(annots_value, list):
            return []

        links: list[dict[str, Any]] = []
        for annot_token in annots_value:
            annot_obj = self.get_object(annot_token) if isinstance(annot_token, PDFRef) else None
            annot = self.resolve(annot_token)
            if not isinstance(annot, dict):
                continue
            if not is_name(annot.get("/Subtype"), "/Link"):
                continue

            action = self.resolve(annot.get("/A"))
            uri = None
            if isinstance(action, dict):
                uri_value = action.get("/URI")
                if isinstance(uri_value, PDFString):
                    uri = uri_value.text
                elif isinstance(uri_value, str):
                    uri = uri_value

            links.append(
                {
                    "rect": value_to_serializable(annot.get("/Rect")),
                    "uri": uri,
                    "dest": value_to_serializable(annot.get("/Dest")),
                    "action": value_to_serializable(action),
                    "object_ref": annot_obj.ref.label() if annot_obj else None,
                    "raw_object": decode_text_for_view(annot_obj.raw_bytes) if annot_obj else None,
                }
            )
        return links

    def extract_images_for_page(self, page_entry: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        page_value = page_entry["value"]
        inherited = page_entry["inherited"]
        resources_token = page_value.get("/Resources", inherited.get("/Resources"))
        stream_objects = self.content_stream_objects_for_page(page_entry)
        warnings: list[str] = []
        images: list[dict[str, Any]] = []

        def walk_stream(
            stream_data: bytes,
            local_resources: Any,
            base_ctm: tuple[float, float, float, float, float, float],
            context_chain: list[str],
        ) -> None:
            tokenizer = ContentTokenizer(stream_data)
            operands: list[Any] = []
            local_ctm = base_ctm
            local_stack: list[tuple[float, float, float, float, float, float]] = []

            while True:
                token = tokenizer.next_token()
                if token is None:
                    return

                if isinstance(token, str):
                    if token == "q":
                        local_stack.append(local_ctm)
                    elif token == "Q":
                        if local_stack:
                            local_ctm = local_stack.pop()
                    elif token == "cm":
                        if len(operands) >= 6:
                            matrix = matrix_from_array(operands[-6:])
                            if matrix is not None:
                                local_ctm = matrix_multiply(matrix, local_ctm)
                    elif token == "Do":
                        if operands and isinstance(operands[-1], PDFName):
                            name = operands[-1].value
                            xobject_obj = self.resolve_xobject(local_resources, name)
                            if xobject_obj and isinstance(xobject_obj.value, dict):
                                subtype = xobject_obj.value.get("/Subtype")
                                if is_name(subtype, "/Image"):
                                    images.append(
                                        {
                                            "name": name,
                                            "object_ref": xobject_obj.ref.label(),
                                            "bbox": bbox_from_matrix(local_ctm),
                                            "ctm": [round(value, 6) for value in local_ctm],
                                            "pixel_size": {
                                                "width": xobject_obj.value.get("/Width"),
                                                "height": xobject_obj.value.get("/Height"),
                                            },
                                            "filters": normalize_filter_list(xobject_obj.value.get("/Filter")),
                                            "raw_object": decode_text_for_view(xobject_obj.raw_bytes, limit=12000),
                                            "context_chain": context_chain,
                                        }
                                    )
                                elif is_name(subtype, "/Form") and xobject_obj.decoded_stream is not None:
                                    form_matrix = matrix_from_array(self.resolve(xobject_obj.value.get("/Matrix")))
                                    next_ctm = matrix_multiply(form_matrix, local_ctm) if form_matrix else local_ctm
                                    next_resources = xobject_obj.value.get("/Resources", local_resources)
                                    walk_stream(
                                        xobject_obj.decoded_stream,
                                        next_resources,
                                        next_ctm,
                                        context_chain + [xobject_obj.ref.label()],
                                    )
                    elif token == "BI":
                        tokenizer.skip_inline_image()
                        warnings.append("Encountered inline image data (BI ... ID ... EI); only XObject images are indexed.")

                    operands.clear()
                else:
                    operands.append(token)

        for stream_obj in stream_objects:
            if stream_obj.decoded_stream is None:
                warnings.append(
                    f"Could not decode content stream {stream_obj.ref.label()}: {stream_obj.decode_error or 'unknown error'}"
                )
                continue
            walk_stream(
                stream_obj.decoded_stream,
                resources_token,
                (1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
                [stream_obj.ref.label()],
            )

        return images, warnings

    def parse_xref_stream(self) -> dict[str, Any] | None:
        xref_obj = self.xref_object()
        if xref_obj is None or not isinstance(xref_obj.value, dict) or xref_obj.decoded_stream is None:
            return None
        if not is_name(xref_obj.value.get("/Type"), "/XRef"):
            return None

        widths = self.resolve(xref_obj.value.get("/W"))
        if not isinstance(widths, list) or len(widths) != 3:
            return None
        try:
            widths = [int(item) for item in widths]
        except (TypeError, ValueError):
            return None

        index_value = self.resolve(xref_obj.value.get("/Index"))
        if not isinstance(index_value, list):
            size = xref_obj.value.get("/Size")
            if not isinstance(size, int):
                return None
            index_value = [0, size]

        try:
            pairs = [int(item) for item in index_value]
        except (TypeError, ValueError):
            return None

        rows = []
        cursor = 0
        entry_size = sum(widths)
        for pair_index in range(0, len(pairs), 2):
            start_obj = pairs[pair_index]
            count = pairs[pair_index + 1]
            for obj_num in range(start_obj, start_obj + count):
                entry_bytes = xref_obj.decoded_stream[cursor : cursor + entry_size]
                cursor += entry_size
                if len(entry_bytes) != entry_size:
                    break
                fields = []
                inner = 0
                for width in widths:
                    if width == 0:
                        fields.append(0)
                        continue
                    field_bytes = entry_bytes[inner : inner + width]
                    inner += width
                    fields.append(int.from_bytes(field_bytes, "big"))
                entry_type = fields[0] if widths[0] else 1
                rows.append(
                    {
                        "obj_num": obj_num,
                        "type": entry_type,
                        "field_2": fields[1],
                        "field_3": fields[2],
                    }
                )
        return {
            "object_ref": xref_obj.ref.label(),
            "dictionary": value_to_serializable(xref_obj.value),
            "entry_count": len(rows),
            "sample_entries": rows[:150],
        }


def summarize_page_resources(inspector: PDFInspector, page_entry: dict[str, Any]) -> dict[str, Any]:
    page_value = page_entry["value"]
    inherited = page_entry["inherited"]
    resources_token = page_value.get("/Resources", inherited.get("/Resources"))
    resources = inspector.resolve_resources_dict(resources_token)

    fonts = inspector.resolve(resources.get("/Font"))
    font_names = sorted(fonts.keys()) if isinstance(fonts, dict) else []

    xobjects = inspector.resolve(resources.get("/XObject"))
    xobject_entries = []
    if isinstance(xobjects, dict):
        for name in sorted(xobjects.keys()):
            target = xobjects[name]
            object_ref = target.label() if isinstance(target, PDFRef) else None
            resolved_obj = inspector.get_object(target) if isinstance(target, PDFRef) else None
            subtype = None
            width = None
            height = None
            if resolved_obj and isinstance(resolved_obj.value, dict):
                subtype_value = resolved_obj.value.get("/Subtype")
                if isinstance(subtype_value, PDFName):
                    subtype = subtype_value.value
                width = resolved_obj.value.get("/Width")
                height = resolved_obj.value.get("/Height")
            xobject_entries.append(
                {
                    "name": name,
                    "object_ref": object_ref,
                    "subtype": subtype,
                    "width": width,
                    "height": height,
                }
            )

    return {
        "font_count": len(font_names),
        "fonts": font_names,
        "xobject_count": len(xobject_entries),
        "xobjects": xobject_entries,
    }


def analyze_page_content(
    inspector: PDFInspector,
    page_entry: dict[str, Any],
    content_objects: list[ParsedIndirectObject],
) -> dict[str, Any]:
    page_value = page_entry["value"]
    inherited = page_entry["inherited"]
    resources_token = page_value.get("/Resources", inherited.get("/Resources"))

    operator_counts: Counter[str] = Counter()
    text_samples: list[dict[str, Any]] = []
    text_preview: list[str] = []
    xobject_calls: list[dict[str, Any]] = []
    notes: list[str] = []
    stream_summaries: list[dict[str, Any]] = []

    for stream_obj in content_objects:
        if stream_obj.decoded_stream is None:
            stream_summaries.append(
                {
                    "stream_ref": stream_obj.ref.label(),
                    "decoded": False,
                    "decode_error": stream_obj.decode_error,
                }
            )
            continue

        tokenizer = ContentTokenizer(stream_obj.decoded_stream)
        operands: list[Any] = []
        stream_ops: Counter[str] = Counter()
        current_font = None
        current_font_size = None
        current_cmap: dict[bytes, str] = {}

        while True:
            token = tokenizer.next_token()
            if token is None:
                break

            if isinstance(token, str):
                operator_counts[token] += 1
                stream_ops[token] += 1

                if token == "Tf" and len(operands) >= 2:
                    if isinstance(operands[-2], PDFName) and isinstance(operands[-1], (int, float)):
                        current_font = operands[-2].value
                        current_font_size = float(operands[-1])
                        current_cmap = inspector.resolve_font_cmap(resources_token, current_font)

                elif token in {"Tj", "'", '"'} and operands:
                    candidate = operands[-1]
                    text = ""
                    raw_glyph_text = ""
                    if isinstance(candidate, PDFString):
                        raw_glyph_text = readable_string(candidate)
                        text = decode_with_cmap(candidate.raw, current_cmap) or raw_glyph_text
                    if text:
                        if len(text_samples) < 80:
                            text_samples.append(
                                {
                                    "text": text,
                                    "raw_glyph_text": raw_glyph_text if raw_glyph_text != text else None,
                                    "operator": token,
                                    "font": current_font,
                                    "font_size": round(current_font_size, 3) if current_font_size is not None else None,
                                    "stream_ref": stream_obj.ref.label(),
                                    "decoded_via_tounicode": bool(current_cmap),
                                }
                            )
                        if text not in text_preview and len(text_preview) < 40:
                            text_preview.append(text)

                elif token == "TJ" and operands:
                    text = ""
                    raw_glyph_text = join_text_array(operands[-1])
                    if isinstance(operands[-1], list) and current_cmap:
                        parts: list[str] = []
                        for item in operands[-1]:
                            if isinstance(item, PDFString):
                                parts.append(decode_with_cmap(item.raw, current_cmap))
                        text = sanitize_text("".join(parts))
                    if not text:
                        text = raw_glyph_text
                    if text:
                        if len(text_samples) < 80:
                            text_samples.append(
                                {
                                    "text": text,
                                    "raw_glyph_text": raw_glyph_text if raw_glyph_text != text else None,
                                    "operator": token,
                                    "font": current_font,
                                    "font_size": round(current_font_size, 3) if current_font_size is not None else None,
                                    "stream_ref": stream_obj.ref.label(),
                                    "decoded_via_tounicode": bool(current_cmap),
                                }
                            )
                        if text not in text_preview and len(text_preview) < 40:
                            text_preview.append(text)

                elif token == "Do" and operands and isinstance(operands[-1], PDFName):
                    name = operands[-1].value
                    xobject_obj = inspector.resolve_xobject(resources_token, name)
                    subtype = None
                    object_ref = None
                    if xobject_obj and isinstance(xobject_obj.value, dict):
                        subtype_value = xobject_obj.value.get("/Subtype")
                        if isinstance(subtype_value, PDFName):
                            subtype = subtype_value.value
                        object_ref = xobject_obj.ref.label()
                    xobject_calls.append(
                        {
                            "name": name,
                            "object_ref": object_ref,
                            "subtype": subtype,
                            "stream_ref": stream_obj.ref.label(),
                        }
                    )

                elif token == "BI":
                    notes.append("Content stream contains an inline image block (BI ... ID ... EI).")
                    tokenizer.skip_inline_image()

                operands.clear()
            else:
                operands.append(token)

        stream_summaries.append(
            {
                "stream_ref": stream_obj.ref.label(),
                "decoded": True,
                "decoded_length_bytes": len(stream_obj.decoded_stream),
                "operator_counts": dict(sorted(stream_ops.items())),
            }
        )

    drawing_summary = {
        "text_show_ops": sum(operator_counts.get(name, 0) for name in ("Tj", "TJ", "'", '"')),
        "text_block_ops": operator_counts.get("BT", 0),
        "stroke_ops": sum(operator_counts.get(name, 0) for name in ("S", "s", "B", "B*", "b", "b*")),
        "fill_ops": sum(operator_counts.get(name, 0) for name in ("f", "F", "f*", "B", "B*", "b", "b*")),
        "line_ops": operator_counts.get("l", 0),
        "curve_ops": sum(operator_counts.get(name, 0) for name in ("c", "v", "y")),
        "rectangle_ops": operator_counts.get("re", 0),
        "image_draw_ops": len([item for item in xobject_calls if item.get("subtype") == "/Image"]),
        "form_draw_ops": len([item for item in xobject_calls if item.get("subtype") == "/Form"]),
        "graphics_state_push": operator_counts.get("q", 0),
        "graphics_state_pop": operator_counts.get("Q", 0),
    }

    return {
        "stream_count": len(content_objects),
        "decoded_stream_count": len([item for item in stream_summaries if item.get("decoded")]),
        "operator_counts": dict(sorted(operator_counts.items())),
        "drawing_summary": drawing_summary,
        "text_sample_count": len(text_samples),
        "fonts_with_tounicode": sorted(
            {
                sample["font"]
                for sample in text_samples
                if sample.get("font") and sample.get("decoded_via_tounicode")
            }
        ),
        "text_preview": text_preview,
        "text_samples": text_samples,
        "xobject_calls": xobject_calls,
        "stream_summaries": stream_summaries,
        "notes": notes,
    }


def simplify_link(link: dict[str, Any]) -> dict[str, Any]:
    kind = "unknown"
    target = None

    if link.get("uri"):
        kind = "external_uri"
        target = {"uri": link.get("uri")}
    elif isinstance(link.get("action"), dict) and link["action"].get("/S") == "/GoTo":
        kind = "internal_goto_action"
        target = destination_to_readable(link["action"].get("/D"))
    elif link.get("dest") is not None:
        kind = "internal_destination"
        target = destination_to_readable(link.get("dest"))
    elif link.get("action") is not None:
        kind = "other_action"
        target = action_to_readable(link.get("action"))

    return {
        "kind": kind,
        "object_ref": link.get("object_ref"),
        "rect": rect_to_readable(link.get("rect")),
        "target": target,
        "action": action_to_readable(link.get("action")),
    }


def build_page_explanation(
    page_index: int,
    content_analysis: dict[str, Any],
    images: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> list[str]:
    explanation = [
        f"Page {page_index} is drawn from {content_analysis['decoded_stream_count']} decoded content stream(s)."
    ]

    drawing = content_analysis["drawing_summary"]
    explanation.append(
        "It contains "
        f"{drawing['text_show_ops']} text-show operation(s), "
        f"{drawing['stroke_ops']} stroke operation(s), "
        f"{drawing['fill_ops']} fill operation(s), and "
        f"{drawing['rectangle_ops']} rectangle command(s)."
    )

    if images:
        explanation.append(f"It places {len(images)} image XObject(s) on the page.")
    else:
        explanation.append("It does not place any image XObjects on the page.")

    internal_links = sum(1 for link in links if link["kind"].startswith("internal"))
    external_links = sum(1 for link in links if link["kind"] == "external_uri")
    if links:
        explanation.append(
            f"It contains {len(links)} hyperlink annotation(s): {internal_links} internal jump(s) and {external_links} external URL link(s)."
        )
    else:
        explanation.append("It has no hyperlink annotations.")

    if content_analysis["text_preview"]:
        preview = ", ".join(content_analysis["text_preview"][:6])
        explanation.append(f"Visible text samples include: {preview}.")
        if not content_analysis["fonts_with_tounicode"]:
            explanation.append(
                "These text samples are shown as raw glyph codes because this page's fonts do not expose a ToUnicode map."
            )

    return explanation


def build_readable_page_json(
    inspector: PDFInspector,
    page_entry: dict[str, Any],
    content_objects: list[ParsedIndirectObject],
    links: list[dict[str, Any]],
    images: list[dict[str, Any]],
    image_warnings: list[str],
) -> dict[str, Any]:
    page_value = page_entry["value"]
    inherited = page_entry["inherited"]
    page_obj = page_entry["object"]
    media_box = page_value.get("/MediaBox", inherited.get("/MediaBox"))
    media_box_numbers = numeric_list(media_box)
    page_size = None
    if media_box_numbers and len(media_box_numbers) == 4:
        page_size = {
            "width_pt": round(media_box_numbers[2] - media_box_numbers[0], 3),
            "height_pt": round(media_box_numbers[3] - media_box_numbers[1], 3),
        }

    resources_summary = summarize_page_resources(inspector, page_entry)
    content_analysis = analyze_page_content(inspector, page_entry, content_objects)
    readable_links = [simplify_link(link) for link in links]
    readable_images = [
        {
            "name": image["name"],
            "object_ref": image["object_ref"],
            "bbox": rect_to_readable(image.get("bbox")),
            "placement_matrix": image.get("ctm"),
            "pixel_size": image.get("pixel_size"),
            "filters": image.get("filters"),
            "context_chain": image.get("context_chain"),
        }
        for image in images
    ]

    return {
        "page_number": page_entry["index"],
        "page_object_ref": page_obj.ref.label() if page_obj else None,
        "page_size": page_size,
        "rotate": value_to_serializable(page_value.get("/Rotate", inherited.get("/Rotate"))),
        "content_streams": [obj.ref.label() for obj in content_objects],
        "resources": resources_summary,
        "content_analysis": content_analysis,
        "images": readable_images,
        "links": readable_links,
        "notes": image_warnings + content_analysis["notes"],
        "explanation": build_page_explanation(page_entry["index"], content_analysis, readable_images, readable_links),
    }


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def inspect_pdf(pdf_path: Path, output_root: Path) -> dict[str, Any]:
    inspector = PDFInspector(pdf_path)
    doc_slug = safe_slug(pdf_path.stem)
    doc_root = output_root / doc_slug
    doc_root.mkdir(parents=True, exist_ok=True)

    pages = inspector.iter_pages()
    metadata_obj = inspector.metadata_object()
    info_obj = inspector.info_object()
    xref_obj = inspector.xref_object()

    if metadata_obj is not None:
        write_text(doc_root / "metadata_raw_object.txt", decode_text_for_view(metadata_obj.raw_bytes))
        if metadata_obj.stream_raw is not None:
            write_bytes(doc_root / "streams" / f"{metadata_obj.object_id}_raw.bin", metadata_obj.stream_raw)
        if metadata_obj.decoded_stream is not None:
            suffix = ".xml" if looks_text_like(metadata_obj.decoded_stream) else ".bin"
            target = doc_root / "streams" / f"{metadata_obj.object_id}_decoded{suffix}"
            if suffix == ".xml":
                write_text(target, decode_text_for_view(metadata_obj.decoded_stream))
            else:
                write_bytes(target, metadata_obj.decoded_stream)

    if info_obj is not None:
        write_text(doc_root / "document_info_raw_object.txt", decode_text_for_view(info_obj.raw_bytes))

    if xref_obj is not None:
        write_text(doc_root / "xref_raw_object.txt", decode_text_for_view(xref_obj.raw_bytes, limit=20000))

    page_summaries = []
    readable_pages = []
    for page in pages:
        page_index = page["index"]
        page_slug = f"page_{page_index:04d}"
        page_dir = doc_root / "pages" / page_slug
        page_dir.mkdir(parents=True, exist_ok=True)

        page_obj = page["object"]
        page_value = page["value"]
        inherited = page["inherited"]
        links = inspector.extract_links_for_page(page)
        images, image_warnings = inspector.extract_images_for_page(page)
        content_objects = inspector.content_stream_objects_for_page(page)

        bundle_parts = [f"=== Page {page_index} ===", ""]
        if page_obj is not None:
            bundle_parts.append(f"Page object: {page_obj.ref.label()} ({page_obj.source})")
            bundle_parts.append(page_obj.raw_bytes.decode("latin-1", errors="replace"))
            bundle_parts.append("")

        bundle_parts.append("-- Inherited / direct page dictionary view --")
        bundle_parts.append(pdf_value_to_source(page_value))
        bundle_parts.append("")
        if inherited:
            bundle_parts.append("-- Inherited values from parent /Pages nodes --")
            bundle_parts.append(json.dumps(value_to_serializable(inherited), ensure_ascii=False, indent=2))
            bundle_parts.append("")

        bundle_parts.append("-- Content streams --")
        if not content_objects:
            bundle_parts.append("No /Contents found.")
        for index, content_obj in enumerate(content_objects, start=1):
            bundle_parts.append(f"[{index}] {content_obj.ref.label()} raw object")
            bundle_parts.append(decode_text_for_view(content_obj.raw_bytes, limit=14000))
            bundle_parts.append("")
            if content_obj.stream_raw is not None:
                write_bytes(page_dir / "streams" / f"{content_obj.object_id}_raw.bin", content_obj.stream_raw)
            if content_obj.decoded_stream is not None:
                write_text(
                    page_dir / "streams" / f"{content_obj.object_id}_decoded.txt",
                    decode_text_for_view(content_obj.decoded_stream),
                )
                bundle_parts.append(f"Decoded content saved to: streams/{content_obj.object_id}_decoded.txt")
                bundle_parts.append("")

        bundle_parts.append("-- Hyperlinks --")
        if not links:
            bundle_parts.append("No /Link annotations detected.")
        for index, link in enumerate(links, start=1):
            bundle_parts.append(f"[{index}] rect={link['rect']} uri={link['uri']} dest={link['dest']}")
            if link["raw_object"]:
                bundle_parts.append(link["raw_object"])
            bundle_parts.append("")

        bundle_parts.append("-- Images --")
        if not images:
            bundle_parts.append("No XObject images detected in decoded content streams.")
        for index, image in enumerate(images, start=1):
            bundle_parts.append(
                f"[{index}] {image['name']} -> {image['object_ref']} bbox={image['bbox']} pixels={image['pixel_size']}"
            )
            bundle_parts.append(f"filters={image['filters']} context={image['context_chain']}")
            bundle_parts.append(image["raw_object"])
            bundle_parts.append("")
            image_ref = image["object_ref"]
            if image_ref:
                obj_num, gen_num, _ = image_ref.split(" ")
                image_obj = inspector.get_object(PDFRef(int(obj_num), int(gen_num)))
                if image_obj and image_obj.stream_raw is not None:
                    write_bytes(page_dir / "images" / f"{image_obj.object_id}_raw.bin", image_obj.stream_raw)
                    if image_obj.decoded_stream is not None and looks_text_like(image_obj.decoded_stream):
                        write_text(
                            page_dir / "images" / f"{image_obj.object_id}_decoded.txt",
                            decode_text_for_view(image_obj.decoded_stream),
                        )

        if image_warnings:
            bundle_parts.append("-- Notes --")
            bundle_parts.extend(image_warnings)

        write_text(page_dir / "bundle.txt", "\n".join(bundle_parts))

        page_summary = {
            "page_index": page_index,
            "page_object_ref": page_obj.ref.label() if page_obj else None,
            "media_box": value_to_serializable(page_value.get("/MediaBox", inherited.get("/MediaBox"))),
            "rotate": value_to_serializable(page_value.get("/Rotate", inherited.get("/Rotate"))),
            "content_stream_refs": [obj.ref.label() for obj in content_objects],
            "links": links,
            "images": images,
            "notes": image_warnings,
        }
        page_summaries.append(page_summary)
        write_text(page_dir / "summary.json", json.dumps(page_summary, ensure_ascii=False, indent=2))

        readable_page = build_readable_page_json(
            inspector=inspector,
            page_entry=page,
            content_objects=content_objects,
            links=links,
            images=images,
            image_warnings=image_warnings,
        )
        readable_pages.append(readable_page)
        write_text(page_dir / "readable.json", json.dumps(readable_page, ensure_ascii=False, indent=2))

    all_objects_index = []
    for parsed in sorted(inspector.objects.values(), key=lambda item: (item.obj_num, item.gen_num)):
        stream_dict = parsed.value if isinstance(parsed.value, dict) else None
        entry = {
            "object_ref": parsed.ref.label(),
            "source": parsed.source,
            "container": parsed.container,
            "offset": parsed.offset,
            "type": parsed.value.get("/Type").value if isinstance(parsed.value, dict) and isinstance(parsed.value.get("/Type"), PDFName) else None,
            "subtype": parsed.value.get("/Subtype").value if isinstance(parsed.value, dict) and isinstance(parsed.value.get("/Subtype"), PDFName) else None,
            "has_stream": parsed.stream_raw is not None,
            "filters": normalize_filter_list(stream_dict.get("/Filter")) if stream_dict else [],
        }
        all_objects_index.append(entry)

    summary = {
        "pdf_path": str(pdf_path),
        "header": inspector.header,
        "file_size_bytes": pdf_path.stat().st_size,
        "startxref": inspector.startxref,
        "top_level_object_count": len(inspector.top_level_objects),
        "resolved_object_count": len(inspector.objects),
        "catalog_ref": inspector.catalog_object().ref.label() if inspector.catalog_object() else None,
        "info_ref": info_obj.ref.label() if info_obj else None,
        "metadata_ref": metadata_obj.ref.label() if metadata_obj else None,
        "xref_ref": xref_obj.ref.label() if xref_obj else None,
        "page_count": len(page_summaries),
        "pages": page_summaries,
        "xref_stream": inspector.parse_xref_stream(),
    }

    write_text(doc_root / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
    write_text(doc_root / "pages_readable.json", json.dumps(readable_pages, ensure_ascii=False, indent=2))
    write_text(doc_root / "all_objects_index.json", json.dumps(all_objects_index, ensure_ascii=False, indent=2))

    overview_lines = [
        f"# {pdf_path.name}",
        "",
        f"- Header: `{inspector.header}`",
        f"- File size: `{pdf_path.stat().st_size}` bytes",
        f"- startxref: `{inspector.startxref}`",
        f"- Top-level objects: `{len(inspector.top_level_objects)}`",
        f"- Total resolved objects (including object streams): `{len(inspector.objects)}`",
        f"- Page count: `{len(page_summaries)}`",
        "",
        "## Where To Look",
        "",
        "- `summary.json`: machine-readable full summary",
        "- `pages_readable.json`: page-by-page readable JSON for understanding what each page is doing",
        "- `all_objects_index.json`: every resolved object with type/filter/source",
        "- `xref_raw_object.txt`: raw cross-reference stream object",
        "- `metadata_raw_object.txt`: raw metadata object",
        "- `pages/page_xxxx/bundle.txt`: the easiest place to read raw page data, content streams, links, and image objects together",
        "- `pages/page_xxxx/readable.json`: a human-readable per-page JSON explanation",
        "",
        "## PDF Storage Notes",
        "",
        "- This file stores data as PDF indirect objects (`n n obj ... endobj`).",
        "- The trailer is carried by an `/XRef` stream object instead of a classic plain-text `xref` table.",
        "- Some logical objects may be packed into `/ObjStm` object streams, so the in-file raw form can be a compressed container rather than one visible top-level object per page element.",
        "- Page drawing commands live in content streams; image placement is usually a `/Name Do` operator after a `cm` matrix that positions/scales the image.",
        "- Hyperlinks usually live in `/Annots` entries with `/Subtype /Link`, plus `/A << /S /URI ... >>` or `/Dest`.",
    ]
    write_text(doc_root / "overview.md", "\n".join(overview_lines))
    return summary


def build_root_index(output_root: Path, summaries: list[dict[str, Any]]) -> None:
    write_text(output_root / "index.json", json.dumps(summaries, ensure_ascii=False, indent=2))
    lines = [
        "# Eplan PDF Inspection",
        "",
        "This folder is generated by `scripts/inspect_eplan_pdfs.py`.",
        "",
    ]
    for summary in summaries:
        slug = safe_slug(Path(summary["pdf_path"]).stem)
        pdf_name = Path(summary["pdf_path"]).name
        lines.extend(
            [
                f"## {pdf_name}",
                "",
                f"- Header: `{summary['header']}`",
                f"- startxref: `{summary['startxref']}`",
                f"- Pages: `{summary['page_count']}`",
                f"- Resolved objects: `{summary['resolved_object_count']}`",
                f"- Readable pages: `{slug}/pages_readable.json`",
                f"- Overview: `{slug}/overview.md`",
                f"- Full summary: `{slug}/summary.json`",
                "",
            ]
        )
    write_text(output_root / "README.md", "\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect raw PDF storage, page streams, link annotations, and image placements."
    )
    parser.add_argument(
        "pdfs",
        nargs="*",
        help="PDF files to inspect. Defaults to every *.pdf under data/eplans.",
    )
    parser.add_argument(
        "--input-dir",
        default="data/eplans",
        help="Directory scanned when no PDF paths are provided.",
    )
    parser.add_argument(
        "--output-dir",
        default="output/pdf_inspection",
        help="Directory where inspection files will be written.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.pdfs:
        pdf_paths = [Path(item) for item in args.pdfs]
    else:
        pdf_paths = sorted(Path(args.input_dir).glob("*.pdf"))

    if not pdf_paths:
        raise SystemExit("No PDF files found to inspect.")

    output_root = Path(args.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    summaries = [inspect_pdf(pdf_path, output_root) for pdf_path in pdf_paths]
    build_root_index(output_root, summaries)

    print(f"Inspected {len(pdf_paths)} PDF file(s).")
    for summary in summaries:
        print(
            f"- {Path(summary['pdf_path']).name}: pages={summary['page_count']}, "
            f"objects={summary['resolved_object_count']}, startxref={summary['startxref']}"
        )
    print(f"Outputs written to: {output_root.resolve()}")


if __name__ == "__main__":
    main()
