#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET

import py7zr

# --- Property IDs seen in sample EPDZs ---
PROP_DEVICE_TAG = 1140
PROP_LOCATION_TAG = 1240
PROP_LOCATION_TEXT = 1250
PROP_FULL_TAG = 20001
PROP_TYPE_TEXT = 20026
PROP_FUNC_TEXT = 20031
PROP_PINS = 20038
PROP_PAGE_POS = 20188
PROP_COMPONENT_TAG = 20215

PROP_CONNECTION_OID = "connectionoid"
PROP_ENDPOINT_A = 31019
PROP_ENDPOINT_B = 31020
PROP_CONNECTION_CLASS = 20006
PROP_CONNECTION_PAGEPOS = 20188
PROP_CONNECTION_COLOR = 31004
PROP_CONNECTION_SIZE = 31007
PROP_CONNECTION_LENGTH = 31003
PROP_CONNECTION_LENGTH_NUM = 31000
PROP_CONNECTION_CORES = 31001
PROP_CONNECTION_PART = 31048
PROP_CONNECTION_UNIT = 31060

SVG_NS = "{http://www.w3.org/2000/svg}"
XLINK_NS = "{http://www.w3.org/1999/xlink}"

# ------------------------------ extraction helpers ------------------------------

def extract_epdz(epdz_path: Path, workdir: Path) -> Path:
    with py7zr.SevenZipFile(epdz_path, mode="r") as zf:
        zf.extractall(path=workdir)
    db = workdir / "manifest.db"
    if not db.exists():
        raise FileNotFoundError("manifest.db not found inside EPDZ")
    return db


def load_properties(cur: sqlite3.Cursor, package_ids: List[int]) -> Dict[int, Dict[str, str]]:
    out: Dict[int, Dict[str, str]] = defaultdict(dict)
    if not package_ids:
        return out
    chunk = 900
    for i in range(0, len(package_ids), chunk):
        ids = package_ids[i:i + chunk]
        placeholders = ",".join("?" for _ in ids)
        q = f"SELECT packageid, propname, propid, propindex, value FROM property WHERE packageid IN ({placeholders})"
        for packageid, propname, propid, propindex, value in cur.execute(q, ids):
            key = propname if propname else str(propid)
            if propindex not in (None, 1):
                key = f"{key}[{propindex}]"
            out[packageid][key] = value
    return out

# ------------------------------ ID normalization ------------------------------

_non_alnum = re.compile(r"[^A-Za-z0-9_.:-]+")


