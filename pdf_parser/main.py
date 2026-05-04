from __future__ import annotations

import argparse
import json

from utils import bbox_from_shapes, bbox_to_dict, relative_bbox, resolve_repo_relative, select_anchor_shapes, transform_bbox
from vector_sniffer import vector_sniffer


def transform_key(match_info: dict[str, object], *, translation_precision: int = 1) -> tuple[int, int, int, int]:
    translation = match_info["translation"]
    assert isinstance(translation, dict)
    return (
        int(round(float(match_info["rotation_degrees"]) / 90.0)) % 4,
        int(round(float(match_info["scale"]) * 1000)),
        int(round(float(translation["x"]) / translation_precision)),
        int(round(float(translation["y"]) / translation_precision)),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract page vectors and query them through vector_sniffer.",
    )
    parser.add_argument(
        "--pdf-file-path",
        dest="pdf_file_path",
        default="./data/eplans/1VLG100537_Standard_Documentation.pdf",
        help="Input PDF path.",
    )
    parser.add_argument(
        "--page",
        dest="page",
        type=int,
        default=6,
        help="1-based page number to inspect.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pdf_path = resolve_repo_relative(args.pdf_file_path)

    if not pdf_path.is_file():
        raise SystemExit(f"PDF not found: {pdf_path}")

    with vector_sniffer(pdf_path) as sniffer:
        sniffer.goto(args.page)
        hits = sniffer.query_bbox(
            bbox=(551.29, 500.398, 572.291, 521.399),
            slack=0.1,
            coord_space="pdf",
        )
        print(json.dumps({"bbox_hit_count": len(hits)}, ensure_ascii=False, indent=2))

        if not hits:
            return

        target_group_bbox = bbox_from_shapes(hits)
        anchor_shapes = select_anchor_shapes(hits)

        matched_groups: list[dict[str, object]] = []
        candidate_groups: list[dict[str, object]] = []
        seen_matched_bboxes: set[tuple[int, int, int, int]] = set()
        anchor_matches_by_index: dict[int, list[dict[str, object]]] = {}

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
                transform_key(match["match"])
                for match in anchor_matches_by_index[secondary_anchor["index"]]
            }

        for anchor_match in anchor_matches_by_index[primary_anchor["index"]]:
            match_info = anchor_match["match"]
            if secondary_anchor is not None and transform_key(match_info) not in secondary_keys:
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
            candidate_group = {
                "primary_anchor": {
                    "source_index": primary_anchor["index"],
                    "source_code": primary_anchor["code"],
                    "relative_bbox": relative_bbox(primary_anchor["bbox"], target_group_bbox),
                    "matched_index": anchor_match["index"],
                    "matched_code": anchor_match["code"],
                },
                "secondary_anchor": (
                    {
                        "source_index": secondary_anchor["index"],
                        "source_code": secondary_anchor["code"],
                        "relative_bbox": relative_bbox(secondary_anchor["bbox"], target_group_bbox),
                    }
                    if secondary_anchor is not None
                    else None
                ),
                "transform": match_info,
                "predicted_bbox": bbox_to_dict(predicted_bbox),
                "shape_count": len(candidate_shapes),
                "group_match": group_match,
                "shapes": candidate_shapes,
            }
            candidate_groups.append(candidate_group)
            if group_match["matched"]:
                bbox_key = tuple(round(value * 1000) for value in predicted_bbox)
                if bbox_key not in seen_matched_bboxes:
                    seen_matched_bboxes.add(bbox_key)
                    matched_groups.append(candidate_group)

        summary = {
            "target_group": {
                "shape_count": len(hits),
                "bbox": bbox_to_dict(target_group_bbox),
                "anchors_selected": [
                    {
                        "index": anchor["index"],
                        "type": anchor["type"],
                        "code": anchor["code"],
                        "bbox": anchor["bbox"],
                    }
                    for anchor in anchor_shapes
                ],
            },
            "anchor_match_count": sum(len(matches) for matches in anchor_matches_by_index.values()),
            "candidate_group_count": len(candidate_groups),
            "matched_group_count": len(matched_groups),
            "matched_groups": matched_groups,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
