#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import fitz


DEFAULT_SOURCE_WIDTH = 420.0
DEFAULT_SOURCE_HEIGHT = 297.0


def load_page_record(json_path: Path, page_number: int) -> dict[str, Any]:
    pages = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(pages, list):
        raise ValueError(f"Expected compact JSON root to be a list: {json_path}")
    for page in pages:
        if page.get("page") == page_number:
            return page
    raise ValueError(f"Page {page_number} not found in {json_path}")


def source_bbox_to_pdf_rect(
    bbox: list[float],
    page_rect: fitz.Rect,
    *,
    source_width: float,
    source_height: float,
    flip_y: bool,
) -> fitz.Rect:
    x0, y0, x1, y1 = bbox
    sx = page_rect.width / source_width
    sy = page_rect.height / source_height

    left = min(x0, x1) * sx + page_rect.x0
    right = max(x0, x1) * sx + page_rect.x0
    if flip_y:
        top = page_rect.y0 + (source_height - max(y0, y1)) * sy
        bottom = page_rect.y0 + (source_height - min(y0, y1)) * sy
    else:
        top = min(y0, y1) * sy + page_rect.y0
        bottom = max(y0, y1) * sy + page_rect.y0

    return fitz.Rect(left, top, right, bottom) & page_rect


def draw_items(
    page: fitz.Page,
    items: list[dict[str, Any]],
    *,
    source_width: float,
    source_height: float,
    flip_y: bool,
    draw_labels: bool,
) -> int:
    drawn = 0
    for item in items:
        bbox = item.get("bbox") or item.get("position")
        if not bbox:
            continue
        rect = source_bbox_to_pdf_rect(
            bbox,
            page.rect,
            source_width=source_width,
            source_height=source_height,
            flip_y=flip_y,
        )
        if rect.is_empty or rect.width <= 0 or rect.height <= 0:
            continue
        page.draw_rect(rect, color=(1, 0, 0), width=1.2, overlay=True)
        if draw_labels:
            label = str(item.get("device_id") or item.get("id") or "")
            if label:
                label_point = fitz.Point(rect.x0 + 2, max(page.rect.y0 + 8, rect.y0 - 3))
                page.insert_text(label_point, label, fontsize=8, color=(1, 0, 0), overlay=True)
        drawn += 1
    return drawn


def render_page(
    pdf_path: Path,
    json_path: Path,
    output_path: Path,
    *,
    page_number: int,
    source_width: float,
    source_height: float,
    flip_y: bool,
    zoom: float,
    draw_labels: bool,
) -> dict[str, Any]:
    page_record = load_page_record(json_path, page_number)
    doc = fitz.open(pdf_path)
    try:
        if page_number < 1 or page_number > doc.page_count:
            raise ValueError(f"PDF page {page_number} is outside 1..{doc.page_count}: {pdf_path}")
        page = doc.load_page(page_number - 1)
        drawable_items = page_record.get("function_occurrences") or page_record.get("devices") or []
        drawn_count = draw_items(
            page,
            drawable_items,
            source_width=source_width,
            source_height=source_height,
            flip_y=flip_y,
            draw_labels=draw_labels,
        )
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(output_path)
        return {
            "pdf": str(pdf_path),
            "json": str(json_path),
            "page": page_number,
            "page_info": page_record.get("info") or {},
            "function_occurrence_count": len(page_record.get("function_occurrences") or []),
            "device_count": len(page_record.get("devices") or []),
            "drawn_device_count": drawn_count,
            "output": str(output_path),
            "source_size": [source_width, source_height],
            "pdf_page_size": [page.rect.width, page.rect.height],
            "flip_y": flip_y,
        }
    finally:
        doc.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a PDF page and draw EPDZ compact-json device bounding boxes on it.",
    )
    parser.add_argument("--pdf", type=Path, default=Path("data/eplans/#000_1.pdf"), help="Input PDF path.")
    parser.add_argument(
        "--json",
        type=Path,
        default=Path("output/epdz_inspection/ESS_Sample_Macros.compact.json"),
        help="Compact JSON from scripts/inspect_eplan_pdfs.py.",
    )
    parser.add_argument("--page", type=int, required=True, help="1-based page number to render.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output PNG path. Defaults to output/epdz_bbox_overlay/<pdf-stem>_page_<n>.png.",
    )
    parser.add_argument("--source-width", type=float, default=DEFAULT_SOURCE_WIDTH, help="JSON bbox source width.")
    parser.add_argument("--source-height", type=float, default=DEFAULT_SOURCE_HEIGHT, help="JSON bbox source height.")
    parser.add_argument("--flip-y", action="store_true", help="Flip JSON bbox Y axis before drawing.")
    parser.add_argument("--zoom", type=float, default=2.0, help="Render zoom. 2.0 is roughly 144 dpi.")
    parser.add_argument("--no-labels", action="store_true", help="Draw boxes only, without device id labels.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output or Path("output/epdz_bbox_overlay") / f"{args.pdf.stem}_page_{args.page:04d}.png"
    summary = render_page(
        args.pdf,
        args.json,
        output,
        page_number=args.page,
        source_width=args.source_width,
        source_height=args.source_height,
        flip_y=args.flip_y,
        zoom=args.zoom,
        draw_labels=not args.no_labels,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
