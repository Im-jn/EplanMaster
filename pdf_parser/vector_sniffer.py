"""
Vector extraction and bbox-only spatial lookup for PDF drawing primitives.

Coordinates are stored in PyMuPDF page space: origin at the top-left, x grows
rightward, and y grows downward. Query helpers can still accept PDF user-space
boxes when ``coord_space="pdf"`` and a page height is available.
"""

from __future__ import annotations

import math
from numbers import Integral
from pathlib import Path
from typing import Any, Sequence

import fitz
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree


BBox = tuple[float, float, float, float]
Point = tuple[float, float]


def _fmt_num(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text if text and text != "-0" else "0"


def _pt(point: Any) -> Point:
    return (float(point.x), float(point.y))


def _bbox_from_points(points: Sequence[Point]) -> BBox:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_to_dict(bbox: BBox) -> dict[str, float]:
    x0, y0, x1, y1 = bbox
    return {"x0": float(x0), "y0": float(y0), "x1": float(x1), "y1": float(y1)}


def _coerce_bbox(bbox: Any) -> BBox:
    if isinstance(bbox, dict):
        return (float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"]))
    if isinstance(bbox, fitz.Rect):
        return (float(bbox.x0), float(bbox.y0), float(bbox.x1), float(bbox.y1))
    if len(bbox) != 4:
        raise ValueError("bbox must contain four values")
    x0, y0, x1, y1 = bbox
    return (float(x0), float(y0), float(x1), float(y1))


def _normalized_bbox(bbox: Any) -> BBox:
    x0, y0, x1, y1 = _coerce_bbox(bbox)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _code_line(p0: Point, p1: Point) -> str:
    return f"{_fmt_num(p0[0])} {_fmt_num(p0[1])} m\n{_fmt_num(p1[0])} {_fmt_num(p1[1])} l"


def _code_curve(p1: Point, p2: Point, p3: Point) -> str:
    return (
        f"{_fmt_num(p1[0])} {_fmt_num(p1[1])} "
        f"{_fmt_num(p2[0])} {_fmt_num(p2[1])} "
        f"{_fmt_num(p3[0])} {_fmt_num(p3[1])} c"
    )


def _code_rect(rect: fitz.Rect) -> str:
    return (
        f"{_fmt_num(rect.x0)} {_fmt_num(rect.y0)} "
        f"{_fmt_num(rect.width)} {_fmt_num(rect.height)} re"
    )


def _code_quad(points: Sequence[Point]) -> str:
    p0, p1, p2, p3 = points
    return (
        f"{_fmt_num(p0[0])} {_fmt_num(p0[1])} m\n"
        f"{_fmt_num(p1[0])} {_fmt_num(p1[1])} l\n"
        f"{_fmt_num(p2[0])} {_fmt_num(p2[1])} l\n"
        f"{_fmt_num(p3[0])} {_fmt_num(p3[1])} l\nh"
    )


def _cubic_value(a: float, b: float, c: float, d: float, t: float) -> float:
    u = 1.0 - t
    return (u**3 * a) + (3 * u**2 * t * b) + (3 * u * t**2 * c) + (t**3 * d)


def _cubic_axis_extrema(a: float, b: float, c: float, d: float) -> list[float]:
    # Derivative roots for a cubic Bezier axis.
    aa = -a + 3 * b - 3 * c + d
    bb = 2 * (a - 2 * b + c)
    cc = b - a
    roots: list[float] = []
    if abs(aa) < 1e-12:
        if abs(bb) >= 1e-12:
            roots.append(-cc / bb)
    else:
        disc = bb * bb - 4 * aa * cc
        if disc >= 0:
            sqrt_disc = math.sqrt(disc)
            roots.append((-bb + sqrt_disc) / (2 * aa))
            roots.append((-bb - sqrt_disc) / (2 * aa))
    return [t for t in roots if 0.0 < t < 1.0]


def _cubic_bbox(points: Sequence[Point]) -> BBox:
    p0, p1, p2, p3 = points
    ts = {0.0, 1.0}
    ts.update(_cubic_axis_extrema(p0[0], p1[0], p2[0], p3[0]))
    ts.update(_cubic_axis_extrema(p0[1], p1[1], p2[1], p3[1]))
    sampled = [
        (
            _cubic_value(p0[0], p1[0], p2[0], p3[0], t),
            _cubic_value(p0[1], p1[1], p2[1], p3[1], t),
        )
        for t in ts
    ]
    return _bbox_from_points(sampled)


def _copy_shape(shape: dict[str, Any]) -> dict[str, Any]:
    copied = dict(shape)
    copied["points"] = [tuple(p) for p in shape.get("points", [])]
    if "bbox" in shape:
        copied["bbox"] = dict(shape["bbox"])
    if "path_meta" in shape:
        copied["path_meta"] = dict(shape["path_meta"])
    return copied


def pdf_bbox_to_mupdf_xyxy(x0: float, y0: float, x1: float, y1: float, page_height_pt: float) -> BBox:
    """Convert PDF user-space bbox to PyMuPDF page-space bbox."""
    h = float(page_height_pt)
    return (float(x0), h - float(y1), float(x1), h - float(y0))


def normalize_query_bbox(
    bbox: tuple[float, float, float, float],
    *,
    coord_space: str,
    page_height_pt: float | None,
) -> BBox:
    x0, y0, x1, y1 = _coerce_bbox(bbox)
    if coord_space == "mupdf":
        return _normalized_bbox((x0, y0, x1, y1))
    if coord_space == "pdf":
        if page_height_pt is None:
            raise ValueError("page_height_pt is required when coord_space='pdf'")
        return _normalized_bbox(pdf_bbox_to_mupdf_xyxy(x0, y0, x1, y1, page_height_pt))
    raise ValueError("coord_space must be 'pdf' or 'mupdf'")


def expand_bbox_slack_xyxy(x0: float, y0: float, x1: float, y1: float, slack: float) -> BBox:
    """Symmetrically expand a bbox; slack=0.1 increases width and height by 10%."""
    if slack < 0:
        raise ValueError("slack must be non-negative")
    width = x1 - x0
    height = y1 - y0
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    half_w = width * (1.0 + slack) / 2.0
    half_h = height * (1.0 + slack) / 2.0
    return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)


def _point_cloud_signature(points: Sequence[Point]) -> list[float]:
    distances: list[float] = []
    for i, p0 in enumerate(points):
        for p1 in points[i + 1 :]:
            distances.append((p0[0] - p1[0]) ** 2 + (p0[1] - p1[1]) ** 2)
    return sorted(distances)


def _signatures_close(a: Sequence[float], b: Sequence[float], tol: float) -> bool:
    if len(a) != len(b):
        return False
    scale = max(max(a, default=0.0), max(b, default=0.0), 1.0)
    return all(abs(x - y) <= tol * scale for x, y in zip(a, b))


def _fit_rigid_transform(source: Sequence[Point], target: Sequence[Point]) -> tuple[float, float, Point, float]:
    if len(source) != len(target) or not source:
        raise ValueError("source and target point counts must match")

    sx = sum(p[0] for p in source) / len(source)
    sy = sum(p[1] for p in source) / len(source)
    tx = sum(p[0] for p in target) / len(target)
    ty = sum(p[1] for p in target) / len(target)

    cross = 0.0
    dot = 0.0
    for sp, tp in zip(source, target):
        x0 = sp[0] - sx
        y0 = sp[1] - sy
        x1 = tp[0] - tx
        y1 = tp[1] - ty
        dot += x0 * x1 + y0 * y1
        cross += x0 * y1 - y0 * x1

    angle = math.atan2(cross, dot)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    offset = (tx - (cos_a * sx - sin_a * sy), ty - (sin_a * sx + cos_a * sy))

    max_error = 0.0
    for sp, tp in zip(source, target):
        px = cos_a * sp[0] - sin_a * sp[1] + offset[0]
        py = sin_a * sp[0] + cos_a * sp[1] + offset[1]
        max_error = max(max_error, math.hypot(px - tp[0], py - tp[1]))

    return angle, 1.0, offset, max_error


def _fit_fixed_similarity_transform(
    source: Sequence[Point],
    target: Sequence[Point],
    *,
    rotation_degrees: float,
    scale: float,
) -> tuple[float, float, Point, float]:
    if len(source) != len(target) or not source:
        raise ValueError("source and target point counts must match")

    angle = math.radians(rotation_degrees)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)

    transformed = [
        (
            scale * (cos_a * point[0] - sin_a * point[1]),
            scale * (sin_a * point[0] + cos_a * point[1]),
        )
        for point in source
    ]
    dx = sum(target_point[0] - source_point[0] for source_point, target_point in zip(transformed, target)) / len(source)
    dy = sum(target_point[1] - source_point[1] for source_point, target_point in zip(transformed, target)) / len(source)

    max_error = 0.0
    for source_point, target_point in zip(transformed, target):
        max_error = max(max_error, math.hypot(source_point[0] + dx - target_point[0], source_point[1] + dy - target_point[1]))

    return angle, float(scale), (dx, dy), max_error


