from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import fitz

from pdf_parsing import (
    REPO_ROOT,
    drawing_intersects_rect,
    rect_to_serializable,
    resolve_repo_relative,
    run_step_one,
)


def rect_from_bbox(bbox: dict[str, Any]) -> fitz.Rect:
    return fitz.Rect(float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"]))


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def default_manifest_path(pdf_path: Path, output_root: Path) -> Path:
    return output_root / pdf_path.stem / "manifest.json"


def find_page_entry(manifest: dict[str, Any], page_number: int) -> dict[str, Any]:
    for page in manifest.get("pages", []):
        n = page.get("page", page.get("page_number"))
        if n is not None and int(n) == page_number:
            return page
    raise SystemExit(f"Page {page_number} not found in manifest.")


def ensure_manifest(
    pdf_path: Path,
    output_root: Path,
    manifest_path: Path | None,
    *,
    rerun: bool,
) -> Path:
    path = manifest_path or default_manifest_path(pdf_path, output_root)
    if rerun or not path.is_file():
        return run_step_one(pdf_path, output_root)
    return path


def draw_bbox(
    page: fitz.Page,
    bbox: dict[str, Any] | None,
    *,
    color: tuple[float, float, float],
    width: float,
) -> None:
    if not bbox:
        return
    page.draw_rect(rect_from_bbox(bbox), color=color, width=width, overlay=True)


def draw_debug_overlay(
    *,
    pdf_path: Path,
    manifest_path: Path,
    page_number: int,
    output_path: Path,
    dpi: int,
    draw_drawing_rects: bool,
    draw_bottom_table: bool,
) -> None:
    manifest = load_json(manifest_path)
    page_entry = find_page_entry(manifest, page_number)

    doc = fitz.open(pdf_path)
    try:
        page_index = page_number - 1
        if page_index < 0 or page_index >= doc.page_count:
            raise SystemExit(f"Page {page_number} is outside PDF page count {doc.page_count}.")

        page = doc[page_index]
        if draw_drawing_rects:
            drawing_area_bbox = rect_from_bbox(page_entry["drawing_area_bbox"])
            for drawing in page.get_drawings(extended=False):
                if not drawing_intersects_rect(drawing, drawing_area_bbox):
                    continue
                rect = drawing.get("rect")
                if isinstance(rect, fitz.Rect):
                    draw_bbox(page, rect_to_serializable(rect), color=(0.0, 0.55, 0.2), width=0.5)

        draw_bbox(page, page_entry.get("drawing_area_bbox"), color=(1.0, 0.0, 0.0), width=2.0)
        if draw_bottom_table:
            bottom_table = page_entry.get("bottom_table") or {}
            draw_bbox(page, bottom_table.get("bbox"), color=(1.0, 0.55, 0.0), width=2.0)

        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(output_path)
    finally:
        doc.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render one PDF page and draw step-1 parser bounding boxes on the screenshot.",
    )
    parser.add_argument(
        "--pdf-file-path",
        required=True,
        help="Input PDF path.",
    )
    parser.add_argument(
        "--page",
        type=int,
        required=True,
        help="1-based page number to render.",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Existing step-1 manifest path. Defaults to output/pdf_drawings/<pdf-stem>/manifest.json.",
    )
    parser.add_argument(
        "--output-root",
        default="./output/pdf_drawings",
        help="Step-1 JSON output root used to find or generate the manifest.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output PNG path. Defaults to output/pdf_drawings/<pdf-stem>/debug/page_<n>_bbox.png.",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=144,
        help="Screenshot DPI.",
    )
    parser.add_argument(
        "--draw-drawing-rects",
        action="store_true",
        help="Also draw each exported drawing rect in green.",
    )
    parser.add_argument(
        "--draw-bottom-table",
        action="store_true",
        help="Also draw the parsed bottom table bbox in orange.",
    )
    parser.add_argument(
        "--rerun",
        action="store_true",
        help="Force regenerating step-1 JSON before rendering.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pdf_path = resolve_repo_relative(args.pdf_file_path)
    output_root = resolve_repo_relative(args.output_root)
    manifest_path = resolve_repo_relative(args.manifest) if args.manifest else None

    if not pdf_path.is_file():
        raise SystemExit(f"PDF not found: {pdf_path}")

    manifest = ensure_manifest(
        pdf_path,
        output_root,
        manifest_path,
        rerun=args.rerun,
    )
    output_path = (
        resolve_repo_relative(args.output)
        if args.output
        else output_root / pdf_path.stem / "debug" / f"page_{args.page:04d}_bbox.png"
    )

    draw_debug_overlay(
        pdf_path=pdf_path,
        manifest_path=manifest,
        page_number=args.page,
        output_path=output_path,
        dpi=args.dpi,
        draw_drawing_rects=args.draw_drawing_rects,
        draw_bottom_table=args.draw_bottom_table,
    )
    print(f"Wrote: {output_path.relative_to(REPO_ROOT) if output_path.is_relative_to(REPO_ROOT) else output_path}")


if __name__ == "__main__":
    main()
