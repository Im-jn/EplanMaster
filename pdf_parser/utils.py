from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable


BBox = tuple[float, float, float, float]
Point = tuple[float, float]

REPO_ROOT = Path(__file__).resolve().parents[1]


def resolve_repo_relative(path_str: str) -> Path:
    path = Path(path_str).expanduser()
    if path.is_absolute():
        return path.resolve()
    cwd_candidate = (Path.cwd() / path).resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    return (REPO_ROOT / path).resolve()


def coerce_bbox(bbox: Any) -> BBox:
    if isinstance(bbox, dict):
        return (float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"]))
    x0, y0, x1, y1 = bbox
    return (float(x0), float(y0), float(x1), float(y1))


def bbox_to_dict(bbox: BBox) -> dict[str, float]:
    x0, y0, x1, y1 = bbox
    return {"x0": float(x0), "y0": float(y0), "x1": float(x1), "y1": float(y1)}


def bbox_center(bbox: Any) -> Point:
    x0, y0, x1, y1 = coerce_bbox(bbox)
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def bbox_gap(a: Any, b: Any) -> float:
    ax0, ay0, ax1, ay1 = coerce_bbox(a)
    bx0, by0, bx1, by1 = coerce_bbox(b)
    dx = max(bx0 - ax1, ax0 - bx1, 0.0)
    dy = max(by0 - ay1, ay0 - by1, 0.0)
    return math.hypot(dx, dy)


def bbox_center_distance(a: Any, b: Any) -> float:
    ac = bbox_center(a)
    bc = bbox_center(b)
    return math.hypot(ac[0] - bc[0], ac[1] - bc[1])


def select_anchor_shapes(shapes: list[dict[str, Any]], *, adjacency_gap: float = 0.75) -> list[dict[str, Any]]:
    """Select up to two distant, non-adjacent shapes as anchors."""
    if not shapes:
        return []
    if len(shapes) == 1:
        return [shapes[0]]

    best_pair: tuple[float, dict[str, Any], dict[str, Any]] | None = None

    for i, left in enumerate(shapes):
        for right in shapes[i + 1 :]:
            distance = bbox_center_distance(left["bbox"], right["bbox"])
            if bbox_gap(left["bbox"], right["bbox"]) <= adjacency_gap:
                continue
            if best_pair is None or distance > best_pair[0]:
                best_pair = (distance, left, right)

    if best_pair is None:
        return [shapes[0]]
    return [best_pair[1], best_pair[2]]


def bbox_from_shapes(shapes: Iterable[dict[str, Any]]) -> BBox:
    xs: list[float] = []
    ys: list[float] = []
    for shape in shapes:
        x0, y0, x1, y1 = coerce_bbox(shape["bbox"])
        xs.extend([x0, x1])
        ys.extend([y0, y1])
    if not xs:
        raise ValueError("shape group is empty")
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_corners(bbox: BBox) -> list[Point]:
    x0, y0, x1, y1 = bbox
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def bbox_from_points(points: Iterable[Point]) -> BBox:
    pts = list(points)
    if not pts:
        raise ValueError("point list is empty")
    xs = [point[0] for point in pts]
    ys = [point[1] for point in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def relative_bbox(inner: Any, outer: Any) -> dict[str, float]:
    ix0, iy0, ix1, iy1 = coerce_bbox(inner)
    ox0, oy0, ox1, oy1 = coerce_bbox(outer)
    width = ox1 - ox0
    height = oy1 - oy0
    if abs(width) < 1e-12 or abs(height) < 1e-12:
        raise ValueError("outer bbox must have non-zero width and height")
    return {
        "x0": (ix0 - ox0) / width,
        "y0": (iy0 - oy0) / height,
        "x1": (ix1 - ox0) / width,
        "y1": (iy1 - oy0) / height,
    }


def transform_point(point: Point, *, rotation_degrees: float, scale: float, translation: Point) -> Point:
    angle = math.radians(rotation_degrees)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    x = point[0] * scale
    y = point[1] * scale
    return (
        cos_a * x - sin_a * y + translation[0],
        sin_a * x + cos_a * y + translation[1],
    )


def transform_bbox(bbox: Any, *, rotation_degrees: float, scale: float, translation: Point) -> BBox:
    return bbox_from_points(
        transform_point(point, rotation_degrees=rotation_degrees, scale=scale, translation=translation)
        for point in bbox_corners(coerce_bbox(bbox))
    )