def _estimate_scale(source: Sequence[Point], target: Sequence[Point]) -> float:
    source_dist = math.sqrt(max(_point_cloud_signature(source), default=0.0))
    target_dist = math.sqrt(max(_point_cloud_signature(target), default=0.0))
    if source_dist < 1e-12:
        return 1.0
    return target_dist / source_dist


def _transform_points(points: Sequence[Point], *, rotation_degrees: float, scale: float, translation: Point) -> list[Point]:
    angle = math.radians(rotation_degrees)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    out: list[Point] = []
    for point in points:
        x = point[0] * scale
        y = point[1] * scale
        out.append((cos_a * x - sin_a * y + translation[0], sin_a * x + cos_a * y + translation[1]))
    return out


def _candidate_point_orders(points: Sequence[Point]) -> list[list[Point]]:
    pts = list(points)
    if len(pts) <= 2:
        return [pts, list(reversed(pts))]
    orders: list[list[Point]] = []
    for start in range(len(pts)):
        rotated = pts[start:] + pts[:start]
        orders.append(rotated)
        orders.append(list(reversed(rotated)))
    return orders


class vector_sniffer:
    """Extract page vectors, index their bboxes, and search by bbox or shape."""

    def __init__(self, pdf_path: str | Path):
        self.pdf_path = Path(pdf_path).expanduser().resolve()
        if not self.pdf_path.is_file():
            raise FileNotFoundError(f"PDF not found: {self.pdf_path}")

        self.doc = fitz.open(self.pdf_path)
        self.page: fitz.Page | None = None
        self.page_number: int | None = None
        self.page_height_pt: float | None = None
        self.page_vector: list[dict[str, Any]] = []
        self.tree: STRtree | None = None
        self.bbox_geometries: list[BaseGeometry] = []
        self._bbox_geom_to_index: dict[int, int] = {}

    def close(self) -> None:
        self.doc.close()

    def __enter__(self) -> vector_sniffer:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _require_page(self) -> None:
        if self.page is None or self.tree is None:
            raise RuntimeError("请使用goto函数选择一个页面")

    def goto(self, page_number: int) -> list[dict[str, Any]]:
        """Select a 1-based page and rebuild page_vector, bboxes, and STRtree."""
        if page_number < 1 or page_number > self.doc.page_count:
            raise ValueError(f"page_number out of range: {page_number}")

        self.page_number = int(page_number)
        self.page = self.doc[self.page_number - 1]
        self.page_height_pt = float(self.page.rect.height)
        self.extract_page_vectors()
        self.add_bboxes()
        self.build_strtree()
        return self.page_vector

    def extract_page_vectors(self) -> list[dict[str, Any]]:
        """Extract drawing primitives as source-like code plus point sets."""
        if self.page is None:
            raise RuntimeError("请使用goto函数选择一个页面")

        vectors: list[dict[str, Any]] = []
        drawings = self.page.get_drawings(extended=False)

        for path_index, path in enumerate(drawings):
            path_meta = {
                "path_index": path_index,
                "seqno": path.get("seqno"),
                "path_type": path.get("type"),
                "stroke_width": path.get("width"),
                "color": path.get("color"),
                "fill": path.get("fill"),
            }
            for item_index, item in enumerate(path.get("items") or []):
                shape = self._shape_from_item(item, path_meta, item_index)
                if shape is not None:
                    shape["index"] = len(vectors)
                    vectors.append(shape)

        self.page_vector = vectors
        return self.page_vector

    def _shape_from_item(
        self,
        item: tuple[Any, ...],
        path_meta: dict[str, Any],
        item_index: int,
    ) -> dict[str, Any] | None:
        op = item[0]
        try:
            if op == "l":
                _, p0_raw, p1_raw = item
                p0 = _pt(p0_raw)
                p1 = _pt(p1_raw)
                return {
                    "type": "line",
                    "op": "l",
                    "code": _code_line(p0, p1),
                    "points": [p0, p1],
                    "path_meta": {**path_meta, "item_index": item_index},
                }
            if op == "c":
                _, p0_raw, p1_raw, p2_raw, p3_raw = item
                p0 = _pt(p0_raw)
                p1 = _pt(p1_raw)
                p2 = _pt(p2_raw)
                p3 = _pt(p3_raw)
                return {
                    "type": "curve",
                    "op": "c",
                    "code": _code_curve(p1, p2, p3),
                    "points": [p0, p1, p2, p3],
                    "path_meta": {**path_meta, "item_index": item_index},
                }
            if op == "re":
                rect_raw = item[1]
                rect = rect_raw if isinstance(rect_raw, fitz.Rect) else fitz.Rect(rect_raw)
                points = [
                    (float(rect.x0), float(rect.y0)),
                    (float(rect.x1), float(rect.y0)),
                    (float(rect.x1), float(rect.y1)),
                    (float(rect.x0), float(rect.y1)),
                ]
                return {
                    "type": "rect",
                    "op": "re",
                    "code": _code_rect(rect),
                    "points": points,
                    "path_meta": {**path_meta, "item_index": item_index},
                }
            if op == "qu":
                _, quad = item
                points = [_pt(quad.ul), _pt(quad.ur), _pt(quad.lr), _pt(quad.ll)]
                return {
                    "type": "quad",
                    "op": "qu",
                    "code": _code_quad(points),
                    "points": points,
                    "path_meta": {**path_meta, "item_index": item_index},
                }
        except Exception:
            return None
        return None

    def add_bboxes(self) -> list[dict[str, Any]]:
        """Attach a tight bbox to each shape record."""
        if self.page is None:
            raise RuntimeError("请使用goto函数选择一个页面")

        for shape in self.page_vector:
            points = [tuple(p) for p in shape.get("points", [])]
            if shape.get("op") == "c" and len(points) == 4:
                bbox_value = _cubic_bbox(points)
            else:
                bbox_value = _bbox_from_points(points)
            shape["bbox"] = _bbox_to_dict(bbox_value)
        return self.page_vector

    def build_strtree(self) -> STRtree:
        """Build an STRtree over shape bbox rectangles."""
        if self.page is None:
            raise RuntimeError("请使用goto函数选择一个页面")

        self.bbox_geometries = []
        self._bbox_geom_to_index = {}
        for index, shape in enumerate(self.page_vector):
            bbox_value = _coerce_bbox(shape["bbox"])
            geom = box(*bbox_value)
            self.bbox_geometries.append(geom)
            self._bbox_geom_to_index[id(geom)] = index
        self.tree = STRtree(self.bbox_geometries)
        return self.tree

    def query_bbox(
        self,
        bbox: Any,
        slack: float = 0.0,
        *,
        coord_space: str = "mupdf",
    ) -> list[dict[str, Any]]:
        """Return shapes whose stored bbox is fully covered by the query bbox."""
        self._require_page()
        assert self.tree is not None
        assert self.page_height_pt is not None

        query = normalize_query_bbox(
            _coerce_bbox(bbox),
            coord_space=coord_space,
            page_height_pt=self.page_height_pt,
        )
        query = expand_bbox_slack_xyxy(*query, slack)
        region = box(*query)

        hits: list[dict[str, Any]] = []
        for candidate in self.tree.query(region, predicate="intersects"):
            index = self._index_from_tree_result(candidate)
            bbox_geom = self.bbox_geometries[index]
            if region.covers(bbox_geom):
                hits.append(_copy_shape(self.page_vector[index]))
        return hits

    def _index_from_tree_result(self, candidate: Any) -> int:
        if isinstance(candidate, Integral):
            return int(candidate)
        index = self._bbox_geom_to_index.get(id(candidate))
        if index is None:
            raise KeyError("STRtree returned an unknown geometry")
        return index

    def match_shape(
        self,
        shape: dict[str, Any],
        *,
        tolerance: float = 0.75,
        type_sensitive: bool = True,
        scale_range: tuple[float, float] = (1.0, 1.0),
        rotation_degrees: Sequence[int] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Find page shapes equal to the input after rotation, scaling, and translation.

        When rotation_degrees is provided, only those rotations are considered.
        Scaling is constrained by scale_range.
        """
        self._require_page()

        min_scale, max_scale = scale_range
        if min_scale <= 0 or max_scale <= 0 or min_scale > max_scale:
            raise ValueError("scale_range must be positive and ordered")

        query_shape = self.normalize_shape(shape)
        query_points = query_shape["points"]
        matches: list[dict[str, Any]] = []

        for candidate in self.page_vector:
            if type_sensitive and candidate.get("type") != query_shape.get("type"):
                continue
            candidate_points = [tuple(p) for p in candidate.get("points", [])]
            if len(candidate_points) != len(query_points):
                continue

            estimated_scale = _estimate_scale(query_points, candidate_points)
            if estimated_scale < min_scale - 1e-9 or estimated_scale > max_scale + 1e-9:
                continue

            best: tuple[float, float, Point, float] | None = None
            for ordered_points in _candidate_point_orders(candidate_points):
                if rotation_degrees is None:
                    transform = _fit_rigid_transform(query_points, ordered_points)
                    if best is None or transform[3] < best[3]:
                        best = transform
                else:
                    for angle in rotation_degrees:
                        transform = _fit_fixed_similarity_transform(
                            query_points,
                            ordered_points,
                            rotation_degrees=angle,
                            scale=estimated_scale,
                        )
                        if best is None or transform[3] < best[3]:
                            best = transform

            if best is not None and best[3] <= tolerance:
                angle_degrees = math.degrees(best[0])
                if rotation_degrees is not None:
                    angle_degrees = round(angle_degrees / 90.0) * 90
                item = _copy_shape(candidate)
                item["match"] = {
                    "rotation_degrees": angle_degrees,
                    "scale": best[1],
                    "translation": {"x": best[2][0], "y": best[2][1]},
                    "max_error": best[3],
                    "tolerance": tolerance,
                }
                matches.append(item)

        return matches

    def normalize_shape(self, shape: dict[str, Any]) -> dict[str, Any]:
        """Normalize a shape input to the internal record format."""
        if "points" not in shape:
            raise ValueError("shape must include points")
        points = [(float(p[0]), float(p[1])) for p in shape["points"]]
        normalized = {
            "type": shape.get("type"),
            "op": shape.get("op"),
            "code": shape.get("code", ""),
            "points": points,
        }
        if "bbox" in shape:
            normalized["bbox"] = _bbox_to_dict(_normalized_bbox(shape["bbox"]))
        elif shape.get("op") == "c" and len(points) == 4:
            normalized["bbox"] = _bbox_to_dict(_cubic_bbox(points))
        else:
            normalized["bbox"] = _bbox_to_dict(_bbox_from_points(points))
        return normalized

    def compare_shape_groups(
        self,
        target_shapes: Sequence[dict[str, Any]],
        candidate_shapes: Sequence[dict[str, Any]],
        *,
        rotation_degrees: float,
        scale: float,
        translation: Point,
        tolerance: float = 0.75,
        type_sensitive: bool = True,
    ) -> dict[str, Any]:
        """Compare a transformed target shape group with a candidate group."""
        transformed_targets: list[dict[str, Any]] = []
        for shape in target_shapes:
            points = [(float(point[0]), float(point[1])) for point in shape.get("points", [])]
            transformed_targets.append(
                {
                    "type": shape.get("type"),
                    "op": shape.get("op"),
                    "code": shape.get("code", ""),
                    "points": _transform_points(
                        points,
                        rotation_degrees=rotation_degrees,
                        scale=scale,
                        translation=translation,
                    ),
                }
            )

        unused_candidate_indexes = set(range(len(candidate_shapes)))
        pairings: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []

        for target_index, target in enumerate(transformed_targets):
            best: tuple[int, float] | None = None
            target_points = target["points"]
            for candidate_index in list(unused_candidate_indexes):
                candidate = candidate_shapes[candidate_index]
                if type_sensitive and candidate.get("type") != target.get("type"):
                    continue
                candidate_points = [(float(point[0]), float(point[1])) for point in candidate.get("points", [])]
                if len(candidate_points) != len(target_points):
                    continue
                for ordered_candidate_points in _candidate_point_orders(candidate_points):
                    error = max(
                        math.hypot(target_point[0] - candidate_point[0], target_point[1] - candidate_point[1])
                        for target_point, candidate_point in zip(target_points, ordered_candidate_points)
                    )
                    if best is None or error < best[1]:
                        best = (candidate_index, error)

            if best is not None and best[1] <= tolerance:
                unused_candidate_indexes.remove(best[0])
                pairings.append(
                    {
                        "target_index": target_index,
                        "candidate_index": best[0],
                        "target_code": target.get("code"),
                        "candidate_code": candidate_shapes[best[0]].get("code"),
                        "max_error": best[1],
                    }
                )
            else:
                missing.append({"target_index": target_index, "target_code": target.get("code")})

        extra = [
            {"candidate_index": index, "candidate_code": candidate_shapes[index].get("code")}
            for index in sorted(unused_candidate_indexes)
        ]
        return {
            "matched": not missing and not extra,
            "target_count": len(target_shapes),
            "candidate_count": len(candidate_shapes),
            "pairings": pairings,
            "missing": missing,
            "extra": extra,
        }
