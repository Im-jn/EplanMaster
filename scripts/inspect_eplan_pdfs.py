#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from epdz_to_connection_json import build_output, extract_epdz
except ModuleNotFoundError as exc:
    if exc.name == "py7zr":
        raise SystemExit(
            "Missing dependency: py7zr. Install project requirements with `pip install -r requirements.txt`."
        ) from exc
    raise


PAGE_NUMBER_PROP = "11000"
PAGE_DESIGNATION_PROP = "11009"
PAGE_TEMPLATE_PROP = "11011"
PAGE_TYPE_PROP = "11017"
PAGE_TYPE_ID_PROP = "11029"
PAGE_SOURCE_REF_PROP = "2000"
PAGE_PLANT_PROP = "1540"
PAGE_LOCATION_PROP = "1640"


def sort_value(value: Any) -> tuple[int, Any]:
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, str(value or ""))


def eplan_page_number(page: dict[str, Any]) -> int | str | None:
    props = page.get("props") or {}
    raw_number = props.get(PAGE_NUMBER_PROP)
    if raw_number in (None, ""):
        return None
    try:
        return int(raw_number)
    except ValueError:
        return raw_number


def page_info(page: dict[str, Any]) -> dict[str, Any]:
    props = page.get("props") or {}
    return {
        "package_id": page.get("id"),
        "name": page.get("name"),
        "eplan_page_number": eplan_page_number(page),
        "designation": props.get(PAGE_DESIGNATION_PROP, ""),
        "template": props.get(PAGE_TEMPLATE_PROP, ""),
        "type": props.get(PAGE_TYPE_PROP, ""),
        "type_id": props.get(PAGE_TYPE_ID_PROP, ""),
        "source_ref": props.get(PAGE_SOURCE_REF_PROP, ""),
        "plant": props.get(PAGE_PLANT_PROP, ""),
        "location": props.get(PAGE_LOCATION_PROP, ""),
        "properties": props,
    }


def device_on_page(device: dict[str, Any], page_name: str) -> dict[str, Any]:
    bbox_info = (device.get("bbox_by_page") or {}).get(page_name) or {}
    pins = device.get("pin") or []
    full_bbox = bbox_info.get("bbox")
    return {
        "id": device.get("id"),
        "raw_ids": device.get("raw_ids") or [],
        "type": device.get("type") or "",
        "bbox": bbox_info.get("symbol_bbox") or full_bbox,
        "full_bbox": full_bbox,
        "symbol_bbox": bbox_info.get("symbol_bbox"),
        "pins": pins,
        "pin_ids": [f"{device.get('id')}:{pin}" for pin in pins],
        "labels": device.get("labels") or [],
        "svg_id": bbox_info.get("svg_id"),
    }


def occurrence_on_page(occurrence: dict[str, Any], page_name: str) -> dict[str, Any]:
    bbox_info = (occurrence.get("bbox_by_page") or {}).get(page_name) or {}
    pins = occurrence.get("pins") or []
    occurrence_id = occurrence.get("package_id")
    full_bbox = bbox_info.get("bbox")
    return {
        "id": f"F{occurrence_id}" if occurrence_id is not None else occurrence.get("source_ref") or occurrence.get("name"),
        "package_id": occurrence_id,
        "source_ref": occurrence.get("source_ref"),
        "name": occurrence.get("name"),
        "device_id": occurrence.get("device_id"),
        "raw_id": occurrence.get("raw_id"),
        "type": occurrence.get("type") or "",
        "bbox": bbox_info.get("symbol_bbox") or full_bbox,
        "full_bbox": full_bbox,
        "symbol_bbox": bbox_info.get("symbol_bbox"),
        "pins": pins,
        "pin_ids": [f"{occurrence.get('device_id')}:{pin}" for pin in pins if occurrence.get("device_id")],
        "labels": occurrence.get("labels") or [],
        "svg_id": bbox_info.get("svg_id") or occurrence.get("svg_id"),
    }