def short_device_id(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    s = s.lstrip("=")
    parts = re.split(r"(?=[+-])", s)
    tail = parts[-1] if parts else s
    tail = tail.lstrip("+-&")
    tail = tail or s
    tail = _non_alnum.sub("", tail)
    return tail or s


def is_meaningful_id(s: str) -> bool:
    return bool(s and re.search(r"[A-Za-z0-9]", s))


_pin_splitter = re.compile(r"[;/,]+")


def parse_pin_list(raw: str) -> List[str]:
    if not raw:
        return []
    vals = []
    for part in _pin_splitter.split(raw):
        p = part.strip()
        if p:
            vals.append(p)
    return vals


def parse_endpoint(raw: str) -> Tuple[str, Optional[str], str]:
    s = (raw or "").strip()
    if not s:
        return "", None, ""
    if ":" in s:
        dev_raw, pin = s.rsplit(":", 1)
        pin = pin.strip() or None
    else:
        dev_raw, pin = s, None
    dev_id = short_device_id(dev_raw)
    return dev_id or dev_raw, pin, dev_raw


def choose_device_id(name: str, props: Dict[str, str]) -> Tuple[str, str]:
    raw_candidates = [
        props.get(str(PROP_FULL_TAG), ""),
        "".join(filter(None, [props.get(str(PROP_DEVICE_TAG), ""), props.get(str(PROP_LOCATION_TAG), ""), props.get(str(PROP_COMPONENT_TAG), "")])),
        props.get(str(PROP_COMPONENT_TAG), ""),
        props.get(str(PROP_DEVICE_TAG), ""),
        name,
    ]
    raw = next((c for c in raw_candidates if c and is_meaningful_id(short_device_id(c))), name)
    sid = short_device_id(raw)
    return (sid if is_meaningful_id(sid) else ""), raw

# ------------------------------ SVG bbox parsing ------------------------------

_num_re = re.compile(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?")
_suffix_id_re = re.compile(r"_(\d+)_(\d+)$")

BBox = Tuple[float, float, float, float]
Matrix = Tuple[float, float, float, float, float, float]  # SVG affine a,b,c,d,e,f

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mul(m1: Matrix, m2: Matrix) -> Matrix:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def apply_mat(m: Matrix, x: float, y: float) -> Tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def transform_bbox(b: BBox, m: Matrix) -> BBox:
    x1, y1, x2, y2 = b
    pts = [apply_mat(m, x1, y1), apply_mat(m, x1, y2), apply_mat(m, x2, y1), apply_mat(m, x2, y2)]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def union_bbox(a: Optional[BBox], b: Optional[BBox]) -> Optional[BBox]:
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def parse_transform(tf: Optional[str]) -> Matrix:
    if not tf:
        return IDENTITY
    tf = tf.strip()
    result = IDENTITY
    for name, args in re.findall(r"([A-Za-z]+)\s*\(([^)]*)\)", tf):
        nums = [float(x) for x in _num_re.findall(args)]
        m = IDENTITY
        lname = name.lower()
        if lname == "matrix" and len(nums) >= 6:
            m = (nums[0], nums[1], nums[2], nums[3], nums[4], nums[5])
        elif lname == "translate":
            tx = nums[0] if nums else 0.0
            ty = nums[1] if len(nums) > 1 else 0.0
            m = (1.0, 0.0, 0.0, 1.0, tx, ty)
        elif lname == "scale":
            sx = nums[0] if nums else 1.0
            sy = nums[1] if len(nums) > 1 else sx
            m = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif lname == "rotate":
            if nums:
                angle = math.radians(nums[0])
                cosv, sinv = math.cos(angle), math.sin(angle)
                rot = (cosv, sinv, -sinv, cosv, 0.0, 0.0)
                if len(nums) >= 3:
                    cx, cy = nums[1], nums[2]
                    m = mat_mul(mat_mul((1, 0, 0, 1, cx, cy), rot), (1, 0, 0, 1, -cx, -cy))
                else:
                    m = rot
        result = mat_mul(result, m)
    return result


def bbox_from_points(points: Iterable[Tuple[float, float]]) -> Optional[BBox]:
    pts = list(points)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def parse_points_attr(points_str: str) -> List[Tuple[float, float]]:
    nums = [float(x) for x in _num_re.findall(points_str or "")]
    pts = []
    for i in range(0, len(nums) - 1, 2):
        pts.append((nums[i], nums[i + 1]))
    return pts


def parse_path_bbox(d: str) -> Optional[BBox]:
    nums = [float(x) for x in _num_re.findall(d or "")]
    if len(nums) < 2:
        return None
    pts = []
    for i in range(0, len(nums) - 1, 2):
        pts.append((nums[i], nums[i + 1]))
    return bbox_from_points(pts)


def element_local_bbox(el: ET.Element) -> Optional[BBox]:
    tag = el.tag.split('}')[-1]
    if tag == "path":
        return parse_path_bbox(el.get("d", ""))
    if tag == "line":
        vals = [float(el.get(k, "0")) for k in ("x1", "y1", "x2", "y2")]
        return (min(vals[0], vals[2]), min(vals[1], vals[3]), max(vals[0], vals[2]), max(vals[1], vals[3]))
    if tag == "rect":
        x = float(el.get("x", "0"))
        y = float(el.get("y", "0"))
        w = float(el.get("width", "0"))
        h = float(el.get("height", "0"))
        return (x, y, x + w, y + h)
    if tag == "circle":
        cx = float(el.get("cx", "0"))
        cy = float(el.get("cy", "0"))
        r = float(el.get("r", "0"))
        return (cx - r, cy - r, cx + r, cy + r)
    if tag == "ellipse":
        cx = float(el.get("cx", "0"))
        cy = float(el.get("cy", "0"))
        rx = float(el.get("rx", "0"))
        ry = float(el.get("ry", "0"))
        return (cx - rx, cy - ry, cx + rx, cy + ry)
    if tag in ("polyline", "polygon"):
        return bbox_from_points(parse_points_attr(el.get("points", "")))
    if tag == "image":
        x = float(el.get("x", "0"))
        y = float(el.get("y", "0"))
        w = float(el.get("width", "0"))
        h = float(el.get("height", "0"))
        return (x, y, x + w, y + h)
    if tag == "text":
        x = float(el.get("x", "0")) if el.get("x") else 0.0
        y = float(el.get("y", "0")) if el.get("y") else 0.0
        # Approximate text bbox from font-size and text length if explicit x/y.
        text = "".join(el.itertext()).strip()
        fs = 3.0
        cls = el.get("class", "")
        m = re.search(r"font-size:\s*([0-9.]+)px", "")
        # Prefer transform translate() when present.
        tf = el.get("transform")
        if tf:
            nums = [float(n) for n in _num_re.findall(tf)]
            if len(nums) >= 2:
                x, y = nums[0], nums[1]
        w = max(1.0, len(text) * fs * 0.6)
        h = fs
        return (x, y - h, x + w, y)
    return None


def collect_group_bboxes(svg_path: Path) -> Dict[str, Dict[str, object]]:
    tree = ET.parse(svg_path)
    root = tree.getroot()
    out: Dict[str, Dict[str, object]] = {}

    def walk(el: ET.Element, parent_mat: Matrix = IDENTITY) -> Optional[BBox]:
        current_mat = mat_mul(parent_mat, parse_transform(el.get("transform")))
        bbox = None
        lb = element_local_bbox(el)
        if lb is not None:
            bbox = union_bbox(bbox, transform_bbox(lb, current_mat))
        for ch in list(el):
            cb = walk(ch, current_mat)
            bbox = union_bbox(bbox, cb)
        tag = el.tag.split('}')[-1]
        if tag == "g":
            gid = el.get("id")
            if gid and gid.startswith("Id") and bbox is not None:
                title_el = el.find(f"{SVG_NS}title")
                title = "".join(title_el.itertext()).strip() if title_el is not None else ""
                out[gid] = {
                    "title": title,
                    "bbox": [round(bbox[0], 3), round(bbox[1], 3), round(bbox[2], 3), round(bbox[3], 3)],
                }
        return bbox

    walk(root)
    return out


def function_name_to_svg_id(name: str) -> Optional[str]:
    m = _suffix_id_re.search(name or "")
    if not m:
        return None
    return f"Id{m.group(1)}_{m.group(2)}"

# ------------------------------ build output ------------------------------

def build_output(db_path: Path, extracted_root: Path) -> Dict:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    functions = cur.execute("SELECT packageid, name FROM function_package").fetchall()
    connections = cur.execute("SELECT packageid, name FROM mergedconnection_package").fetchall()
    pages = cur.execute("SELECT packageid, name FROM page_package").fetchall()
    page_links = cur.execute("SELECT pageid, functionid FROM page_functions").fetchall()

    function_ids = [pid for pid, _ in functions]
    connection_ids = [pid for pid, _ in connections]
    page_ids = [pid for pid, _ in pages]

    fprops = load_properties(cur, function_ids)
    cprops = load_properties(cur, connection_ids)
    pprops = load_properties(cur, page_ids)

    function_to_pages: Dict[int, List[int]] = defaultdict(list)
    for pageid, fid in page_links:
        function_to_pages[fid].append(pageid)

    page_names = {pid: name for pid, name in pages}

    # Map page package -> referenced svg file(s)
    page_svg_file: Dict[int, Path] = {}
    for packageid, item_type, locator, referenced in cur.execute(
        "SELECT packageid, type, locator, referenced FROM item WHERE type='pagesvg'"
    ):
        if locator and locator.lower().endswith('.svg'):
            page_svg_file[packageid] = extracted_root / 'packages' / 'pages' / 'items' / 'pagesvg' / locator

    # Parse page SVGs once.
    page_svg_bboxes: Dict[int, Dict[str, Dict[str, object]]] = {}
    for pid, svg_path in page_svg_file.items():
        if svg_path.exists():
            try:
                page_svg_bboxes[pid] = collect_group_bboxes(svg_path)
            except Exception:
                page_svg_bboxes[pid] = {}

    devices: Dict[str, Dict[str, object]] = {}
    terminal_set = set()
    package_to_device: Dict[int, str] = {}

    def ensure_device(device_id: str, raw_id: str = "", typ: str = "", pages_list: Optional[List[str]] = None):
        if not device_id:
            return None
        d = devices.setdefault(device_id, {
            "id": device_id,
            "raw_ids": [],
            "type": "",
            "pins": set(),
            "pages": set(),
            "labels": set(),
            "source_packages": set(),
            "bbox_by_page": {},
            "svg_ids": set(),
        })
        if raw_id and raw_id not in d["raw_ids"]:
            d["raw_ids"].append(raw_id)
        if typ and not d["type"]:
            d["type"] = typ
        if pages_list:
            d["pages"].update(pages_list)
        return d

    # Build devices.
    for pid, name in functions:
        props = fprops.get(pid, {})
        device_id, raw_id = choose_device_id(name, props)
        pageids = function_to_pages.get(pid, [])
        page_list = [page_names[p] for p in pageids if p in page_names]
        d = ensure_device(device_id, raw_id=raw_id, typ=props.get(str(PROP_TYPE_TEXT), ""), pages_list=page_list)
        if d is None:
            continue
        package_to_device[pid] = device_id
        d["source_packages"].add(pid)
        for k in [str(PROP_FUNC_TEXT), str(PROP_LOCATION_TEXT), str(PROP_FULL_TAG), str(PROP_COMPONENT_TAG), str(PROP_DEVICE_TAG)]:
            v = props.get(k, "")
            if v:
                d["labels"].add(v)
        for pin in parse_pin_list(props.get(str(PROP_PINS), "")):
            d["pins"].add(pin)

        svg_id = function_name_to_svg_id(name)
        if svg_id:
            d["svg_ids"].add(svg_id)
            for pageid in pageids:
                group_info = page_svg_bboxes.get(pageid, {}).get(svg_id)
                if group_info:
                    d["bbox_by_page"][page_names[pageid]] = {
                        "svg_id": svg_id,
                        "bbox": group_info["bbox"],
                        "title": group_info.get("title", ""),
                    }

    # Wires.
    wires: List[Dict[str, object]] = []
    for idx, (pid, name) in enumerate(connections, start=1):
        props = cprops.get(pid, {})
        endpoint_vals = [props.get(str(PROP_ENDPOINT_A), ""), props.get(str(PROP_ENDPOINT_B), "")]
        endpoint_vals = [x for x in endpoint_vals if x]

        parsed_terms: List[str] = []
        parsed_connections: List[Dict[str, Optional[str]]] = []
        page_union = set()
        bbox_union: Optional[BBox] = None

        for ep_raw in endpoint_vals:
            dev_id, pin, dev_raw = parse_endpoint(ep_raw)
            if not dev_id or not is_meaningful_id(dev_id):
                continue
            d = ensure_device(dev_id, raw_id=dev_raw)
            if pin:
                d["pins"].add(pin)
                term = f"{dev_id}:{pin}"
            else:
                term = dev_id
            terminal_set.add(term)
            parsed_terms.append(term)
            parsed_connections.append({"device": dev_id, "pin": pin, "raw": ep_raw})
            if dev_id in devices:
                page_union.update(devices[dev_id]["pages"])
                for page_name, bb in devices[dev_id].get("bbox_by_page", {}).items():
                    if page_name in page_union or page_name:
                        box = bb.get("bbox")
                        if box:
                            bbox_union = union_bbox(bbox_union, tuple(box))

        raw_wire_id = props.get(str(PROP_CONNECTION_OID)) or name or f"W{idx}"
        wires.append({
            "id": f"W{idx}",
            "raw_id": raw_wire_id,
            "connections": parsed_terms,
            "endpoints": parsed_connections,
            "pages": sorted(page_union),
            "bbox": [round(bbox_union[0], 3), round(bbox_union[1], 3), round(bbox_union[2], 3), round(bbox_union[3], 3)] if bbox_union else None,
            "attrs": {
                "class": props.get(str(PROP_CONNECTION_CLASS), ""),
                "pagepos": props.get(str(PROP_CONNECTION_PAGEPOS), ""),
                "color": props.get(str(PROP_CONNECTION_COLOR), ""),
                "size": props.get(str(PROP_CONNECTION_SIZE), ""),
                "length": props.get(str(PROP_CONNECTION_LENGTH), "") or props.get(str(PROP_CONNECTION_LENGTH_NUM), ""),
                "cores": props.get(str(PROP_CONNECTION_CORES), ""),
                "part": props.get(str(PROP_CONNECTION_PART), ""),
                "unit": props.get(str(PROP_CONNECTION_UNIT), ""),
            },
        })

    devices_out: List[Dict[str, object]] = []
    for d in devices.values():
        devices_out.append({
            "id": d["id"],
            "type": d["type"],
            "pin": sorted(d["pins"]),
            "raw_ids": d["raw_ids"],
            "pages": sorted(d["pages"]),
            "labels": sorted(d["labels"]),
            "bbox_by_page": d["bbox_by_page"],
            "bbox": next((v["bbox"] for _, v in sorted(d["bbox_by_page"].items())), None),
            "svg_ids": sorted(d["svg_ids"]),
        })
    devices_out.sort(key=lambda x: x["id"])
    wires.sort(key=lambda x: x["id"])

    pages_out = []
    for pid, name in pages:
        assets = []
        for item_id, packageid, item_type, locator, base, attached, referenced, filesize in cur.execute(
            "SELECT id, packageid, type, locator, base, attatched, referenced, filesize FROM item WHERE packageid=?",
            (pid,),
        ):
            assets.append({"type": item_type, "locator": locator, "referenced": referenced, "filesize": filesize})
        pages_out.append({
            "id": pid,
            "name": name,
            "props": pprops.get(pid, {}),
            "assets": assets,
        })

    return {
        "devices": devices_out,
        "terminals": sorted(terminal_set),
        "wires": wires,
        "pages": pages_out,
        "meta": {
            "schema": "epdz-connection-json/v2",
            "notes": [
                "device IDs are heuristic short forms derived from EPLAN engineering designations",
                "wire IDs are synthetic W1/W2/...; raw_id keeps the original connection identifier",
                "device bbox comes from matching function package names to SVG group IDs such as Id17_47440",
                "wire bbox is an approximation from the union of endpoint device bboxes",
                "coordinates are in page SVG viewBox units",
            ],
        },
    }

# ------------------------------ CLI ------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Convert EPLAN EPDZ into connection JSON with device bboxes.")
    p.add_argument("-i", "--input", required=True, type=Path, help="Input .epdz file")
    p.add_argument("-o", "--output-dir", required=True, type=Path, help="Output directory")
    p.add_argument("--output-name", default=None, help="Output JSON filename (default: <input_stem>.connection.bbox.json)")
    p.add_argument("--keep-extracted", action="store_true", help="Keep extracted EPDZ contents in the output directory")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    in_path: Path = args.input
    out_dir: Path = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = args.output_name or f"{in_path.stem}.connection.bbox.json"
    out_path = out_dir / out_name

    if args.keep_extracted:
        extract_dir = out_dir / f"{in_path.stem}_extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        db_path = extract_epdz(in_path, extract_dir)
        data = build_output(db_path, extract_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="epdz_extract_") as td:
            extract_dir = Path(td)
            db_path = extract_epdz(in_path, extract_dir)
            data = build_output(db_path, extract_dir)

    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
