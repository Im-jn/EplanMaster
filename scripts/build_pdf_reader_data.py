from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from inspect_eplan_pdfs import (  # type: ignore[import-not-found]
    PDFInspector,
    PDFName,
    PDFRef,
    PDFString,
    PDFValueParser,
    ParsedIndirectObject,
    decode_with_cmap,
    decode_text_for_view,
    is_name,
    join_text_array,
    looks_text_like,
    matrix_from_array,
    matrix_multiply,
    numeric_list,
    normalize_filter_list,
    rect_to_readable,
    readable_string,
    safe_slug,
    sanitize_text,
    simplify_link,
)


Matrix = tuple[float, float, float, float, float, float]
IDENTITY_MATRIX: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
STYLE_OPERATORS = {
    "q",
    "Q",
    "cm",
    "w",
    "J",
    "j",
    "d",
    "M",
    "RG",
    "rg",
    "G",
    "g",
    "K",
    "k",
    "CS",
    "cs",
    "SC",
    "SCN",
    "sc",
    "scn",
    "gs",
}
PAINT_OPERATORS = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
TEXT_STATE_OPERATORS = {"BT", "ET", "Tf", "Tm", "Td", "TD", "T*", "Tc", "Tw", "Tz", "TL", "Ts"}


def round_float(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


def transform_point(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return (a * x + c * y + e, b * x + d * y + f)


def effective_scale(matrix: Matrix) -> float:
    a, b, c, d, _, _ = matrix
    x_scale = (a * a + b * b) ** 0.5
    y_scale = (c * c + d * d) ** 0.5
    base = max((x_scale + y_scale) / 2.0, 0.001)
    return base


def build_bbox(points: list[tuple[float, float]], margin: float = 0.0) -> dict[str, float] | None:
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    x0 = min(xs) - margin
    y0 = min(ys) - margin
    x1 = max(xs) + margin
    y1 = max(ys) + margin
    return {
        "x0": round_float(x0),
        "y0": round_float(y0),
        "x1": round_float(x1),
        "y1": round_float(y1),
        "width": round_float(x1 - x0),
        "height": round_float(y1 - y0),
    }


def bbox_key(bbox: dict[str, float]) -> tuple[float, float, float, float]:
    return (
        round_float(bbox["x0"]),
        round_float(bbox["y0"]),
        round_float(bbox["x1"]),
        round_float(bbox["y1"]),
    )


def expand_bbox(bbox: dict[str, float], margin: float) -> dict[str, float]:
    x0 = bbox["x0"] - margin
    y0 = bbox["y0"] - margin
    x1 = bbox["x1"] + margin
    y1 = bbox["y1"] + margin
    return {
        "x0": round_float(x0),
        "y0": round_float(y0),
        "x1": round_float(x1),
        "y1": round_float(y1),
        "width": round_float(x1 - x0),
        "height": round_float(y1 - y0),
    }


def bbox_contains_point(bbox: dict[str, float], x: float, y: float) -> bool:
    return bbox["x0"] <= x <= bbox["x1"] and bbox["y0"] <= y <= bbox["y1"]


def bbox_intersects(left: dict[str, float], right: dict[str, float]) -> bool:
    return not (
        left["x1"] < right["x0"]
        or left["x0"] > right["x1"]
        or left["y1"] < right["y0"]
        or left["y0"] > right["y1"]
    )


def slice_snippet(text: str, start: int, end: int, context_before: int = 160, context_after: int = 120) -> dict[str, Any]:
    safe_start = max(0, start - context_before)
    safe_end = min(len(text), end + context_after)
    snippet = text[safe_start:safe_end]
    return {
        "snippet": snippet,
        "highlight_start": start - safe_start,
        "highlight_end": end - safe_start,
    }


def ref_from_label(label: str | None) -> PDFRef | None:
    if not label:
        return None
    parts = label.split()
    if len(parts) < 3 or parts[2] != "R":
        return None
    try:
        return PDFRef(int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def pdf_name_text(value: Any) -> str | None:
    return value.value if isinstance(value, PDFName) else None


def object_kind_label(obj: ParsedIndirectObject) -> str:
    if isinstance(obj.value, dict):
        type_name = pdf_name_text(obj.value.get("/Type"))
        subtype_name = pdf_name_text(obj.value.get("/Subtype"))
        action_name = pdf_name_text(obj.value.get("/S"))

        if type_name == "/Page":
            return "Page object"
        if type_name == "/Annot" and subtype_name == "/Link":
            return "Link annotation"
        if type_name == "/XObject" and subtype_name == "/Form":
            return "Form XObject"
        if type_name == "/XObject" and subtype_name == "/Image":
            return "Image XObject"
        if type_name == "/Font":
            return "Font object"
        if action_name:
            return f"Action object ({action_name})"
        if type_name:
            return f"{type_name.removeprefix('/')} object"
        if subtype_name:
            return f"{subtype_name.removeprefix('/')} object"

    if obj.stream_raw is not None:
        return "Stream object"
    return "PDF object"


def object_description(obj: ParsedIndirectObject) -> str:
    if not isinstance(obj.value, dict):
        if obj.stream_raw is not None:
            return "Referenced stream object. Inspect the raw source below to understand how this PDF stores the data."
        return "Referenced indirect object. Inspect the raw source below to understand its role in the PDF."

    type_name = pdf_name_text(obj.value.get("/Type"))
    subtype_name = pdf_name_text(obj.value.get("/Subtype"))
    action_name = pdf_name_text(obj.value.get("/S"))

    if type_name == "/Page":
        return "Owns page-level dictionaries such as /MediaBox, /Resources, /Contents, and /Annots."
    if type_name == "/Annot" and subtype_name == "/Link":
        return "Defines the clickable rectangle, border behavior, and the action or destination triggered by the selected hyperlink."
    if action_name == "/GoTo":
        return "Defines an internal jump action. Its /D entry points to the destination page or named destination."
    if action_name == "/URI":
        return "Defines an external hyperlink action. Its /URI entry stores the target URL."
    if action_name:
        return f"Defines what happens when the annotation is activated. The /S entry is {action_name}."
    if type_name == "/XObject" and subtype_name == "/Form":
        return "Reusable Form XObject stream that nests additional drawing instructions."
    if type_name == "/XObject" and subtype_name == "/Image":
        return "Image XObject referenced from page content streams."
    if type_name == "/Font":
        return "Font resource used by text-showing operators. Width data and /ToUnicode mappings help decode and size text."
    if obj.stream_raw is not None:
        return "Stream object that stores compressed PDF instructions or other stream data."
    return "Referenced indirect object used by the selected PDF element."


def ensure_object_detail(
    inspector: PDFInspector,
    object_details: dict[str, dict[str, Any]],
    ref: PDFRef,
) -> str:
    key = ref.label()
    if key in object_details:
        return key

    obj = inspector.get_object(ref)
    if obj is None:
        object_details[key] = {
            "object_ref": key,
            "kind_label": "Referenced object",
            "description": "This reference could not be resolved from the current PDF object table.",
            "raw_source": None,
            "decoded_stream_preview": None,
        }
        return key

    decoded_stream_preview = None
    if obj.decoded_stream is not None and looks_text_like(obj.decoded_stream):
        decoded_stream_preview = decode_text_for_view(obj.decoded_stream, limit=2500)

    object_details[key] = {
        "object_ref": key,
        "kind_label": object_kind_label(obj),
        "description": object_description(obj),
        "raw_source": decode_text_for_view(obj.raw_bytes, limit=6000),
        "decoded_stream_preview": decoded_stream_preview,
    }
    return key


def append_reference(
    inspector: PDFInspector,
    object_details: dict[str, dict[str, Any]],
    reference_chain: list[dict[str, str]],
    ref: PDFRef | None,
    role: str,
) -> None:
    if ref is None:
        return

    object_ref = ensure_object_detail(inspector, object_details, ref)
    if any(entry["object_ref"] == object_ref and entry["role"] == role for entry in reference_chain):
        return
    reference_chain.append({"object_ref": object_ref, "role": role})


def destination_page_ref(value: Any) -> PDFRef | None:
    if isinstance(value, list) and value and isinstance(value[0], PDFRef):
        return value[0]
    return None


def attach_vector_reference_context(
    inspector: PDFInspector,
    page_entry: dict[str, Any],
    stream_obj: ParsedIndirectObject,
    context_chain: list[str],
    object_details: dict[str, dict[str, Any]],
    item: dict[str, Any],
) -> None:
    reference_chain: list[dict[str, str]] = []
    append_reference(inspector, object_details, reference_chain, stream_obj.ref, "Selected content stream")

    for index, ref_label in enumerate(context_chain[1:], start=1):
        role = "Parent Form XObject" if index == 1 else f"Nested Form XObject {index}"
        append_reference(inspector, object_details, reference_chain, ref_from_label(ref_label), role)

    page_obj = page_entry.get("object")
    if page_obj is not None:
        append_reference(inspector, object_details, reference_chain, page_obj.ref, "Owning page object")

    item["source_comment"] = (
        "This excerpt comes from the decoded content stream that draws the selected vector path. "
        "Path operators build the geometry and the final paint operator renders it."
    )
    item["reference_chain"] = reference_chain


def attach_link_reference_context(
    inspector: PDFInspector,
    page_entry: dict[str, Any],
    annotation_obj: ParsedIndirectObject | None,
    object_details: dict[str, dict[str, Any]],
    item: dict[str, Any],
) -> None:
    reference_chain: list[dict[str, str]] = []

    if annotation_obj is not None:
        append_reference(inspector, object_details, reference_chain, annotation_obj.ref, "Selected annotation object")

        if isinstance(annotation_obj.value, dict):
            page_ref = annotation_obj.value.get("/P")
            if isinstance(page_ref, PDFRef):
                append_reference(inspector, object_details, reference_chain, page_ref, "Owning page (/P)")
            elif page_entry.get("object") is not None:
                append_reference(inspector, object_details, reference_chain, page_entry["object"].ref, "Current page object")

            action_token = annotation_obj.value.get("/A")
            if isinstance(action_token, PDFRef):
                append_reference(inspector, object_details, reference_chain, action_token, "Action object (/A)")
                action_obj = inspector.get_object(action_token)
                if action_obj is not None and isinstance(action_obj.value, dict):
                    append_reference(
                        inspector,
                        object_details,
                        reference_chain,
                        destination_page_ref(action_obj.value.get("/D")),
                        "Destination page from action /D",
                    )
                    next_action = action_obj.value.get("/Next")
                    if isinstance(next_action, PDFRef):
                        append_reference(inspector, object_details, reference_chain, next_action, "Next action (/Next)")

            append_reference(
                inspector,
                object_details,
                reference_chain,
                destination_page_ref(annotation_obj.value.get("/Dest")),
                "Destination page from /Dest",
            )
    elif page_entry.get("object") is not None:
        append_reference(inspector, object_details, reference_chain, page_entry["object"].ref, "Current page object")

    item["source_comment"] = (
        "This excerpt is the raw link annotation dictionary. "
        "/Rect defines the clickable area, /P points to the owning page, and /A or /Dest determines the navigation target."
    )
    item["reference_chain"] = reference_chain


@dataclass
class PositionedToken:
    value: Any
    start: int
    end: int


class PositionedContentTokenizer:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.parser = PDFValueParser(data)

    def skip_inline_image(self) -> None:
        id_match = self.data.find(b" ID", self.parser.pos)
        if id_match < 0:
            id_match = self.data.find(b"\nID", self.parser.pos)
        if id_match < 0:
            self.parser.pos = len(self.data)
            return

        self.parser.pos = id_match + 3
        end_marker = self.data.find(b" EI", self.parser.pos)
        if end_marker < 0:
            end_marker = self.data.find(b"\nEI", self.parser.pos)
        if end_marker < 0:
            self.parser.pos = len(self.data)
            return
        self.parser.pos = end_marker + 3

    def next_token(self) -> PositionedToken | None:
        self.parser.skip_ws_and_comments()
        if self.parser.pos >= len(self.data):
            return None

        start = self.parser.pos
        current = self.data[self.parser.pos : self.parser.pos + 1]
        if current in {b"/", b"[", b"(", b"<"} or current in b"+-.0123456789":
            value = self.parser.parse_value()
        else:
            value = self.parser.parse_value(allow_operator=True)
        return PositionedToken(value=value, start=start, end=self.parser.pos)


@dataclass
class GraphicsState:
    ctm: Matrix = IDENTITY_MATRIX
    line_width: float = 1.0

    def clone(self) -> "GraphicsState":
        return GraphicsState(ctm=self.ctm, line_width=self.line_width)


@dataclass
class PathBuilder:
    commands: list[dict[str, Any]] = field(default_factory=list)
    points: list[tuple[float, float]] = field(default_factory=list)
    current_point: tuple[float, float] | None = None
    subpath_start: tuple[float, float] | None = None
    source_start: int | None = None
    context_start: int | None = None

    def begin(self, source_start: int, context_start: int) -> None:
        if self.source_start is None:
            self.source_start = source_start
        if self.context_start is None:
            self.context_start = min(context_start, source_start)

    def move_to(self, point: tuple[float, float]) -> None:
        self.commands.append(
            {
                "op": "M",
                "points": [[round_float(point[0]), round_float(point[1])]],
            }
        )
        self.points.append(point)
        self.current_point = point
        self.subpath_start = point

    def line_to(self, point: tuple[float, float]) -> None:
        if self.current_point is None:
            self.move_to(point)
            return
        self.commands.append(
            {
                "op": "L",
                "points": [[round_float(point[0]), round_float(point[1])]],
            }
        )
        self.points.append(point)
        self.current_point = point

    def curve_to(
        self,
        control_1: tuple[float, float],
        control_2: tuple[float, float],
        end_point: tuple[float, float],
    ) -> None:
        if self.current_point is None:
            self.move_to(end_point)
            return
        self.commands.append(
            {
                "op": "C",
                "points": [
                    [round_float(control_1[0]), round_float(control_1[1])],
                    [round_float(control_2[0]), round_float(control_2[1])],
                    [round_float(end_point[0]), round_float(end_point[1])],
                ],
            }
        )
        self.points.extend([control_1, control_2, end_point])
        self.current_point = end_point

    def close_path(self) -> None:
        if self.current_point is None or self.subpath_start is None:
            return
        self.commands.append({"op": "Z"})
        self.current_point = self.subpath_start

    def rectangle(self, matrix: Matrix, x: float, y: float, width: float, height: float) -> None:
        p0 = transform_point(matrix, x, y)
        p1 = transform_point(matrix, x + width, y)
        p2 = transform_point(matrix, x + width, y + height)
        p3 = transform_point(matrix, x, y + height)
        self.move_to(p0)
        self.line_to(p1)
        self.line_to(p2)
        self.line_to(p3)
        self.close_path()

    def has_geometry(self) -> bool:
        return bool(self.commands and self.points)

    def reset(self) -> None:
        self.commands.clear()
        self.points.clear()
        self.current_point = None
        self.subpath_start = None
        self.source_start = None
        self.context_start = None


def operand_start(operands: list[PositionedToken], count: int, fallback: int) -> int:
    if count <= 0 or len(operands) < count:
        return fallback
    return min(token.start for token in operands[-count:])


def operand_numbers(operands: list[PositionedToken], count: int) -> list[float] | None:
    if len(operands) < count:
        return None
    values = [token.value for token in operands[-count:]]
    if not all(isinstance(item, (int, float)) for item in values):
        return None
    return [float(item) for item in values]


def translation_matrix(tx: float, ty: float) -> Matrix:
    return (1.0, 0.0, 0.0, 1.0, float(tx), float(ty))


def transformed_rect_bbox(matrix: Matrix, x0: float, y0: float, x1: float, y1: float) -> dict[str, float] | None:
    return build_bbox(
        [
            transform_point(matrix, x0, y0),
            transform_point(matrix, x1, y0),
            transform_point(matrix, x1, y1),
            transform_point(matrix, x0, y1),
        ]
    )


@dataclass
class FontMetrics:
    object_ref: PDFRef | None = None
    to_unicode_ref: PDFRef | None = None
    code_size: int = 1
    default_width: float = 500.0
    widths: dict[int, float] = field(default_factory=dict)
    average_width: float = 500.0

    def width_for_code(self, code: int) -> float:
        return self.widths.get(code, self.default_width or self.average_width or 500.0)


@dataclass
class TextState:
    text_matrix: Matrix = IDENTITY_MATRIX
    line_matrix: Matrix = IDENTITY_MATRIX
    font_name: str | None = None
    font_size: float = 12.0
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    horizontal_scale: float = 1.0
    leading: float = 0.0
    rise: float = 0.0
    cmap: dict[bytes, str] = field(default_factory=dict)
    font_metrics: FontMetrics = field(default_factory=FontMetrics)
    in_text_object: bool = False

    def clone(self) -> "TextState":
        return TextState(
            text_matrix=self.text_matrix,
            line_matrix=self.line_matrix,
            font_name=self.font_name,
            font_size=self.font_size,
            char_spacing=self.char_spacing,
            word_spacing=self.word_spacing,
            horizontal_scale=self.horizontal_scale,
            leading=self.leading,
            rise=self.rise,
            cmap=dict(self.cmap),
            font_metrics=self.font_metrics,
            in_text_object=self.in_text_object,
        )


def parse_cid_widths(value: Any) -> dict[int, float]:
    if not isinstance(value, list):
        return {}

    widths: dict[int, float] = {}
    index = 0
    while index < len(value):
        start_code = value[index]
        if not isinstance(start_code, (int, float)):
            index += 1
            continue
        start = int(start_code)
        index += 1
        if index >= len(value):
            break

        width_spec = value[index]
        if isinstance(width_spec, list):
            for offset, item in enumerate(width_spec):
                if isinstance(item, (int, float)):
                    widths[start + offset] = float(item)
            index += 1
            continue

        if isinstance(width_spec, (int, float)) and index + 1 < len(value) and isinstance(value[index + 1], (int, float)):
            end = int(width_spec)
            shared_width = float(value[index + 1])
            for code in range(start, end + 1):
                widths[code] = shared_width
            index += 2
            continue

        index += 1

    return widths


def resolve_font_metrics(
    inspector: PDFInspector,
    resources_token: Any,
    font_name: str,
    cache: dict[str, FontMetrics],
) -> FontMetrics:
    font_obj = inspector.resolve_font(resources_token, font_name)
    cache_key = font_obj.ref.label() if font_obj is not None else f"missing:{font_name}"
    if cache_key in cache:
        return cache[cache_key]

    metrics = FontMetrics()
    if font_obj is None or not isinstance(font_obj.value, dict):
        cache[cache_key] = metrics
        return metrics

    metrics.object_ref = font_obj.ref
    to_unicode = font_obj.value.get("/ToUnicode")
    if isinstance(to_unicode, PDFRef):
        metrics.to_unicode_ref = to_unicode

    first_char = font_obj.value.get("/FirstChar")
    widths_value = inspector.resolve(font_obj.value.get("/Widths"))
    if isinstance(first_char, int) and isinstance(widths_value, list):
        for offset, item in enumerate(widths_value):
            if isinstance(item, (int, float)):
                metrics.widths[first_char + offset] = float(item)

    font_descriptor = inspector.resolve(font_obj.value.get("/FontDescriptor"))
    if isinstance(font_descriptor, dict):
        missing_width = font_descriptor.get("/MissingWidth")
        if isinstance(missing_width, (int, float)):
            metrics.default_width = float(missing_width)

    subtype_name = pdf_name_text(font_obj.value.get("/Subtype"))
    if subtype_name == "/Type0":
        metrics.code_size = 2
        descendants = inspector.resolve(font_obj.value.get("/DescendantFonts"))
        if isinstance(descendants, list) and descendants:
            descendant_ref = descendants[0]
            descendant_obj = inspector.get_object(descendant_ref) if isinstance(descendant_ref, PDFRef) else None
            if descendant_obj is not None and isinstance(descendant_obj.value, dict):
                descendant_value = descendant_obj.value
                default_width = descendant_value.get("/DW")
                if isinstance(default_width, (int, float)):
                    metrics.default_width = float(default_width)
                metrics.widths.update(parse_cid_widths(inspector.resolve(descendant_value.get("/W"))))

                descendant_descriptor = inspector.resolve(descendant_value.get("/FontDescriptor"))
                if isinstance(descendant_descriptor, dict):
                    missing_width = descendant_descriptor.get("/MissingWidth")
                    if isinstance(missing_width, (int, float)):
                        metrics.default_width = float(missing_width)

    cmap = inspector.resolve_font_cmap(resources_token, font_name)
    if cmap:
        metrics.code_size = max(min((len(key) for key in cmap if key), default=metrics.code_size), 1)

    if metrics.widths:
        metrics.average_width = sum(metrics.widths.values()) / len(metrics.widths)
    else:
        metrics.average_width = metrics.default_width or 500.0

    cache[cache_key] = metrics
    return metrics


def split_glyph_codes(raw: bytes, cmap: dict[bytes, str], code_size: int) -> list[bytes]:
    if not raw:
        return []

    if cmap:
        lengths = sorted({len(key) for key in cmap if key}, reverse=True)
        fallback = max(min(lengths, default=code_size), 1)
        parts: list[bytes] = []
        pos = 0
        while pos < len(raw):
            matched = False
            for length in lengths:
                chunk = raw[pos : pos + length]
                if chunk in cmap:
                    parts.append(chunk)
                    pos += length
                    matched = True
                    break
            if matched:
                continue
            chunk = raw[pos : pos + fallback]
            if not chunk:
                break
            parts.append(chunk)
            pos += len(chunk)
        return parts

    step = max(code_size, 1)
    return [raw[index : index + step] for index in range(0, len(raw), step) if raw[index : index + step]]


def decode_text_operand(operand: Any, cmap: dict[bytes, str]) -> tuple[str, str]:
    if isinstance(operand, PDFString):
        raw_glyph_text = readable_string(operand)
        return (decode_with_cmap(operand.raw, cmap) or raw_glyph_text, raw_glyph_text)

    if isinstance(operand, list):
        decoded_parts: list[str] = []
        raw_parts: list[str] = []
        for item in operand:
            if not isinstance(item, PDFString):
                continue
            raw_text = readable_string(item)
            raw_parts.append(raw_text)
            decoded_parts.append(decode_with_cmap(item.raw, cmap) or raw_text)
        decoded_text = sanitize_text("".join(decoded_parts))
        raw_glyph_text = join_text_array(operand)
        return (decoded_text or raw_glyph_text, raw_glyph_text)

    return ("", "")


def advance_for_pdf_string(raw: bytes, decoded_text: str, text_state: TextState) -> float:
    glyph_codes = split_glyph_codes(raw, text_state.cmap, text_state.font_metrics.code_size)
    glyph_count = len(glyph_codes) or max(len(decoded_text), 1)
    width_units = 0.0
    for code in glyph_codes:
        width_units += text_state.font_metrics.width_for_code(int.from_bytes(code, "big"))
    if width_units <= 0 and glyph_count:
        width_units = text_state.font_metrics.average_width * glyph_count

    space_count = decoded_text.count(" ")
    base_advance = (width_units / 1000.0) * text_state.font_size
    spacing_advance = text_state.char_spacing * glyph_count + text_state.word_spacing * space_count
    return (base_advance + spacing_advance) * text_state.horizontal_scale


def advance_for_text_operand(operand: Any, text_state: TextState) -> float:
    if isinstance(operand, PDFString):
        decoded_text = decode_with_cmap(operand.raw, text_state.cmap) or readable_string(operand)
        return advance_for_pdf_string(operand.raw, decoded_text, text_state)

    if isinstance(operand, list):
        total = 0.0
        for item in operand:
            if isinstance(item, PDFString):
                decoded_text = decode_with_cmap(item.raw, text_state.cmap) or readable_string(item)
                total += advance_for_pdf_string(item.raw, decoded_text, text_state)
            elif isinstance(item, (int, float)):
                total -= (float(item) / 1000.0) * text_state.font_size * text_state.horizontal_scale
        return total

    return 0.0


def move_text_position(text_state: TextState, tx: float, ty: float) -> None:
    translated = matrix_multiply(text_state.line_matrix, translation_matrix(tx, ty))
    text_state.line_matrix = translated
    text_state.text_matrix = translated


def update_text_matrix_after_showing(text_state: TextState, advance: float) -> None:
    text_state.text_matrix = matrix_multiply(text_state.text_matrix, translation_matrix(advance, 0.0))


def finalize_text_item(
    *,
    item_id: str,
    page_number: int,
    operator: str,
    text_value: str,
    raw_glyph_text: str,
    operand: Any,
    text_state: TextState,
    graphics_state: GraphicsState,
    stream_obj: ParsedIndirectObject,
    context_chain: list[str],
    stream_text: str,
    start_offset: int,
    end_offset: int,
    context_start: int,
) -> dict[str, Any] | None:
    clean_text = sanitize_text(text_value)
    if not clean_text:
        return None

    advance = advance_for_text_operand(operand, text_state)
    width = max(abs(advance), text_state.font_size * 0.35, 1.0)
    x0 = 0.0
    x1 = width
    ascent = max(text_state.font_size * 0.85, 1.0)
    descent = max(text_state.font_size * 0.2, 0.8)
    combined_matrix = matrix_multiply(graphics_state.ctm, text_state.text_matrix)
    bbox = transformed_rect_bbox(
        combined_matrix,
        x0,
        text_state.rise - descent,
        x1,
        text_state.rise + ascent,
    )
    if bbox is None:
        return None

    snippet_start = start_offset
    if (start_offset - context_start) <= 120:
        snippet_start = context_start

    source_slice = slice_snippet(
        text=stream_text,
        start=snippet_start,
        end=end_offset,
        context_before=0,
        context_after=120,
    )

    return {
        "id": item_id,
        "kind": "text",
        "page_number": page_number,
        "bbox": bbox,
        "text": {
            "content": clean_text,
            "raw_glyph_text": raw_glyph_text if raw_glyph_text and raw_glyph_text != clean_text else None,
            "operator": operator,
            "font": text_state.font_name,
            "font_size": round_float(text_state.font_size),
            "decoded_via_tounicode": bool(text_state.cmap),
        },
        "source": {
            "object_ref": stream_obj.ref.label(),
            "context_chain": context_chain,
            "snippet": source_slice["snippet"],
            "highlight_start": source_slice["highlight_start"],
            "highlight_end": source_slice["highlight_end"],
        },
        "summary": {
            "char_count": len(clean_text),
        },
    }


def finalize_image_item(
    *,
    item_id: str,
    page_number: int,
    xobject_name: str,
    ctm: Matrix,
    stream_obj: ParsedIndirectObject,
    context_chain: list[str],
    stream_text: str,
    start_offset: int,
    end_offset: int,
    context_start: int,
    xobject_obj: ParsedIndirectObject,
) -> dict[str, Any] | None:
    bbox = transformed_rect_bbox(ctm, 0.0, 0.0, 1.0, 1.0)
    if bbox is None:
        return None

    snippet_start = start_offset
    if (start_offset - context_start) <= 120:
        snippet_start = context_start

    source_slice = slice_snippet(
        text=stream_text,
        start=snippet_start,
        end=end_offset,
        context_before=0,
        context_after=120,
    )

    pixel_width = xobject_obj.value.get("/Width") if isinstance(xobject_obj.value, dict) else None
    pixel_height = xobject_obj.value.get("/Height") if isinstance(xobject_obj.value, dict) else None
    filters = normalize_filter_list(xobject_obj.value.get("/Filter")) if isinstance(xobject_obj.value, dict) else []

    return {
        "id": item_id,
        "kind": "image",
        "page_number": page_number,
        "bbox": bbox,
        "image": {
            "name": xobject_name,
            "pixel_width": pixel_width,
            "pixel_height": pixel_height,
            "filters": filters,
            "object_ref": xobject_obj.ref.label(),
        },
        "source": {
            "object_ref": stream_obj.ref.label(),
            "context_chain": context_chain,
            "snippet": source_slice["snippet"],
            "highlight_start": source_slice["highlight_start"],
            "highlight_end": source_slice["highlight_end"],
        },
        "summary": {
            "draw_width": round_float(bbox["width"]),
            "draw_height": round_float(bbox["height"]),
        },
    }


def attach_text_reference_context(
    inspector: PDFInspector,
    page_entry: dict[str, Any],
    stream_obj: ParsedIndirectObject,
    context_chain: list[str],
    object_details: dict[str, dict[str, Any]],
    item: dict[str, Any],
    text_state: TextState,
) -> None:
    reference_chain: list[dict[str, str]] = []
    append_reference(inspector, object_details, reference_chain, stream_obj.ref, "Selected content stream")

    for index, ref_label in enumerate(context_chain[1:], start=1):
        role = "Parent Form XObject" if index == 1 else f"Nested Form XObject {index}"
        append_reference(inspector, object_details, reference_chain, ref_from_label(ref_label), role)

    if text_state.font_metrics.object_ref is not None:
        append_reference(inspector, object_details, reference_chain, text_state.font_metrics.object_ref, "Active font resource")
    if text_state.font_metrics.to_unicode_ref is not None:
        append_reference(inspector, object_details, reference_chain, text_state.font_metrics.to_unicode_ref, "ToUnicode CMap")

    page_obj = page_entry.get("object")
    if page_obj is not None:
        append_reference(inspector, object_details, reference_chain, page_obj.ref, "Owning page object")

    item["source_comment"] = (
        "This excerpt comes from a PDF text-showing instruction. "
        "PDF text is positioned by the current text matrix plus the page graphics transform, so text also has locatable coordinates."
    )
    item["reference_chain"] = reference_chain


def attach_image_reference_context(
    inspector: PDFInspector,
    page_entry: dict[str, Any],
    stream_obj: ParsedIndirectObject,
    context_chain: list[str],
    object_details: dict[str, dict[str, Any]],
    item: dict[str, Any],
    xobject_obj: ParsedIndirectObject,
) -> None:
    reference_chain: list[dict[str, str]] = []
    append_reference(inspector, object_details, reference_chain, stream_obj.ref, "Selected content stream")

    for index, ref_label in enumerate(context_chain[1:], start=1):
        role = "Parent Form XObject" if index == 1 else f"Nested Form XObject {index}"
        append_reference(inspector, object_details, reference_chain, ref_from_label(ref_label), role)

    append_reference(inspector, object_details, reference_chain, xobject_obj.ref, "Image XObject")

    page_obj = page_entry.get("object")
    if page_obj is not None:
        append_reference(inspector, object_details, reference_chain, page_obj.ref, "Owning page object")

    item["source_comment"] = (
        "This excerpt comes from an image draw instruction. "
        "The `cm` matrix places and scales the image, and the `Do` operator invokes the referenced Image XObject."
    )
    item["reference_chain"] = reference_chain

def finalize_path_item(
    *,
    item_id: str,
    page_number: int,
    paint_operator: str,
    path: PathBuilder,
    state: GraphicsState,
    stream_obj: ParsedIndirectObject,
    context_chain: list[str],
    stream_text: str,
    end_offset: int,
) -> dict[str, Any] | None:
    if not path.has_geometry() or path.source_start is None:
        return None

    expanded_line_width = max(state.line_width * effective_scale(state.ctm), 0.6)
    bbox = build_bbox(path.points, margin=expanded_line_width / 2.0)
    if bbox is None:
        return None

    snippet_start = path.source_start
    if path.context_start is not None and (path.source_start - path.context_start) <= 120:
        snippet_start = path.context_start

    source_slice = slice_snippet(
        text=stream_text,
        start=snippet_start,
        end=end_offset,
        context_before=0,
        context_after=120,
    )

    return {
        "id": item_id,
        "kind": "vector_path",
        "page_number": page_number,
        "paint_operator": paint_operator,
        "bbox": bbox,
        "commands": [dict(command) for command in path.commands],
        "line_width": round_float(state.line_width),
        "effective_line_width": round_float(expanded_line_width),
        "source": {
            "object_ref": stream_obj.ref.label(),
            "context_chain": context_chain,
            "snippet": source_slice["snippet"],
            "highlight_start": source_slice["highlight_start"],
            "highlight_end": source_slice["highlight_end"],
        },
        "summary": {
            "command_count": len(path.commands),
            "point_count": len(path.points),
        },
    }


def extract_vector_paths_for_page(
    inspector: PDFInspector,
    page_entry: dict[str, Any],
    object_details: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    page_value = page_entry["value"]
    inherited = page_entry["inherited"]
    resources_token = page_value.get("/Resources", inherited.get("/Resources"))
    stream_objects = inspector.content_stream_objects_for_page(page_entry)
    warnings: list[str] = []
    items: list[dict[str, Any]] = []
    item_counter = 0

    def walk_stream(
        stream_obj: ParsedIndirectObject,
        stream_data: bytes,
        local_resources: Any,
        base_ctm: Matrix,
        context_chain: list[str],
    ) -> None:
        nonlocal item_counter

        tokenizer = PositionedContentTokenizer(stream_data)
        operands: list[PositionedToken] = []
        state = GraphicsState(ctm=base_ctm)
        stack: list[GraphicsState] = []
        path = PathBuilder()
        last_context_start = 0
        stream_text = stream_data.decode("latin-1", errors="replace")

        while True:
            token = tokenizer.next_token()
            if token is None:
                return

            if isinstance(token.value, str):
                operator = token.value

                if operator in STYLE_OPERATORS:
                    last_context_start = operand_start(operands, len(operands), token.start)

                if operator == "q":
                    stack.append(state.clone())
                elif operator == "Q":
                    if stack:
                        state = stack.pop()
                elif operator == "cm":
                    values = operand_numbers(operands, 6)
                    if values is not None:
                        matrix = matrix_from_array(values)
                        if matrix is not None:
                            state.ctm = matrix_multiply(matrix, state.ctm)
                elif operator == "w":
                    values = operand_numbers(operands, 1)
                    if values is not None:
                        state.line_width = values[0]
                elif operator == "m":
                    values = operand_numbers(operands, 2)
                    if values is not None:
                        source_start = operand_start(operands, 2, token.start)
                        path.begin(source_start, last_context_start)
                        path.move_to(transform_point(state.ctm, values[0], values[1]))
                elif operator == "l":
                    values = operand_numbers(operands, 2)
                    if values is not None:
                        source_start = operand_start(operands, 2, token.start)
                        path.begin(source_start, last_context_start)
                        path.line_to(transform_point(state.ctm, values[0], values[1]))
                elif operator == "c":
                    values = operand_numbers(operands, 6)
                    if values is not None:
                        source_start = operand_start(operands, 6, token.start)
                        path.begin(source_start, last_context_start)
                        path.curve_to(
                            transform_point(state.ctm, values[0], values[1]),
                            transform_point(state.ctm, values[2], values[3]),
                            transform_point(state.ctm, values[4], values[5]),
                        )
                elif operator == "v":
                    values = operand_numbers(operands, 4)
                    if values is not None and path.current_point is not None:
                        source_start = operand_start(operands, 4, token.start)
                        path.begin(source_start, last_context_start)
                        path.curve_to(
                            path.current_point,
                            transform_point(state.ctm, values[0], values[1]),
                            transform_point(state.ctm, values[2], values[3]),
                        )
                elif operator == "y":
                    values = operand_numbers(operands, 4)
                    if values is not None:
                        source_start = operand_start(operands, 4, token.start)
                        path.begin(source_start, last_context_start)
                        end_point = transform_point(state.ctm, values[2], values[3])
                        path.curve_to(
                            transform_point(state.ctm, values[0], values[1]),
                            end_point,
                            end_point,
                        )
                elif operator == "re":
                    values = operand_numbers(operands, 4)
                    if values is not None:
                        source_start = operand_start(operands, 4, token.start)
                        path.begin(source_start, last_context_start)
                        path.rectangle(state.ctm, values[0], values[1], values[2], values[3])
                elif operator == "h":
                    path.close_path()
                elif operator in {"s", "b", "b*"}:
                    path.close_path()
                    item_counter += 1
                    item = finalize_path_item(
                        item_id=f"page-{page_entry['index']:04d}-vector-{item_counter:05d}",
                        page_number=page_entry["index"],
                        paint_operator=operator,
                        path=path,
                        state=state,
                        stream_obj=stream_obj,
                        context_chain=context_chain,
                        stream_text=stream_text,
                        end_offset=token.end,
                    )
                    if item is not None:
                        attach_vector_reference_context(
                            inspector=inspector,
                            page_entry=page_entry,
                            stream_obj=stream_obj,
                            context_chain=context_chain,
                            object_details=object_details,
                            item=item,
                        )
                        items.append(item)
                    path.reset()
                elif operator in PAINT_OPERATORS:
                    item_counter += 1
                    item = finalize_path_item(
                        item_id=f"page-{page_entry['index']:04d}-vector-{item_counter:05d}",
                        page_number=page_entry["index"],
                        paint_operator=operator,
                        path=path,
                        state=state,
                        stream_obj=stream_obj,
                        context_chain=context_chain,
                        stream_text=stream_text,
                        end_offset=token.end,
                    )
                    if item is not None:
                        attach_vector_reference_context(
                            inspector=inspector,
                            page_entry=page_entry,
                            stream_obj=stream_obj,
                            context_chain=context_chain,
                            object_details=object_details,
                            item=item,
                        )
                        items.append(item)
                    path.reset()
                elif operator == "n":
                    path.reset()
                elif operator == "Do":
                    if operands and isinstance(operands[-1].value, PDFName):
                        xobject_name = operands[-1].value.value
                        xobject_obj = inspector.resolve_xobject(local_resources, xobject_name)
                        if xobject_obj and isinstance(xobject_obj.value, dict):
                            subtype = xobject_obj.value.get("/Subtype")
                            if is_name(subtype, "/Form") and xobject_obj.decoded_stream is not None:
                                form_matrix = matrix_from_array(inspector.resolve(xobject_obj.value.get("/Matrix")))
                                next_ctm = matrix_multiply(form_matrix, state.ctm) if form_matrix else state.ctm
                                next_resources = xobject_obj.value.get("/Resources", local_resources)
                                walk_stream(
                                    stream_obj=xobject_obj,
                                    stream_data=xobject_obj.decoded_stream,
                                    local_resources=next_resources,
                                    base_ctm=next_ctm,
                                    context_chain=context_chain + [xobject_obj.ref.label()],
                                )
                            elif is_name(subtype, "/Form") and xobject_obj.decoded_stream is None:
                                warnings.append(
                                    f"Could not decode Form XObject {xobject_obj.ref.label()} referenced from {stream_obj.ref.label()}."
                                )
                elif operator == "BI":
                    warnings.append(
                        f"Encountered inline image data inside {stream_obj.ref.label()}; inline images are not clickable in this viewer."
                    )
                    path.reset()
                    tokenizer.skip_inline_image()

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
            stream_obj=stream_obj,
            stream_data=stream_obj.decoded_stream,
            local_resources=resources_token,
            base_ctm=IDENTITY_MATRIX,
            context_chain=[stream_obj.ref.label()],
        )

    return items, warnings


def extract_text_and_image_items_for_page(
    inspector: PDFInspector,
    page_entry: dict[str, Any],
    object_details: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    page_value = page_entry["value"]
    inherited = page_entry["inherited"]
    resources_token = page_value.get("/Resources", inherited.get("/Resources"))
    stream_objects = inspector.content_stream_objects_for_page(page_entry)
    warnings: list[str] = []
    items: list[dict[str, Any]] = []
    item_counter = 0
    font_metrics_cache: dict[str, FontMetrics] = {}

    def emit_text_item(
        *,
        operator: str,
        operand: Any,
        operand_token: PositionedToken,
        token: PositionedToken,
        text_state: TextState,
        graphics_state: GraphicsState,
        stream_obj: ParsedIndirectObject,
        context_chain: list[str],
        stream_text: str,
        context_start: int,
    ) -> None:
        nonlocal item_counter
        text_value, raw_glyph_text = decode_text_operand(operand, text_state.cmap)
        item = finalize_text_item(
            item_id=f"page-{page_entry['index']:04d}-text-{item_counter + 1:05d}",
            page_number=page_entry["index"],
            operator=operator,
            text_value=text_value,
            raw_glyph_text=raw_glyph_text,
            operand=operand,
            text_state=text_state,
            graphics_state=graphics_state,
            stream_obj=stream_obj,
            context_chain=context_chain,
            stream_text=stream_text,
            start_offset=operand_token.start,
            end_offset=token.end,
            context_start=context_start,
        )
        advance = advance_for_text_operand(operand, text_state)
        if item is not None:
            item_counter += 1
            item["id"] = f"page-{page_entry['index']:04d}-text-{item_counter:05d}"
            attach_text_reference_context(
                inspector=inspector,
                page_entry=page_entry,
                stream_obj=stream_obj,
                context_chain=context_chain,
                object_details=object_details,
                item=item,
                text_state=text_state,
            )
            items.append(item)
        update_text_matrix_after_showing(text_state, advance)

    def emit_image_item(
        *,
        xobject_name: str,
        operand_token: PositionedToken,
        token: PositionedToken,
        graphics_state: GraphicsState,
        stream_obj: ParsedIndirectObject,
        context_chain: list[str],
        stream_text: str,
        context_start: int,
        xobject_obj: ParsedIndirectObject,
    ) -> None:
        nonlocal item_counter
        item = finalize_image_item(
            item_id=f"page-{page_entry['index']:04d}-image-{item_counter + 1:05d}",
            page_number=page_entry["index"],
            xobject_name=xobject_name,
            ctm=graphics_state.ctm,
            stream_obj=stream_obj,
            context_chain=context_chain,
            stream_text=stream_text,
            start_offset=operand_token.start,
            end_offset=token.end,
            context_start=context_start,
            xobject_obj=xobject_obj,
        )
        if item is None:
            return
        item_counter += 1
        item["id"] = f"page-{page_entry['index']:04d}-image-{item_counter:05d}"
        attach_image_reference_context(
            inspector=inspector,
            page_entry=page_entry,
            stream_obj=stream_obj,
            context_chain=context_chain,
            object_details=object_details,
            item=item,
            xobject_obj=xobject_obj,
        )
        items.append(item)

    def walk_stream(
        stream_obj: ParsedIndirectObject,
        stream_data: bytes,
        local_resources: Any,
        base_ctm: Matrix,
        context_chain: list[str],
    ) -> None:
        tokenizer = PositionedContentTokenizer(stream_data)
        operands: list[PositionedToken] = []
        graphics_state = GraphicsState(ctm=base_ctm)
        graphics_stack: list[tuple[GraphicsState, TextState]] = []
        text_state = TextState()
        stream_text = stream_data.decode("latin-1", errors="replace")
        last_context_start = 0

        while True:
            token = tokenizer.next_token()
            if token is None:
                return

            if isinstance(token.value, str):
                operator = token.value

                if operator in STYLE_OPERATORS or operator in TEXT_STATE_OPERATORS or operator in {"Tj", "TJ", "'", '"', "Do"}:
                    last_context_start = operand_start(operands, len(operands), token.start)

                if operator == "q":
                    graphics_stack.append((graphics_state.clone(), text_state.clone()))
                elif operator == "Q":
                    if graphics_stack:
                        graphics_state, text_state = graphics_stack.pop()
                elif operator == "cm":
                    values = operand_numbers(operands, 6)
                    if values is not None:
                        matrix = matrix_from_array(values)
                        if matrix is not None:
                            graphics_state.ctm = matrix_multiply(matrix, graphics_state.ctm)
                elif operator == "BT":
                    text_state = TextState(
                        font_name=text_state.font_name,
                        font_size=text_state.font_size,
                        char_spacing=text_state.char_spacing,
                        word_spacing=text_state.word_spacing,
                        horizontal_scale=text_state.horizontal_scale,
                        leading=text_state.leading,
                        rise=text_state.rise,
                        cmap=dict(text_state.cmap),
                        font_metrics=text_state.font_metrics,
                        in_text_object=True,
                    )
                elif operator == "ET":
                    text_state.in_text_object = False
                elif operator == "Tf":
                    if len(operands) >= 2:
                        font_token = operands[-2].value
                        size_token = operands[-1].value
                        if isinstance(font_token, PDFName) and isinstance(size_token, (int, float)):
                            text_state.font_name = font_token.value
                            text_state.font_size = float(size_token)
                            text_state.cmap = inspector.resolve_font_cmap(local_resources, text_state.font_name)
                            text_state.font_metrics = resolve_font_metrics(
                                inspector,
                                local_resources,
                                text_state.font_name,
                                font_metrics_cache,
                            )
                elif operator == "Tm":
                    values = operand_numbers(operands, 6)
                    if values is not None:
                        matrix = matrix_from_array(values)
                        if matrix is not None:
                            text_state.text_matrix = matrix
                            text_state.line_matrix = matrix
                elif operator == "Td":
                    values = operand_numbers(operands, 2)
                    if values is not None:
                        move_text_position(text_state, values[0], values[1])
                elif operator == "TD":
                    values = operand_numbers(operands, 2)
                    if values is not None:
                        text_state.leading = -values[1]
                        move_text_position(text_state, values[0], values[1])
                elif operator == "T*":
                    move_text_position(text_state, 0.0, -text_state.leading)
                elif operator == "Tc":
                    values = operand_numbers(operands, 1)
                    if values is not None:
                        text_state.char_spacing = values[0]
                elif operator == "Tw":
                    values = operand_numbers(operands, 1)
                    if values is not None:
                        text_state.word_spacing = values[0]
                elif operator == "Tz":
                    values = operand_numbers(operands, 1)
                    if values is not None:
                        text_state.horizontal_scale = values[0] / 100.0
                elif operator == "TL":
                    values = operand_numbers(operands, 1)
                    if values is not None:
                        text_state.leading = values[0]
                elif operator == "Ts":
                    values = operand_numbers(operands, 1)
                    if values is not None:
                        text_state.rise = values[0]
                elif operator in {"Tj", "'", '"'}:
                    if operands:
                        if operator == "'":
                            move_text_position(text_state, 0.0, -text_state.leading)
                            operand_token = operands[-1]
                            emit_text_item(
                                operator=operator,
                                operand=operand_token.value,
                                operand_token=operand_token,
                                token=token,
                                text_state=text_state,
                                graphics_state=graphics_state,
                                stream_obj=stream_obj,
                                context_chain=context_chain,
                                stream_text=stream_text,
                                context_start=last_context_start,
                            )
                        elif operator == '"':
                            if len(operands) >= 3:
                                word_spacing = operands[-3].value
                                char_spacing = operands[-2].value
                                if isinstance(word_spacing, (int, float)):
                                    text_state.word_spacing = float(word_spacing)
                                if isinstance(char_spacing, (int, float)):
                                    text_state.char_spacing = float(char_spacing)
                                move_text_position(text_state, 0.0, -text_state.leading)
                                operand_token = operands[-1]
                                emit_text_item(
                                    operator=operator,
                                    operand=operand_token.value,
                                    operand_token=operand_token,
                                    token=token,
                                    text_state=text_state,
                                    graphics_state=graphics_state,
                                    stream_obj=stream_obj,
                                    context_chain=context_chain,
                                    stream_text=stream_text,
                                    context_start=last_context_start,
                                )
                        else:
                            operand_token = operands[-1]
                            emit_text_item(
                                operator=operator,
                                operand=operand_token.value,
                                operand_token=operand_token,
                                token=token,
                                text_state=text_state,
                                graphics_state=graphics_state,
                                stream_obj=stream_obj,
                                context_chain=context_chain,
                                stream_text=stream_text,
                                context_start=last_context_start,
                            )
                elif operator == "TJ":
                    if operands:
                        operand_token = operands[-1]
                        emit_text_item(
                            operator=operator,
                            operand=operand_token.value,
                            operand_token=operand_token,
                            token=token,
                            text_state=text_state,
                            graphics_state=graphics_state,
                            stream_obj=stream_obj,
                            context_chain=context_chain,
                            stream_text=stream_text,
                            context_start=last_context_start,
                        )
                elif operator == "Do":
                    if operands and isinstance(operands[-1].value, PDFName):
                        operand_token = operands[-1]
                        xobject_name = operand_token.value.value
                        xobject_obj = inspector.resolve_xobject(local_resources, xobject_name)
                        if xobject_obj and isinstance(xobject_obj.value, dict):
                            subtype = xobject_obj.value.get("/Subtype")
                            if is_name(subtype, "/Image"):
                                emit_image_item(
                                    xobject_name=xobject_name,
                                    operand_token=operand_token,
                                    token=token,
                                    graphics_state=graphics_state,
                                    stream_obj=stream_obj,
                                    context_chain=context_chain,
                                    stream_text=stream_text,
                                    context_start=last_context_start,
                                    xobject_obj=xobject_obj,
                                )
                            elif is_name(subtype, "/Form") and xobject_obj.decoded_stream is not None:
                                form_matrix = matrix_from_array(inspector.resolve(xobject_obj.value.get("/Matrix")))
                                next_ctm = matrix_multiply(form_matrix, graphics_state.ctm) if form_matrix else graphics_state.ctm
                                next_resources = xobject_obj.value.get("/Resources", local_resources)
                                walk_stream(
                                    stream_obj=xobject_obj,
                                    stream_data=xobject_obj.decoded_stream,
                                    local_resources=next_resources,
                                    base_ctm=next_ctm,
                                    context_chain=context_chain + [xobject_obj.ref.label()],
                                )
                            elif is_name(subtype, "/Form") and xobject_obj.decoded_stream is None:
                                warnings.append(
                                    f"Could not decode Form XObject {xobject_obj.ref.label()} referenced from {stream_obj.ref.label()}."
                                )
                elif operator == "BI":
                    warnings.append(
                        f"Encountered inline image data inside {stream_obj.ref.label()}; inline images are not clickable in this viewer."
                    )
                    tokenizer.skip_inline_image()

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
            stream_obj=stream_obj,
            stream_data=stream_obj.decoded_stream,
            local_resources=resources_token,
            base_ctm=IDENTITY_MATRIX,
            context_chain=[stream_obj.ref.label()],
        )

    return items, warnings


def build_link_items(
    inspector: PDFInspector,
    page_entry: dict[str, Any],
    object_details: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    raw_links = inspector.extract_links_for_page(page_entry)
    items: list[dict[str, Any]] = []

    for index, raw_link in enumerate(raw_links, start=1):
        rect = rect_to_readable(raw_link.get("rect"))
        if rect is None:
            continue
        readable = simplify_link(raw_link)
        raw_source = raw_link.get("raw_object") or ""
        item = {
            "id": f"page-{page_entry['index']:04d}-link-{index:05d}",
            "kind": "link",
            "page_number": page_entry["index"],
            "bbox": rect,
            "link": {
                "kind": readable.get("kind"),
                "target": readable.get("target"),
                "action": readable.get("action"),
            },
            "source": {
                "object_ref": raw_link.get("object_ref"),
                "context_chain": [raw_link.get("object_ref")] if raw_link.get("object_ref") else [],
                "snippet": raw_source,
                "highlight_start": 0,
                "highlight_end": len(raw_source),
            },
        }
        attach_link_reference_context(
            inspector=inspector,
            page_entry=page_entry,
            annotation_obj=inspector.get_object(ref_from_label(raw_link.get("object_ref"))),
            object_details=object_details,
            item=item,
        )
        items.append(item)

    return items


def build_page_data(inspector: PDFInspector, page_entry: dict[str, Any]) -> dict[str, Any]:
    page_value = page_entry["value"]
    inherited = page_entry["inherited"]
    media_box = page_value.get("/MediaBox", inherited.get("/MediaBox"))
    numbers = numeric_list(media_box)
    if numbers is None or len(numbers) != 4:
        raise ValueError(f"Page {page_entry['index']} does not expose a usable /MediaBox.")

    width = numbers[2] - numbers[0]
    height = numbers[3] - numbers[1]
    object_details: dict[str, dict[str, Any]] = {}
    vector_items, vector_warnings = extract_vector_paths_for_page(inspector, page_entry, object_details)
    text_image_items, text_image_warnings = extract_text_and_image_items_for_page(inspector, page_entry, object_details)
    link_items = build_link_items(inspector, page_entry, object_details)
    items = vector_items + text_image_items + link_items
    warnings = list(dict.fromkeys(vector_warnings + text_image_warnings))
    text_items = [item for item in text_image_items if item.get("kind") == "text"]
    image_items = [item for item in text_image_items if item.get("kind") == "image"]

    return {
        "page_number": page_entry["index"],
        "page_object_ref": page_entry["object"].ref.label() if page_entry["object"] else None,
        "page_size": {
            "width_pt": round_float(width),
            "height_pt": round_float(height),
        },
        "content_streams": [item.ref.label() for item in inspector.content_stream_objects_for_page(page_entry)],
        "item_counts": {
            "vector_path": len(vector_items),
            "text": len(text_items),
            "image": len(image_items),
            "link": len(link_items),
        },
        "warnings": warnings,
        "object_details": object_details,
        "items": items,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_document_data(pdf_path: Path, data_root: Path) -> dict[str, Any]:
    inspector = PDFInspector(pdf_path)
    doc_slug = safe_slug(pdf_path.stem)
    pdf_target_path = data_root / "pdfs" / f"{doc_slug}.pdf"
    pdf_target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf_path, pdf_target_path)

    doc_root = data_root / "documents" / doc_slug
    pages_root = doc_root / "pages"
    pages = inspector.iter_pages()
    page_entries: list[dict[str, Any]] = []

    for page_entry in pages:
        page_data = build_page_data(inspector, page_entry)
        page_entries.append(
            {
                "page_number": page_data["page_number"],
                "page_size": page_data["page_size"],
                "item_counts": page_data["item_counts"],
                "warnings": page_data["warnings"],
                "data_url": f"/reader-data/documents/{doc_slug}/pages/page-{page_entry['index']:04d}.json",
            }
        )
        write_json(pages_root / f"page-{page_entry['index']:04d}.json", page_data)

    document_payload = {
        "id": doc_slug,
        "title": pdf_path.name,
        "pdf_url": f"/reader-data/pdfs/{doc_slug}.pdf",
        "page_count": len(page_entries),
        "pages": page_entries,
        "resolved_object_count": len(inspector.objects),
        "header": inspector.header,
    }
    write_json(doc_root / "document.json", document_payload)
    return document_payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build interactive data for the PDF reader frontend.")
    parser.add_argument(
        "--input-dir",
        default="data/eplans",
        help="Directory that contains source PDF files.",
    )
    parser.add_argument(
        "--output-dir",
        default="pdf_reader/public/reader-data",
        help="Directory where generated reader data will be written.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    input_dir = repo_root / args.input_dir
    output_dir = repo_root / args.output_dir

    pdf_paths = sorted(input_dir.glob("*.pdf"))
    if not pdf_paths:
        raise SystemExit(f"No PDF files found under {input_dir}.")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    documents = [build_document_data(pdf_path, output_dir) for pdf_path in pdf_paths]
    write_json(output_dir / "manifest.json", {"documents": documents})

    print(f"Built reader data for {len(documents)} PDF file(s).")
    for document in documents:
        print(
            f"- {document['title']}: pages={document['page_count']}, "
            f"objects={document['resolved_object_count']}"
        )
    print(f"Reader data written to: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