def wire_on_page(wire: dict[str, Any]) -> dict[str, Any]:
    endpoints = []
    for endpoint in wire.get("endpoints") or []:
        device = endpoint.get("device")
        pin = endpoint.get("pin")
        terminal = f"{device}:{pin}" if pin else device
        endpoints.append(
            {
                "device": device,
                "pin": pin,
                "terminal": terminal,
                "raw": endpoint.get("raw"),
            }
        )

    return {
        "id": wire.get("id"),
        "raw_id": wire.get("raw_id"),
        "pins": wire.get("connections") or [item["terminal"] for item in endpoints if item["terminal"]],
        "endpoints": endpoints,
        "bbox": wire.get("bbox"),
        "attrs": wire.get("attrs") or {},
    }


def simplify_epdz(data: dict[str, Any]) -> list[dict[str, Any]]:
    occurrences_by_page: dict[str, list[dict[str, Any]]] = {}
    for occurrence in data.get("function_occurrences", []):
        for page_name in occurrence.get("pages") or []:
            occurrences_by_page.setdefault(page_name, []).append(occurrence_on_page(occurrence, page_name))

    devices_by_page: dict[str, list[dict[str, Any]]] = {}
    for device in data.get("devices", []):
        for page_name in device.get("pages") or []:
            devices_by_page.setdefault(page_name, []).append(device_on_page(device, page_name))

    wires_by_page: dict[str, list[dict[str, Any]]] = {}
    for wire in data.get("wires", []):
        for page_name in wire.get("pages") or []:
            wires_by_page.setdefault(page_name, []).append(wire_on_page(wire))

    pages = list(data.get("pages", []))

    simplified = []
    for index, page in enumerate(pages, start=1):
        name = page.get("name") or ""
        simplified.append(
            {
                "page": index,
                "info": page_info(page),
                "function_occurrences": sorted(
                    occurrences_by_page.get(name, []),
                    key=lambda item: sort_value(item.get("package_id")),
                ),
                "devices": sorted(devices_by_page.get(name, []), key=lambda item: str(item.get("id") or "")),
                "wires": sorted(wires_by_page.get(name, []), key=lambda item: sort_value((item.get("id") or "W0")[1:])),
            }
        )
    return simplified


def inspect_epdz(epdz_path: Path) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="epdz_inspect_") as tmp:
        extract_dir = Path(tmp)
        db_path = extract_epdz(epdz_path, extract_dir)
        pages = simplify_epdz(build_output(db_path, extract_dir))
        gc.collect()
        return pages


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract a compact page/device/wire JSON view from EPLAN .epdz files.",
    )
    parser.add_argument(
        "epdz_files",
        nargs="*",
        type=Path,
        help="EPDZ files to inspect. Defaults to every *.epdz under data/epdz_files.",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/epdz_files"),
        help="Directory scanned when no EPDZ paths are provided.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/epdz_inspection"),
        help="Directory where compact JSON files will be written.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print JSON to stdout instead of writing files. Only valid for one input file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    epdz_paths = args.epdz_files or sorted(args.input_dir.glob("*.epdz"))
    if not epdz_paths:
        raise SystemExit(f"No .epdz files found in {args.input_dir}.")
    if args.stdout and len(epdz_paths) != 1:
        raise SystemExit("--stdout can only be used with exactly one EPDZ file.")

    results: list[dict[str, Any]] = []
    args.output_dir.mkdir(parents=True, exist_ok=True)

    for epdz_path in epdz_paths:
        if not epdz_path.is_file():
            raise SystemExit(f"EPDZ file not found: {epdz_path}")
        compact_pages = inspect_epdz(epdz_path)
        if args.stdout:
            print(json.dumps(compact_pages, ensure_ascii=False, indent=2))
        else:
            out_path = args.output_dir / f"{epdz_path.stem}.compact.json"
            out_path.write_text(json.dumps(compact_pages, ensure_ascii=False, indent=2), encoding="utf-8")
            results.append({"input": str(epdz_path), "output": str(out_path), "pages": len(compact_pages)})

    if not args.stdout:
        index_path = args.output_dir / "index.json"
        index_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Inspected {len(results)} EPDZ file(s).")
        for item in results:
            print(f"- {Path(item['input']).name}: pages={item['pages']} -> {item['output']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
