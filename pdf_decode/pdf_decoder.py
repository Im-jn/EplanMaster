"""
Step-1 PDF page layout extraction: drawing-area detection, bottom table parsing,
and compact manifest export under output/pdf_drawings/.

Run from repo root, for example:

    python pdf_parser/main.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fitz


REPO_ROOT = Path(__file__).resolve().parents[1]


def resolve_repo_relative(path_str: str) -> Path:
    """Resolve ./foo paths from cwd first, then from repo root."""
    path = Path(path_str).expanduser()
    if path.is_absolute():
        return path.resolve()
    cwd_candidate = (Path.cwd() / path).resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    return (REPO_ROOT / path).resolve()


def drawing_point_bounds(drawings: list[dict[str, Any]]) -> fitz.Rect | None:
    """Union of all geometry bounds in PyMuPDF page space (origin top-left, y downward)."""
    xs: list[float] = []
    ys: list[float] = []

    for path in drawings:
        for item in path.get("items") or []:
            op = item[0]
            if op == "l":
                _, p0, p1 = item
                for p in (p0, p1):
                    xs.append(float(p.x))
                    ys.append(float(p.y))
            elif op == "c":
                _, p0, p1, p2, p3 = item
                for p in (p0, p1, p2, p3):
                    xs.append(float(p.x))
                    ys.append(float(p.y))
            elif op == "re":
                rect = item[1]
                r = rect if isinstance(rect, fitz.Rect) else fitz.Rect(rect)
                xs.extend([float(r.x0), float(r.x1)])
                ys.extend([float(r.y0), float(r.y1)])
            elif op == "qu":
                _, quad = item
                for p in (quad.ul, quad.ur, quad.lr, quad.ll):
                    xs.append(float(p.x))
                    ys.append(float(p.y))

    if not xs:
        return None
    return fitz.Rect(min(xs), min(ys), max(xs), max(ys))


def extract_axis_aligned_segments(
    drawings: list[dict[str, Any]],
    *,
    axis_tol: float = 0.75,
) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    """Return horizontal and vertical line candidates in MuPDF page space."""
    horizontal: list[dict[str, float]] = []
    vertical: list[dict[str, float]] = []

    def add_line(x0: float, y0: float, x1: float, y1: float, width: float | None) -> None:
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        if dy <= axis_tol and dx > axis_tol:
            horizontal.append(
                {
                    "x0": min(x0, x1),
                    "y": (y0 + y1) / 2.0,
                    "x1": max(x0, x1),
                    "length": dx,
                    "width": float(width or 0.0),
                }
            )
        elif dx <= axis_tol and dy > axis_tol:
            vertical.append(
                {
                    "x": (x0 + x1) / 2.0,
                    "y0": min(y0, y1),
                    "y1": max(y0, y1),
                    "length": dy,
                    "width": float(width or 0.0),
                }
            )

    for path in drawings:
        width = path.get("width")
        for item in path.get("items") or []:
            op = item[0]
            if op == "l":
                _, p0, p1 = item
                add_line(float(p0.x), float(p0.y), float(p1.x), float(p1.y), width)
            elif op == "re":
                rect = item[1]
                r = rect if isinstance(rect, fitz.Rect) else fitz.Rect(rect)
                add_line(float(r.x0), float(r.y0), float(r.x1), float(r.y0), width)
                add_line(float(r.x1), float(r.y0), float(r.x1), float(r.y1), width)
                add_line(float(r.x1), float(r.y1), float(r.x0), float(r.y1), width)
                add_line(float(r.x0), float(r.y1), float(r.x0), float(r.y0), width)

    return horizontal, vertical


def segment_covers_x_range(segment: dict[str, float], x0: float, x1: float, *, tol: float) -> bool:
    return segment["x0"] <= x0 + tol and segment["x1"] >= x1 - tol


def detect_inner_drawing_area_bbox(
    page_rect: fitz.Rect,
    drawings: list[dict[str, Any]],
    *,
    axis_tol: float = 0.75,
    edge_band_ratio: float = 0.2,
    min_border_span_ratio: float = 0.72,
    title_separator_min_y_ratio: float = 0.55,
) -> tuple[fitz.Rect | None, dict[str, Any]]:
    """
    Detect the EPLAN inner drawing area.

    The template has an outer sheet border, an inner page border, and a bottom title block.
    We choose the innermost left/right/top border lines near the page edges, then choose
    the first full-width horizontal separator in the lower half as the drawing area's bottom.
    """
    page_width = float(page_rect.width)
    page_height = float(page_rect.height)
    horizontal, vertical = extract_axis_aligned_segments(drawings, axis_tol=axis_tol)

    min_vertical_span = page_height * min_border_span_ratio
    left_band_max = page_rect.x0 + page_width * edge_band_ratio
    right_band_min = page_rect.x1 - page_width * edge_band_ratio

    left_candidates = [
        s
        for s in vertical
        if s["length"] >= min_vertical_span and page_rect.x0 <= s["x"] <= left_band_max
    ]
    right_candidates = [
        s
        for s in vertical
        if s["length"] >= min_vertical_span and right_band_min <= s["x"] <= page_rect.x1
    ]

    meta: dict[str, Any] = {
        "method": "inner_drawing_area_frame",
        "axis_tol": axis_tol,
        "horizontal_segments": len(horizontal),
        "vertical_segments": len(vertical),
        "left_candidates": len(left_candidates),
        "right_candidates": len(right_candidates),
    }

    if not left_candidates or not right_candidates:
        meta["note"] = "missing long left/right border candidates"
        return None, meta

    left = max(left_candidates, key=lambda s: s["x"])
    right = min(right_candidates, key=lambda s: s["x"])
    x0 = float(left["x"])
    x1 = float(right["x"])

    if x1 <= x0 or (x1 - x0) < page_width * 0.5:
        meta.update({"left_x": x0, "right_x": x1, "note": "side borders are not plausible"})
        return None, meta

    span_tol = max(axis_tol * 2.0, page_width * 0.01)
    top_band_max = page_rect.y0 + page_height * edge_band_ratio
    full_width_lines = [
        s
        for s in horizontal
        if segment_covers_x_range(s, x0, x1, tol=span_tol)
    ]
    top_candidates = [s for s in full_width_lines if page_rect.y0 <= s["y"] <= top_band_max]

    meta.update(
        {
            "full_width_horizontal_candidates": len(full_width_lines),
            "top_candidates": len(top_candidates),
            "left_x": x0,
            "right_x": x1,
        }
    )

    if not top_candidates:
        meta["note"] = "missing full-width top border candidate"
        return None, meta

    y0 = float(max(top_candidates, key=lambda s: s["y"])["y"])
    title_min_y = page_rect.y0 + page_height * title_separator_min_y_ratio
    bottom_candidates = [
        s
        for s in full_width_lines
        if title_min_y <= s["y"] <= page_rect.y1 + axis_tol and s["y"] > y0 + page_height * 0.2
    ]
    meta["bottom_candidates"] = len(bottom_candidates)

    if bottom_candidates:
        y1 = float(min(bottom_candidates, key=lambda s: s["y"])["y"])
    else:
        y1 = min(float(left["y1"]), float(right["y1"]), float(page_rect.y1))
        meta["note"] = "missing title separator; using inner border bottom"

    if y1 <= y0 or (y1 - y0) < page_height * 0.25:
        meta.update({"top_y": y0, "bottom_y": y1, "note": "detected frame height is not plausible"})
        return None, meta

    meta.update({"top_y": y0, "bottom_y": y1, "note": "detected inner drawing area"})
    return fitz.Rect(x0, y0, x1, y1), meta


def geometry_nonnegative_margin(rect: fitz.Rect, geom: fitz.Rect) -> tuple[float, float, float, float]:
    """Margins from geom edges to rect edges (how far we could shrink rect inward before clipping geom)."""
    return (
        geom.x0 - rect.x0,
        rect.x1 - geom.x1,
        geom.y0 - rect.y0,
        rect.y1 - geom.y1,
    )


def tighten_geometry_bbox_iterative(
    page_rect: fitz.Rect,
    geom_bounds: fitz.Rect,
    *,
    step_pt: float = 1.0,
) -> tuple[fitz.Rect, dict[str, Any]]:
    """
    Start from page_rect and shrink left/right/top/bottom in rounds until moving any edge
    further would pull it past geom_bounds (i.e.「线会被裁切」).

    Stopping criterion equivalent to: tight rect equals the minimal rect containing geom_bounds,
    intersected with page_rect if geom extends outside page (clamp).
    """
    # Clamp geometry to page — drawings outside mediabox are rare but possible.
    g = geom_bounds & page_rect
    if g.is_empty:
        g = geom_bounds

    cur = fitz.Rect(page_rect)
    rounds = 0
    moves: list[str] = []

    left_m, right_m, top_m, bot_m = geometry_nonnegative_margin(cur, g)

    while True:
        progressed = False
        rounds += 1
        left_m, right_m, top_m, bot_m = geometry_nonnegative_margin(cur, g)

        # Try move each inward edge toward geometry by `step_pt`, without crossing geom.
        if left_m >= step_pt:
            cur.x0 += step_pt
            moves.append(f"L+{step_pt}")
            progressed = True
        elif left_m > 0:
            cur.x0 += left_m
            moves.append(f"L+{left_m:.4f}")
            progressed = True

        if right_m >= step_pt:
            cur.x1 -= step_pt
            moves.append(f"R+{step_pt}")
            progressed = True
        elif right_m > 0:
            cur.x1 -= right_m
            moves.append(f"R+{right_m:.4f}")
            progressed = True

        if top_m >= step_pt:
            cur.y0 += step_pt
            moves.append(f"T+{step_pt}")
            progressed = True
        elif top_m > 0:
            cur.y0 += top_m
            moves.append(f"T+{top_m:.4f}")
            progressed = True

        if bot_m >= step_pt:
            cur.y1 -= step_pt
            moves.append(f"B+{step_pt}")
            progressed = True
        elif bot_m > 0:
            cur.y1 -= bot_m
            moves.append(f"B+{bot_m:.4f}")
            progressed = True

        if not progressed:
            break

    meta = {
        "iterations": rounds,
        "step_pt": step_pt,
        "edge_moves_sample": moves[:80],
        "edge_moves_total": len(moves),
    }
    return cur, meta


def rect_to_serializable(rect: fitz.Rect) -> dict[str, float]:
    return {
        "x0": float(rect.x0),
        "y0": float(rect.y0),
        "x1": float(rect.x1),
        "y1": float(rect.y1),
        "width": float(rect.width),
        "height": float(rect.height),
    }


def bbox_compact_mupdf(rect: fitz.Rect) -> dict[str, float]:
    """Minimal bbox for manifest export (PyMuPDF page space)."""
    return {
        "x0": round(float(rect.x0), 3),
        "y0": round(float(rect.y0), 3),
        "x1": round(float(rect.x1), 3),
        "y1": round(float(rect.y1), 3),
    }


def detection_note(
    *,
    geom: fitz.Rect | None,
    detected_rect: fitz.Rect | None,
    used_fallback_iter: bool,
) -> str | None:
    """Short single string only when result is not the default frame detector success."""
    if geom is None:
        return "no_vector_geometry_using_page_rect"
    if detected_rect is not None:
        return None
    if used_fallback_iter:
        return "frame_detect_failed_using_min_geometry_bbox"
    return None


def drawing_intersects_rect(path: dict[str, Any], rect: fitz.Rect) -> bool:
    """Rough filter: path bbox intersects rect."""
    pr = path.get("rect")
    if isinstance(pr, fitz.Rect):
        return not (pr & rect).is_empty
    gb = drawing_point_bounds([path])
    return gb is not None and not (gb & rect).is_empty


def count_drawings_intersecting_rect(drawings: list[dict[str, Any]], bbox: fitz.Rect) -> int:
    return sum(1 for drawing in drawings if drawing_intersects_rect(drawing, bbox))


def merge_close_values(values: list[float], *, tolerance: float = 1.5) -> list[float]:
    if not values:
        return []
    merged: list[float] = []
    for value in sorted(values):
        if not merged or abs(value - merged[-1]) > tolerance:
            merged.append(value)
        else:
            merged[-1] = (merged[-1] + value) / 2.0
    return merged


def markdown_escape_cell(value: str) -> str:
    return " ".join(value.replace("|", r"\|").split())


def words_in_rect(page: fitz.Page, rect: fitz.Rect) -> list[tuple[float, float, float, float, str]]:
    words = page.get_text("words", clip=rect)
    return [
        (float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4]))
        for w in words
        if len(w) >= 5
    ]


def text_from_words(words: list[tuple[float, float, float, float, str]]) -> str:
    if not words:
        return ""
    ordered = sorted(words, key=lambda w: (round((w[1] + w[3]) / 2.0, 1), w[0]))
    lines: list[list[tuple[float, float, float, float, str]]] = []
    line_tol = 3.0
    for word in ordered:
        cy = (word[1] + word[3]) / 2.0
        if not lines:
            lines.append([word])
            continue
        last_cy = sum((w[1] + w[3]) / 2.0 for w in lines[-1]) / len(lines[-1])
        if abs(cy - last_cy) <= line_tol:
            lines[-1].append(word)
        else:
            lines.append([word])
    return "<br>".join(" ".join(w[4] for w in sorted(line, key=lambda w: w[0])) for line in lines)


def infer_bottom_table_bbox(page_rect: fitz.Rect, drawing_area: fitz.Rect) -> fitz.Rect | None:
    if drawing_area.y1 >= page_rect.y1 - 2.0:
        return None
    return fitz.Rect(drawing_area.x0, drawing_area.y1, drawing_area.x1, page_rect.y1)


def parse_bottom_table(page: fitz.Page, drawings: list[dict[str, Any]], table_rect: fitz.Rect) -> dict[str, Any]:
    horizontal, vertical = extract_axis_aligned_segments(drawings)
    min_grid_segment = 20.0
    edge_tol = 2.0

    row_coords = [table_rect.y0, table_rect.y1]
    col_coords = [table_rect.x0, table_rect.x1]

    for segment in horizontal:
        if (
            table_rect.y0 - edge_tol <= segment["y"] <= table_rect.y1 + edge_tol
            and segment["length"] >= min_grid_segment
            and segment["x1"] >= table_rect.x0 - edge_tol
            and segment["x0"] <= table_rect.x1 + edge_tol
        ):
            row_coords.append(float(segment["y"]))

    for segment in vertical:
        overlap = min(segment["y1"], table_rect.y1) - max(segment["y0"], table_rect.y0)
        if (
            table_rect.x0 - edge_tol <= segment["x"] <= table_rect.x1 + edge_tol
            and segment["length"] >= 10.0
            and overlap >= 10.0
        ):
            col_coords.append(float(segment["x"]))

    rows = merge_close_values(row_coords)
    cols = merge_close_values(col_coords)
    words = words_in_rect(page, table_rect)

    if len(rows) < 2 or len(cols) < 2:
        text = text_from_words(words)
        markdown = text.replace("<br>", "\n")
        return {
            "bbox": rect_to_serializable(table_rect),
            "markdown": markdown,
            "parse_meta": {
                "method": "plain_text_fallback",
                "word_count": len(words),
                "row_count": 0,
                "column_count": 0,
            },
        }

    grid: list[list[str]] = []
    for y0, y1 in zip(rows, rows[1:]):
        row: list[str] = []
        for col_index, (x0, x1) in enumerate(zip(cols, cols[1:])):
            cell_rect = fitz.Rect(x0, y0, x1, y1)
            is_last_col = col_index == len(cols) - 2
            cell_words = [
                word
                for word in words
                if cell_rect.x0 <= (word[0] + word[2]) / 2.0 < cell_rect.x1 + (edge_tol if is_last_col else 0.0)
                and cell_rect.y0 <= (word[1] + word[3]) / 2.0 < cell_rect.y1
            ]
            row.append(markdown_escape_cell(text_from_words(cell_words)))
        grid.append(row)

    non_empty_cols = [
        idx
        for idx in range(len(cols) - 1)
        if any(row[idx] for row in grid)
    ]
    if non_empty_cols:
        grid = [[row[idx] for idx in non_empty_cols] for row in grid]

    headers = [f"C{idx + 1}" for idx in range(len(grid[0]) if grid else 0)]
    markdown_lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    markdown_lines.extend("| " + " | ".join(row) + " |" for row in grid if any(row))

    return {
        "bbox": rect_to_serializable(table_rect),
        "markdown": "\n".join(markdown_lines),
        "parse_meta": {
            "method": "line_grid",
            "word_count": len(words),
            "row_count": len(rows) - 1,
            "column_count": len(cols) - 1,
            "non_empty_column_count": len(non_empty_cols),
        },
    }


def run_step_one(pdf_path: Path, output_root: Path) -> Path:
    """Run extraction and write a compact manifest; returns manifest path."""
    pdf_path = pdf_path.resolve()
    out_dir = output_root / pdf_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    for old_page_file in out_dir.glob("page_*.json"):
        old_page_file.unlink()

    doc = fitz.open(pdf_path)
    try:
        manifest_pages: list[dict[str, Any]] = []

        for page_index in range(doc.page_count):
            page = doc[page_index]
            page_no = page_index + 1
            drawings = page.get_drawings(extended=False)

            geom = drawing_point_bounds(drawings)
            tight_rect: fitz.Rect
            detected_rect: fitz.Rect | None
            used_fallback = False

            if geom is None:
                tight_rect = fitz.Rect(page.rect)
                detected_rect = None
            else:
                detected_rect, _detect_meta = detect_inner_drawing_area_bbox(page.rect, drawings)
                if detected_rect is None:
                    tight_rect, _fb = tighten_geometry_bbox_iterative(page.rect, geom)
                    used_fallback = True
                else:
                    tight_rect = detected_rect

            note = detection_note(geom=geom, detected_rect=detected_rect, used_fallback_iter=used_fallback)

            drawing_area_count = count_drawings_intersecting_rect(drawings, tight_rect)
            table_rect = infer_bottom_table_bbox(page.rect, tight_rect)
            bottom_table = parse_bottom_table(page, drawings, table_rect) if table_rect else None

            page_row: dict[str, Any] = {
                "page": page_no,
                "drawing_area_bbox": bbox_compact_mupdf(tight_rect),
                "drawing_counts": {
                    "raw": len(drawings),
                    "inside_drawing_area": drawing_area_count,
                },
            }
            if note:
                page_row["drawing_area_note"] = note
            if bottom_table:
                page_row["bottom_table"] = bottom_table

            manifest_pages.append(page_row)

        manifest = {
            "pdf": str(pdf_path),
            "page_count": doc.page_count,
            "coord_space": "mupdf_page_top_left_y_down",
            "pages": manifest_pages,
        }
        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest_path
    finally:
        doc.close()
