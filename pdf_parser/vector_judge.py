"""
Judge whether vectors found by bbox lookup match target_code.txt.

The target code is interpreted as PDF path source in PDF user space. The page
lookup is delegated to vector_sniffer, which converts PDF-space query bboxes to
PyMuPDF page space internally.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from utils import resolve_repo_relative
from vector_sniffer import vector_sniffer


Token = str
Point = tuple[float, float]
BBox = tuple[float, float, float, float]

NUM = re.compile(r"^-?(?:\d+\.?\d*|\.\d+)$")


def _fmt_num(value: float, precision: int = 3) -> str:
    text = f"{float(value):.{precision}f}".rstrip("0").rstrip(".")
    return text if text and text != "-0" else "0"


def _normalize_code(code: str, precision: int = 3) -> str:
    tokens = tokenize_pdf_path(code)
    normalized: list[str] = []
    for token in tokens:
        if is_number(token):
            normalized.append(_fmt_num(float(token), precision))
        else:
            normalized.append(token)
    return " ".join(normalized)


def _shape_code_from_points(shape: dict[str, Any], page_height: float, precision: int) -> str:
    points = [(float(point[0]), page_height - float(point[1])) for point in shape["points"]]
    op = shape.get("op")
    if op == "l" and len(points) == 2:
        p0, p1 = points
        return (
            f"{_fmt_num(p0[0], precision)} {_fmt_num(p0[1], precision)} m "
            f"{_fmt_num(p1[0], precision)} {_fmt_num(p1[1], precision)} l"
        )
    if op == "c" and len(points) == 4:
        _, p1, p2, p3 = points
        return (
            f"{_fmt_num(p1[0], precision)} {_fmt_num(p1[1], precision)} "
            f"{_fmt_num(p2[0], precision)} {_fmt_num(p2[1], precision)} "
            f"{_fmt_num(p3[0], precision)} {_fmt_num(p3[1], precision)} c"
        )
    if op == "re" and len(points) == 4:
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        x0 = min(xs)
        y0 = min(ys)
        width = max(xs) - x0
        height = max(ys) - y0
        return (
            f"{_fmt_num(x0, precision)} {_fmt_num(y0, precision)} "
            f"{_fmt_num(width, precision)} {_fmt_num(height, precision)} re"
        )
    return _normalize_code(shape["code"], precision)


def is_number(token: str) -> bool:
    return bool(NUM.match(token))


def tokenize_pdf_path(src: str) -> list[Token]:
    """Tokenize a small PDF path source fragment."""
    tokens: list[Token] = []
    i = 0
    while i < len(src):
        ch = src[i]
        if ch == "%":
            while i < len(src) and src[i] not in "\r\n":
                i += 1
            continue
        if ch.isspace():
            i += 1
            continue

        rest = src[i:]
        number = re.match(r"^-?(?:\d+\.?\d*|\.\d+)", rest)
        if number:
            tokens.append(number.group(0))
            i += len(number.group(0))
            continue

        op = re.match(r"^[A-Za-z*']+", rest)
        if op:
            tokens.append(op.group(0))
            i += len(op.group(0))
            continue

        i += 1
    return tokens


def parse_target_code(text: str) -> list[dict[str, Any]]:
    """Parse m/l/c/re path commands into vector_sniffer-like shape records."""
    tokens = tokenize_pdf_path(text)
    stack: list[float] = []
    current: Point | None = None
    shapes: list[dict[str, Any]] = []

    def pop_numbers(count: int) -> list[float] | None:
        if len(stack) < count:
            return None
        values = stack[-count:]
        del stack[-count:]
        return values

    for token in tokens:
        if is_number(token):
            stack.append(float(token))
            continue

        if token == "m":
            values = pop_numbers(2)
            if values is not None:
                current = (values[0], values[1])
        elif token == "l":
            values = pop_numbers(2)
            if values is not None and current is not None:
                end = (values[0], values[1])
                code = f"{_fmt_num(current[0])} {_fmt_num(current[1])} m\n{_fmt_num(end[0])} {_fmt_num(end[1])} l"
                shapes.append({"type": "line", "op": "l", "code": code, "points": [current, end]})
                current = end
        elif token == "c":
            values = pop_numbers(6)
            if values is not None and current is not None:
                p1 = (values[0], values[1])
                p2 = (values[2], values[3])
                p3 = (values[4], values[5])
                code = (
                    f"{_fmt_num(p1[0])} {_fmt_num(p1[1])} "
                    f"{_fmt_num(p2[0])} {_fmt_num(p2[1])} "
                    f"{_fmt_num(p3[0])} {_fmt_num(p3[1])} c"
                )
                shapes.append({"type": "curve", "op": "c", "code": code, "points": [current, p1, p2, p3]})
                current = p3
        elif token == "re":
            values = pop_numbers(4)
            if values is not None:
                x, y, width, height = values
                points = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
                code = f"{_fmt_num(x)} {_fmt_num(y)} {_fmt_num(width)} {_fmt_num(height)} re"
                shapes.append({"type": "rect", "op": "re", "code": code, "points": points})
                current = points[0]
        else:
            stack.clear()

    return shapes


def bbox_from_shapes(shapes: list[dict[str, Any]]) -> BBox:
    points = [point for shape in shapes for point in shape["points"]]
    if not points:
        raise ValueError("No vector points found in target code")
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def find_target_code_bbox(target_code_path: Path) -> dict[str, float]:
    """Find the PDF-space bbox for all shapes inside target_code.txt."""
    shapes = parse_target_code(target_code_path.read_text(encoding="utf-8"))
    x0, y0, x1, y1 = bbox_from_shapes(shapes)
    return {
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "width": x1 - x0,
        "height": y1 - y0,
    }


def compare_target_with_hits(
    target_shapes: list[dict[str, Any]],
    hits: list[dict[str, Any]],
    *,
    page_height: float,
    precision: int = 3,
) -> dict[str, Any]:
    """Compare target code shapes with vector_sniffer query results."""
    target_codes = [_normalize_code(shape["code"], precision) for shape in target_shapes]
    hit_codes = [_shape_code_from_points(hit, page_height, precision) for hit in hits]

    target_counter = Counter(target_codes)
    hit_counter = Counter(hit_codes)
    missing_counter = target_counter - hit_counter
    extra_counter = hit_counter - target_counter

    return {
        "matched": not missing_counter and not extra_counter,
        "target_count": len(target_codes),
        "hit_count": len(hit_codes),
        "missing": list(missing_counter.elements()),
        "extra": list(extra_counter.elements()),
        "target_codes": target_codes,
        "hit_codes": hit_codes,
    }


def judge_target_code(
    *,
    pdf_path: Path,
    target_code_path: Path,
    page: int = 6,
    slack: float = 0.000001,
    precision: int = 3,
) -> dict[str, Any]:
    """Find target bbox, query that bbox on the selected page, and compare code."""
    target_text = target_code_path.read_text(encoding="utf-8")
    target_shapes = parse_target_code(target_text)
    bbox = find_target_code_bbox(target_code_path)

    with vector_sniffer(pdf_path) as sniffer:
        sniffer.goto(page)
        page_height = float(sniffer.page_height_pt or 0.0)
        hits = sniffer.query_bbox(
            bbox=(bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"]),
            slack=slack,
            coord_space="pdf",
        )

    comparison = compare_target_with_hits(target_shapes, hits, page_height=page_height, precision=precision)
    return {
        "pdf": str(pdf_path),
        "page": page,
        "target_code": str(target_code_path),
        "bbox_pdf": bbox,
        "slack": slack,
        "precision": precision,
        "comparison": comparison,
        "hits": hits,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare target_code.txt with vectors queried from its bbox.")
    parser.add_argument(
        "--pdf-file-path",
        default="./data/eplans/1VLG100537_Standard_Documentation.pdf",
        help="Input PDF path.",
    )
    parser.add_argument(
        "--target-code-path",
        default="./pdf_parser/target_code.txt",
        help="PDF path source snippet to judge.",
    )
    parser.add_argument("--page", type=int, default=6, help="1-based PDF page number.")
    parser.add_argument("--slack", type=float, default=0.000001, help="Symmetric bbox expansion ratio.")
    parser.add_argument("--precision", type=int, default=3, help="Decimal precision for code comparison.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pdf_path = resolve_repo_relative(args.pdf_file_path)
    target_code_path = resolve_repo_relative(args.target_code_path)

    if not pdf_path.is_file():
        raise SystemExit(f"PDF not found: {pdf_path}")
    if not target_code_path.is_file():
        raise SystemExit(f"Target code not found: {target_code_path}")

    result = judge_target_code(
        pdf_path=pdf_path,
        target_code_path=target_code_path,
        page=args.page,
        slack=args.slack,
        precision=args.precision,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
