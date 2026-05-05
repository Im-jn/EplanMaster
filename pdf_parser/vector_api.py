from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from utils import bbox_from_shapes, bbox_to_dict, resolve_repo_relative, select_anchor_shapes, transform_bbox
from vector_sniffer import vector_sniffer


def _mupdf_bbox_to_pdf(bbox: dict[str, float] | tuple[float, float, float, float], page_height: float) -> dict[str, float]:
    if isinstance(bbox, dict):
        x0, y0, x1, y1 = float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"])
    else:
        x0, y0, x1, y1 = bbox
    pdf_y0 = page_height - y1
    pdf_y1 = page_height - y0
    return {"x0": x0, "y0": pdf_y0, "x1": x1, "y1": pdf_y1, "width": x1 - x0, "height": pdf_y1 - pdf_y0}


def _transform_key(match_info: dict[str, Any], *, translation_precision: float = 1.0) -> tuple[int, int, int, int]:
    translation = match_info["translation"]
    return (
        int(round(float(match_info["rotation_degrees"]) / 90.0)) % 4,
        int(round(float(match_info["scale"]) * 1000)),
        int(round(float(translation["x"]) / translation_precision)),
        int(round(float(translation["y"]) / translation_precision)),
    )


def _query_selected_shapes(
    sniffer: vector_sniffer,
    bbox: dict[str, Any],
    *,
    slack: float,
    coord_space: str = "pdf",
) -> list[dict[str, Any]]:
    return sniffer.query_bbox(
        bbox=(float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"])),
        slack=slack,
        coord_space=coord_space,
    )


def _match_groups(sniffer: vector_sniffer, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not hits:
        return []

    target_group_bbox = bbox_from_shapes(hits)
    anchor_shapes = select_anchor_shapes(hits)
    if not anchor_shapes:
        return []

    anchor_matches_by_index: dict[int, list[dict[str, Any]]] = {}
    for anchor_shape in anchor_shapes:
        anchor_matches_by_index[anchor_shape["index"]] = sniffer.match_shape(
            anchor_shape,
            tolerance=0.75,
            scale_range=(0.9, 1.1),
            rotation_degrees=(0, 90, 180, 270),
        )

    primary_anchor = anchor_shapes[0]
    secondary_anchor = anchor_shapes[1] if len(anchor_shapes) > 1 else None
    secondary_keys: set[tuple[int, int, int, int]] = set()
    if secondary_anchor is not None:
        secondary_keys = {
            _transform_key(match["match"])
            for match in anchor_matches_by_index[secondary_anchor["index"]]
        }

    matched_groups: list[dict[str, Any]] = []
    seen_bboxes: set[tuple[int, int, int, int]] = set()
    for anchor_match in anchor_matches_by_index[primary_anchor["index"]]:
        match_info = anchor_match["match"]
        if secondary_anchor is not None and _transform_key(match_info) not in secondary_keys:
            continue

        translation = (float(match_info["translation"]["x"]), float(match_info["translation"]["y"]))
        predicted_bbox = transform_bbox(
            target_group_bbox,
            rotation_degrees=float(match_info["rotation_degrees"]),
            scale=float(match_info["scale"]),
            translation=translation,
        )
        candidate_shapes = sniffer.query_bbox(
            bbox=predicted_bbox,
            slack=0.001,
            coord_space="mupdf",
        )
        group_match = sniffer.compare_shape_groups(
            hits,
            candidate_shapes,
            rotation_degrees=float(match_info["rotation_degrees"]),
            scale=float(match_info["scale"]),
            translation=translation,
            tolerance=0.75,
        )
        if not group_match["matched"]:
            continue

        bbox_key = tuple(round(value * 1000) for value in predicted_bbox)
        if bbox_key in seen_bboxes:
            continue
        seen_bboxes.add(bbox_key)
        matched_groups.append(
            {
                "bbox_mupdf": bbox_to_dict(predicted_bbox),
                "shape_count": len(candidate_shapes),
                "anchor": {
                    "source_index": primary_anchor["index"],
                    "matched_index": anchor_match["index"],
                    "transform": match_info,
                },
            }
        )

    return matched_groups


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    pdf_path = resolve_repo_relative(str(payload["pdf_path"]))
    page = int(payload["page"])
    bbox = payload["bbox"]
    mode = str(payload.get("mode", "match"))
    coord_space = str(payload.get("coord_space", "pdf"))
    search_scope = str(payload.get("search_scope", "global"))

    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    with vector_sniffer(pdf_path) as sniffer:
        sniffer.goto(page)
        page_height = float(sniffer.page_height_pt or 0.0)
        hits = _query_selected_shapes(
            sniffer,
            bbox,
            slack=float(payload.get("select_slack", 0.0)),
            coord_space=coord_space,
        )
        selected_bbox_mupdf = bbox_from_shapes(hits) if hits else None

        result: dict[str, Any] = {
            "page": page,
            "selected_shape_count": len(hits),
            "selected_bbox_pdf": _mupdf_bbox_to_pdf(selected_bbox_mupdf, page_height) if selected_bbox_mupdf else None,
            "selected_shapes": hits,
        }

        if mode == "select":
            return result

        all_matches: list[dict[str, Any]] = []
        if search_scope == "current":
            search_pages = [page]
        else:
            search_pages = list(range(1, sniffer.doc.page_count + 1))

        for match_page in search_pages:
            sniffer.goto(match_page)
            match_page_height = float(sniffer.page_height_pt or 0.0)
            for match in _match_groups(sniffer, hits):
                all_matches.append(
                    {
                        **match,
                        "page_number": match_page,
                        "bbox_pdf": _mupdf_bbox_to_pdf(match["bbox_mupdf"], match_page_height),
                    }
                )

        result["searched_page_count"] = len(search_pages)
        result["matches"] = all_matches
        return result


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
        print(json.dumps({"ok": True, "result": handle(payload)}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
