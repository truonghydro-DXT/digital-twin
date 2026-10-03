#!/usr/bin/env python3
"""Chuyen model.obj / LAS thanh Cesium 3D Tiles.

Mesh: output/tiled/*.b3dm
May diem:  output/tiled-las/*.pnts  (3D Tiles point cloud, khong dung b3dm)

Chỉ rendder *.b3dm:
  .\\venv\\Scripts\\python.exe -u build-3dtiles.py D:\\WORK\\digital-twin\\output-3d-filnal --max-zoom 32 --rebuild
Chỉ rendder LAS (*.pnts):
  .\\venv\\Scripts\\python.exe -u build-3dtiles.py D:\\WORK\\digital-twin\\output-3d-new --points --max-zoom 32 --rebuild
Tất cả:
  .\\venv\\Scripts\\python.exe -u build-3dtiles.py D:\\WORK\\digital-twin\\output-3d-filnal --all --max-zoom 32 --rebuild
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import shutil
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAP_MAX_ZOOM = 32
WEB_MERCATOR_Z0_MPP = 156543.03392804097
LEAF_FACE_TARGET = 500_000
# pnts: refine ADD, moi diem ghi dung 1 lan (cha giu mau, con giu phan con lai).
POINT_TILE_TARGET = 1_000_000
POINT_MAX_TREE_DEPTH = 14
MAX_TREE_DEPTH = 8
MIN_SPLIT_METERS = 4.0
# overlap mat tai bien tile — tranh khe den (centroid-only split)
TILE_FACE_OVERLAP_M = 1.25
OBJ_MESH_MAGIC = b"DTM2"
LAS_DTP1_HEADER = struct.Struct("<4sIII6d4d")
PNTS_HEADER = struct.Struct("<4sIIIIII")


def log(msg: str) -> None:
    print(msg, flush=True)


def _lat_ok(lat: float) -> bool:
    return math.isfinite(lat) and abs(lat) < 80.0


def pixel_size_for_zoom(zoom: int, lat_deg: float) -> float:
    zoom = max(0, min(MAP_MAX_ZOOM, int(zoom)))
    lat = max(-85.0, min(85.0, float(lat_deg)))
    return WEB_MERCATOR_Z0_MPP * math.cos(math.radians(lat)) / (2 ** zoom)


def estimate_lonlat(output: Path) -> tuple[float, float]:
    cams = output / "camera_reference.csv"
    if cams.is_file():
        lats: list[float] = []
        lons: list[float] = []
        with cams.open(encoding="utf-8", errors="ignore") as handle:
            header = next(handle, "")
            cols = [c.strip().lower() for c in header.split(",")]
            lon_idx = cols.index("longitude") if "longitude" in cols else 1
            lat_idx = cols.index("latitude") if "latitude" in cols else 2
            for line in handle:
                parts = line.strip().split(",")
                if len(parts) <= max(lon_idx, lat_idx):
                    continue
                lon = float(parts[lon_idx])
                lat = float(parts[lat_idx])
                if _lat_ok(lat) and math.isfinite(lon):
                    lons.append(lon)
                    lats.append(lat)
        if lats:
            return sum(lons) / len(lons), sum(lats) / len(lats)
    ortho = output / "orthomosaic" / "orthomosaic.tif"
    if ortho.is_file():
        try:
            import rasterio
            from rasterio.warp import transform_bounds

            with rasterio.open(ortho) as src:
                west, south, east, north = transform_bounds(
                    src.crs, "EPSG:4326", *src.bounds, densify_pts=21
                )
            lat = (float(south) + float(north)) / 2.0
            lon = (float(west) + float(east)) / 2.0
            if _lat_ok(lat):
                return lon, lat
        except Exception:
            pass
    return 105.681, 21.123


def enu_to_ecef_matrix(lon_deg: float, lat_deg: float, height: float = 0.0) -> list[float]:
    lon = math.radians(lon_deg)
    lat = math.radians(lat_deg)
    slon, clon = math.sin(lon), math.cos(lon)
    slat, clat = math.sin(lat), math.cos(lat)
    a = 6378137.0
    e2 = 6.69437999014e-3
    nrad = a / math.sqrt(1.0 - e2 * slat * slat)
    x = (nrad + height) * clat * clon
    y = (nrad + height) * clat * slon
    z = (nrad * (1.0 - e2) + height) * slat
    # column-major 4x4: X east, Y north, Z up
    return [
        -slon, clon, 0.0, 0.0,
        -slat * clon, -slat * slon, clat, 0.0,
        clat * clon, clat * slon, slat, 0.0,
        x, y, z, 1.0,
    ]


def _clear_exported_tiles(tiled_dir: Path) -> None:
    for name in ("Data", "tileset.json", "tileset.zip", "tile.zip", ".max-zoom"):
        path = tiled_dir / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


def _read_dtm_mesh(path: Path) -> dict:
    import numpy as np

    raw = path.read_bytes()
    if len(raw) < 40:
        raise ValueError(f"Mesh binary qua ngan: {path}")
    magic, nverts, nidx, flags, xmin, xmax, ymin, ymax, zmin, zmax = struct.unpack_from(
        "<4sIII6f", raw, 0
    )
    if magic not in (b"DTM1", OBJ_MESH_MAGIC):
        raise ValueError(f"Sai magic {magic!r} trong {path}")
    off = 40
    pos = np.frombuffer(raw, dtype=np.float32, count=nverts * 3, offset=off).reshape(nverts, 3).copy()
    off += nverts * 12
    if flags & 1:
        off += nverts * 3
        off = (off + 3) & ~3
    uvs = None
    if flags & 2:
        uvs = np.frombuffer(raw, dtype=np.float32, count=nverts * 2, offset=off).reshape(nverts, 2).copy()
        off += nverts * 8
    if flags & 4:
        off += nverts * 12
    pages = None
    if flags & 8:
        pages = np.frombuffer(raw, dtype=np.uint8, count=nverts, offset=off).copy()
        off += nverts
        off = (off + 3) & ~3
    indices = np.empty(0, dtype=np.uint32)
    if nidx > 0:
        indices = np.frombuffer(raw, dtype=np.uint32, count=nidx, offset=off).copy()
    return {
        "pos": pos,
        "uvs": uvs,
        "pages": pages,
        "indices": indices,
        "xmin": float(xmin),
        "xmax": float(xmax),
        "ymin": float(ymin),
        "ymax": float(ymax),
        "zmin": float(zmin),
        "zmax": float(zmax),
    }


def load_bin_jpg():
    path = HERE / "build-bin-jpg.py"
    if not path.is_file():
        raise FileNotFoundError(f"Khong thay {path}")
    spec = importlib.util.spec_from_file_location("build_bin_jpg", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Khong nap duoc build-bin-jpg.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_bin_jpg"] = mod
    spec.loader.exec_module(mod)
    return mod


def _preview_mesh_ready(output: Path, obj_path: Path) -> Path | None:
    preview = output / ".preview"
    obj_mtime = obj_path.stat().st_mtime if obj_path.is_file() else 0
    for name in ("model-mesh-full-view.bin", "model-mesh-full.bin"):
        path = preview / name
        if path.is_file() and path.stat().st_size > 64 and path.stat().st_mtime >= obj_mtime - 2:
            return path
    return None


def ensure_preview_bins(output: Path, args: argparse.Namespace) -> None:
    if getattr(args, "skip_preview", False):
        log("-- Bo qua build-bin-jpg (--skip-preview)")
        return
    obj_path = output / "model" / "model.obj"
    ready = _preview_mesh_ready(output, obj_path)
    if ready is not None:
        log(f"-- RESUME: da co {ready.name} ({ready.stat().st_size:,} byte), bo qua build-bin-jpg")
        return
    log("[START] build-bin-jpg: OBJ/LAS -> .preview *.bin *.jpg")
    preview = load_bin_jpg().run_preview(
        output,
        rebuild=False,
        skip_mesh=False,
        skip_las=bool(getattr(args, "skip_las", False)),
        skip_gzip=bool(getattr(args, "skip_gzip", False)),
    )
    log(f"[DONE ] preview san sang: {preview}")


def _find_obj_mesh_bin(output: Path, obj_path: Path) -> Path:
    path = _preview_mesh_ready(output, obj_path)
    if path is not None:
        return path
    raise FileNotFoundError(
        f"Chua co mesh da parse tu {obj_path}. Mo Mesh OBJ mot lan hoac doi .preview bin."
    )


def _save_jpeg(img, dest: Path) -> None:
    img.save(dest, format="JPEG", quality=90, optimize=True, subsampling=0)


def _atlas_sort_key(path: Path) -> tuple[str, int]:
    stem = path.stem.lower()
    match = re.match(r"^(.*?)(\d+)?$", stem)
    if not match:
        return (stem, 0)
    return (match.group(1), int(match.group(2) or 0))


def _atlas_uri(texture_uri: str | None, page_i: int) -> str | None:
    if not texture_uri:
        return None
    if page_i <= 0:
        return texture_uri
    stem = Path(texture_uri).stem
    suffix = Path(texture_uri).suffix or ".jpg"
    return f"{stem}_{page_i + 1}{suffix}"


def _texture_sources(output: Path) -> list[Path]:
    pages: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        if not path.is_file():
            return
        key = path.resolve().as_posix().lower()
        if key in seen:
            return
        seen.add(key)
        pages.append(path)

    for folder, stem in ((output / "model", "model"),):
        if not folder.is_dir():
            continue
        tifs = sorted(folder.glob(stem + "*.tif"), key=_atlas_sort_key)
        jpgs = sorted(folder.glob(stem + "*.jpg"), key=_atlas_sort_key)
        for path in tifs or jpgs:
            add(path)
    if pages:
        return pages
    tex_dir = output / "texture"
    if tex_dir.is_dir():
        tifs = sorted(tex_dir.glob("texture*.tif"), key=_atlas_sort_key)
        jpgs = sorted(tex_dir.glob("texture*.jpg"), key=_atlas_sort_key)
        for path in tifs or jpgs:
            add(path)
    if pages:
        return pages
    preview = output / ".preview"
    add(preview / "model-mesh-full.jpg")
    for path in sorted(preview.glob("model-mesh-full-*.jpg")):
        add(path)
    return pages


def _prepare_texture(output: Path, dest: Path, max_side: int) -> Path | None:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    srcs = _texture_sources(output)
    if not srcs:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = None
    for i, src in enumerate(srcs):
        out = dest if i == 0 else dest.with_name(f"texture_{i + 1}.jpg")
        img = Image.open(src)
        if img.mode != "RGB":
            img = img.convert("RGB")
        longest = max(img.size)
        if longest > max_side:
            scale = max_side / float(longest)
            img = img.resize(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                Image.Resampling.LANCZOS,
            )
        _save_jpeg(img, out)
        if i == 0:
            written = out
    return written


def _pad4(buf: bytes, fill: bytes = b" ") -> bytes:
    pad = (-len(buf)) % 4
    return buf if pad == 0 else buf + fill * pad


def _pad8(buf: bytes, fill: bytes = b" ") -> bytes:
    pad = (-len(buf)) % 8
    return buf if pad == 0 else buf + fill * pad


def write_glb(pos, uvs, indices, texture_uri: str | None, vert_pages=None) -> bytes:
    import numpy as np

    pos = np.asarray(pos, dtype=np.float64).reshape(-1, 3)
    indices = np.ascontiguousarray(indices, dtype=np.uint32)
    have_uv = uvs is not None and bool(texture_uri)
    uv = None
    if have_uv:
        uv_f = np.asarray(uvs, dtype=np.float64).reshape(-1, 2).copy()
        uv_f[:, 1] = 1.0 - uv_f[:, 1]
        uv = np.rint(uv_f.clip(0.0, 1.0) * 65535.0).astype(np.uint16)
        del uv_f
    # KHR_mesh_quantization: POSITION uint16 (stride 8 cho can 4 byte), giai nen qua node scale/translation.
    q_lo = pos.min(axis=0)
    q_span = np.maximum(pos.max(axis=0) - q_lo, 1e-6)
    q_pos = np.zeros((pos.shape[0], 4), dtype=np.uint16)
    q_pos[:, :3] = np.rint((pos - q_lo) / q_span * 65535.0).clip(0, 65535).astype(np.uint16)

    index_groups: list[tuple[int, np.ndarray]] = [(0, indices)]
    if have_uv and vert_pages is not None and len(vert_pages) == pos.shape[0] and indices.size >= 3:
        vp = np.ascontiguousarray(vert_pages, dtype=np.uint8)
        faces = indices.reshape(-1, 3)
        face_page = vp[faces[:, 0]]
        groups: list[tuple[int, np.ndarray]] = []
        for page_i in np.unique(face_page):
            sel = faces[face_page == page_i]
            if sel.size:
                groups.append((int(page_i), np.ascontiguousarray(sel.ravel(), dtype=np.uint32)))
        if groups:
            index_groups = groups

    parts = [q_pos.tobytes()]
    if uv is not None:
        parts.append(uv.tobytes())
    for _, idx in index_groups:
        parts.append(idx.tobytes())
    blob = bytearray(b"".join(parts))

    views = [
        {"buffer": 0, "byteOffset": 0, "byteLength": int(q_pos.nbytes), "byteStride": 8, "target": 34962},
    ]
    accessors: list[dict] = [
        {
            "bufferView": 0,
            "componentType": 5123,
            "count": int(pos.shape[0]),
            "type": "VEC3",
            "min": [int(v) for v in q_pos[:, :3].min(axis=0)],
            "max": [int(v) for v in q_pos[:, :3].max(axis=0)],
        }
    ]
    attrs = {"POSITION": 0}
    off = int(q_pos.nbytes)
    if uv is not None:
        views.append({"buffer": 0, "byteOffset": off, "byteLength": int(uv.nbytes), "target": 34962})
        accessors.append(
            {
                "bufferView": 1,
                "componentType": 5123,
                "normalized": True,
                "count": int(uv.shape[0]),
                "type": "VEC2",
            }
        )
        attrs["TEXCOORD_0"] = 1
        off += int(uv.nbytes)

    primitives = []
    page_to_mat: dict[int, int] = {}
    used_pages = [p for p, _ in index_groups]
    for mat_i, page_i in enumerate(used_pages):
        page_to_mat[page_i] = mat_i
    for page_i, idx in index_groups:
        views.append({"buffer": 0, "byteOffset": off, "byteLength": int(idx.nbytes), "target": 34963})
        accessors.append(
            {
                "bufferView": len(views) - 1,
                "componentType": 5125,
                "count": int(idx.size),
                "type": "SCALAR",
            }
        )
        primitive = {"attributes": attrs, "indices": len(accessors) - 1, "mode": 4}
        if have_uv:
            primitive["material"] = page_to_mat[page_i]
        primitives.append(primitive)
        off += int(idx.nbytes)

    gltf: dict = {
        "asset": {"version": "2.0", "generator": "digital-twin-b3dm"},
        "buffers": [{"byteLength": len(blob)}],
        "bufferViews": views,
        "accessors": accessors,
        "meshes": [{"primitives": primitives}],
        "nodes": [
            {
                "mesh": 0,
                "translation": [float(v) for v in q_lo],
                "scale": [float(v) / 65535.0 for v in q_span],
            }
        ],
        "scenes": [{"nodes": [0]}],
        "scene": 0,
        "extensionsUsed": ["KHR_mesh_quantization"],
        "extensionsRequired": ["KHR_mesh_quantization"],
    }
    if have_uv:
        images = []
        textures = []
        materials = []
        for page_i in used_pages:
            images.append({"uri": _atlas_uri(texture_uri, page_i)})
            textures.append({"source": len(images) - 1})
            materials.append(
                {
                    "pbrMetallicRoughness": {
                        "baseColorTexture": {"index": len(textures) - 1},
                        "metallicFactor": 0.0,
                        "roughnessFactor": 1.0,
                    },
                    "doubleSided": True,
                    "extensions": {"KHR_materials_unlit": {}},
                }
            )
        gltf["images"] = images
        gltf["textures"] = textures
        gltf["materials"] = materials
        gltf["extensionsUsed"].append("KHR_materials_unlit")
    js = _pad4(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b" ")
    blob = _pad4(bytes(blob), b"\x00")
    total = 12 + 8 + len(js) + 8 + len(blob)
    out = bytearray()
    out += struct.pack("<4sII", b"glTF", 2, total)
    out += struct.pack("<I4s", len(js), b"JSON")
    out += js
    out += struct.pack("<I4s", len(blob), b"BIN\x00")
    out += blob
    return bytes(out)


def write_b3dm(glb: bytes) -> bytes:
    feature = _pad8(b'{"BATCH_LENGTH":1}', b" ")
    header = struct.pack(
        "<4sIIIIII",
        b"b3dm",
        1,
        28 + len(feature) + len(glb),
        len(feature),
        0,
        0,
        0,
    )
    return header + feature + glb


def _box_volume(xmin, xmax, ymin, ymax, zmin, zmax) -> list[float]:
    cx = 0.5 * (xmin + xmax)
    cy = 0.5 * (ymin + ymax)
    cz = 0.5 * (zmin + zmax)
    hx = max(0.5 * (xmax - xmin), 0.05)
    hy = max(0.5 * (ymax - ymin), 0.05)
    hz = max(0.5 * (zmax - zmin), 0.05)
    return [cx, cy, cz, hx, 0.0, 0.0, 0.0, hy, 0.0, 0.0, 0.0, hz]


def _fit_child_bounds(node: dict) -> tuple[list[float], list[float]]:
    """Noi hop cha cho chua tron hop con: Cesium loai hop cha ngoai khung nhin thi khong duyet con
    → zoom gan bi thieu tile (dinh cot/tuong cao, tam giac lon o mep o)."""
    b = node["boundingVolume"]["box"]
    lo = [b[0] - b[3], b[1] - b[7], b[2] - b[11]]
    hi = [b[0] + b[3], b[1] + b[7], b[2] + b[11]]
    for child in node.get("children", []):
        clo, chi = _fit_child_bounds(child)
        lo = [min(a, c) for a, c in zip(lo, clo)]
        hi = [max(a, c) for a, c in zip(hi, chi)]
    node["boundingVolume"]["box"] = _box_volume(lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])
    return lo, hi


def _extract_tile_mesh(east, north, up, uvs, faces, face_ids, pages=None):
    import numpy as np

    if face_ids.size == 0:
        return None
    tri = faces[face_ids]
    used, inverse = np.unique(tri.ravel(), return_inverse=True)
    pos = np.column_stack([east[used], north[used], up[used]]).astype(np.float32)
    tile_uv = uvs[used] if uvs is not None else None
    tile_pages = pages[used].astype(np.uint8, copy=False) if pages is not None else None
    return pos, tile_uv, inverse.astype(np.uint32), tile_pages


def _cluster_decimate(pos, uv, idx, pages, cell: float):
    """Gom dinh theo luoi toan cuc (cell m): mat kin, khong thung nhu lay 1/step tam giac.
    Luoi co goc chung → 2 tile ke nhau cung depth cho cung dinh o vung overlap."""
    import numpy as np

    q = np.floor(pos.astype(np.float64) / cell).astype(np.int64)
    q -= q.min(axis=0)
    span = q.max(axis=0) + 1
    skey = (q[:, 0] * span[1] + q[:, 1]) * span[2] + q[:, 2]
    del q
    _, s_inv = np.unique(skey, return_inverse=True)
    del skey
    ns = int(s_inv.max()) + 1
    s_cnt = np.bincount(s_inv, minlength=ns).astype(np.float64)
    s_pos = np.empty((ns, 3), dtype=np.float64)
    for k in range(3):
        s_pos[:, k] = np.bincount(s_inv, weights=pos[:, k], minlength=ns) / s_cnt
    # Dinh cung o nhung khac manh UV (seam atlas) khong duoc gop: gop se keo UV manh khac
    # → tam giac lay nham vung texture (dom mau). Vi tri van chung theo o → khong nut.
    cols = [s_inv.astype(np.int64)]
    sizes = [ns]
    if uv is not None:
        uv_cell = _uv_cluster_cell(pos, uv, idx, cell)
        if uv_cell > 0.0:
            uq = np.floor(np.asarray(uv, dtype=np.float64) / uv_cell).astype(np.int64)
            uq -= uq.min(axis=0)
            for k in range(2):
                cols.append(uq[:, k])
                sizes.append(int(uq[:, k].max()) + 1)
            del uq
    if pages is not None:
        cols.append(pages.astype(np.int64))
        sizes.append(8)
    if math.prod(sizes) < (1 << 62):
        key = cols[0]
        for col, size in zip(cols[1:], sizes[1:]):
            key = key * size + col
        _, first, inv = np.unique(key, return_index=True, return_inverse=True)
        del key
    else:
        _, first, inv = np.unique(np.column_stack(cols), axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    del cols
    n = int(first.size)
    new_pos = s_pos[s_inv[first]].astype(np.float32)
    tri = inv[idx.reshape(-1, 3)]
    ok = (tri[:, 0] != tri[:, 1]) & (tri[:, 1] != tri[:, 2]) & (tri[:, 0] != tri[:, 2])
    tri = tri[ok]
    if tri.shape[0] == 0:
        return None
    srt = np.sort(tri, axis=1)
    if n < (1 << 21):
        fkey = (srt[:, 0] << 42) | (srt[:, 1] << 21) | srt[:, 2]
        _, keep = np.unique(fkey, return_index=True)
    else:
        _, keep = np.unique(srt, axis=0, return_index=True)
    tri = tri[np.sort(keep)]
    used, inv2 = np.unique(tri.ravel(), return_inverse=True)
    out_uv = None
    if uv is not None:
        cnt = np.bincount(inv, minlength=n).astype(np.float64)
        mean_uv = np.empty((n, 2), dtype=np.float32)
        for k in range(2):
            mean_uv[:, k] = np.bincount(inv, weights=uv[:, k], minlength=n) / cnt
        out_uv = mean_uv[used]
    out_pages = pages[first[used]] if pages is not None else None
    return new_pos[used], out_uv, inv2.astype(np.uint32), out_pages


def _uv_cluster_cell(pos, uv, idx, cell: float) -> float:
    """O luoi UV ~ 2 o khong gian theo mat do texel trung vi (canh seam hiem nen trung vi on dinh)."""
    import numpy as np

    tri = idx.reshape(-1, 3)
    if tri.shape[0] > 200_000:
        tri = tri[:: int(math.ceil(tri.shape[0] / 200_000))]
    a, b = tri[:, 0], tri[:, 1]
    dp = np.linalg.norm(pos[b].astype(np.float64) - pos[a], axis=1)
    du = np.linalg.norm(np.asarray(uv[b], dtype=np.float64) - uv[a], axis=1)
    ok = (dp > 1e-9) & (du > 0)
    if not np.any(ok):
        return 0.0
    ratio = float(np.median(du[ok] / dp[ok]))
    return 2.0 * cell * ratio if ratio > 0 and math.isfinite(ratio) else 0.0


def _merge_meshes(meshes):
    import numpy as np

    pos_l, uv_l, idx_l, pg_l = [], [], [], []
    off = 0
    for pos, uv, idx, pg in meshes:
        pos_l.append(pos)
        uv_l.append(uv)
        pg_l.append(pg)
        idx_l.append(idx.astype(np.int64) + off)
        off += pos.shape[0]
    has_uv = all(u is not None for u in uv_l)
    has_pg = all(p is not None for p in pg_l)
    return (
        np.concatenate(pos_l),
        np.concatenate(uv_l) if has_uv else None,
        np.concatenate(idx_l),
        np.concatenate(pg_l) if has_pg else None,
    )


def _parent_lod_mesh(child_meshes, width: float, height: float, face_target: int):
    """Mesh LOD cho tile cha tu mesh 4 con (bottom-up), ~face_target/2 tam.
    Tra ve (mesh, cell): cell (m) = sai so hinh hoc thuc cua LOD → geometricError."""
    merged = _merge_meshes(child_meshes)
    goal = max(face_target // 2, 20_000)
    # mat ~ 2 x so o luoi tren be mat (~1.3 x dien tich ngang) → cell = sqrt(5.2 w h / target)
    cell = max(math.sqrt(5.2 * max(width, 1.0) * max(height, 1.0) / max(face_target, 1)), 0.01)
    out = None
    for _ in range(6):
        out = _cluster_decimate(*merged, cell)
        if out is None or out[2].size // 3 <= goal * 1.5:
            break
        cell *= 1.3
    return out, cell


def build_quadtree(*args, **kwargs) -> dict | None:
    result = _build_quadtree_lod(*args, **kwargs)
    return result[0] if result is not None else None


def _build_quadtree_lod(
    east,
    north,
    up,
    uvs,
    faces,
    face_cx,
    face_cy,
    xmin,
    xmax,
    ymin,
    ymax,
    depth: int,
    max_depth: int,
    face_target: int,
    data_dir: Path,
    texture_uri: str | None,
    tile_id: list[int],
    pages=None,
    cand=None,
):
    """Tra ve (node, mesh): la = mesh goc; cha = gom dinh tu mesh 4 con (khong thung)."""
    import numpy as np

    pad = float(TILE_FACE_OVERLAP_M)
    # cand = mat trong hop (co pad) cua nut cha: chi loc tren tap nay, khong quet lai ca mesh.
    if cand is None:
        cand = np.arange(face_cx.shape[0], dtype=np.int64)
    cx = face_cx[cand]
    cy = face_cy[cand]
    in_pad = (cx >= xmin - pad) & (cx <= xmax + pad) & (cy >= ymin - pad) & (cy <= ymax + pad)
    sel = cand[in_pad]
    cx = cx[in_pad]
    cy = cy[in_pad]
    # Chia cay theo hop chuan; chon mat noi dung co overlap de kin bien.
    sel_exact = sel[(cx >= xmin) & (cx <= xmax) & (cy >= ymin) & (cy <= ymax)]
    del cx, cy, in_pad
    if sel_exact.size == 0 and sel.size == 0:
        return None
    split = (
        sel_exact.size > face_target
        and depth < max_depth
        and (xmax - xmin) > 2.0
        and (ymax - ymin) > 2.0
    )
    children = []
    child_meshes = []
    if split:
        mx = 0.5 * (xmin + xmax)
        my = 0.5 * (ymin + ymax)
        quads = (
            (xmin, mx, ymin, my),
            (mx, xmax, ymin, my),
            (xmin, mx, my, ymax),
            (mx, xmax, my, ymax),
        )
        for q in quads:
            child = _build_quadtree_lod(
                east, north, up, uvs, faces, face_cx, face_cy,
                q[0], q[1], q[2], q[3],
                depth + 1, max_depth, face_target, data_dir, texture_uri, tile_id, pages,
                cand=sel,
            )
            if child is not None:
                children.append(child[0])
                if child[1] is not None:
                    child_meshes.append(child[1])
    lod_cell = 0.0
    if children:
        extracted = None
        if child_meshes:
            extracted, lod_cell = _parent_lod_mesh(child_meshes, xmax - xmin, ymax - ymin, face_target)
        del child_meshes
    else:
        content = sel if sel.size else sel_exact
        extracted = _extract_tile_mesh(east, north, up, uvs, faces, content, pages)
    if extracted is None:
        if not children:
            return None
        if len(children) == 1:
            return children[0], None
        return (
            {"boundingVolume": {"box": _box_volume(xmin - 2.0, xmax + 2.0, ymin - 2.0, ymax + 2.0, float(up.min()) - 2.0, float(up.max()) + 2.0)},
             "geometricError": max((xmax - xmin), (ymax - ymin)) * 0.35,
             "refine": "REPLACE",
             "children": children},
            None,
        )
    pos, tile_uv, indices, tile_pages = extracted
    name = f"t{tile_id[0]:05d}.b3dm"
    tile_id[0] += 1
    glb = write_glb(pos, tile_uv, indices, texture_uri, tile_pages)
    (data_dir / name).write_bytes(write_b3dm(glb))
    bx0 = float(min(xmin, pos[:, 0].min()))
    bx1 = float(max(xmax, pos[:, 0].max()))
    by0 = float(min(ymin, pos[:, 1].min()))
    by1 = float(max(ymax, pos[:, 1].max()))
    zmin = float(pos[:, 2].min())
    zmax = float(pos[:, 2].max())
    box_pad = max(2.0, pad)
    # geometricError = sai so thuc cua LOD (o gom dinh); qua lon → Cesium luon tai tile la.
    ge = 0.0
    if children:
        ge = max(lod_cell, 1.05 * max(float(c.get("geometricError", 0.0)) for c in children), 0.05)
    node = {
        "boundingVolume": {"box": _box_volume(bx0 - box_pad, bx1 + box_pad, by0 - box_pad, by1 + box_pad, zmin - box_pad, zmax + box_pad)},
        "geometricError": float(ge),
        "refine": "REPLACE",
        "content": {"uri": f"Data/{name}"},
    }
    if children:
        node["children"] = children
    log(f"-- tile {name}  faces={indices.size // 3:,}  depth={depth}" + ("  (LOD gom dinh)" if children else ""))
    return node, extracted


def run_build(args: argparse.Namespace) -> Path:
    import numpy as np

    args.max_zoom = max(0, min(MAP_MAX_ZOOM, int(args.max_zoom)))
    output = Path(args.output).expanduser().resolve()
    tiled_dir = output / "tiled"
    obj_path = output / "model" / "model.obj"
    tileset = tiled_dir / "tileset.json"
    stamp = tiled_dir / ".max-zoom"
    if not obj_path.is_file():
        raise FileNotFoundError(f"Khong thay OBJ: {obj_path}")
    stamp_zoom = None
    if stamp.is_file():
        try:
            stamp_zoom = int(stamp.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            stamp_zoom = None
    if (
        tileset.is_file()
        and tileset.stat().st_size > 32
        and (tiled_dir / "Data").is_dir()
        and any((tiled_dir / "Data").glob("*.b3dm"))
        and not args.rebuild
        and stamp_zoom == args.max_zoom
        and tileset.stat().st_mtime >= obj_path.stat().st_mtime
    ):
        log(f"-- RESUME: da co {tileset} (z{stamp_zoom} b3dm), bo qua build")
        return tileset

    ensure_preview_bins(output, args)
    bin_path = _find_obj_mesh_bin(output, obj_path)
    log(f"[START] doc mesh tu OBJ qua {bin_path.name}  ({bin_path.stat().st_size} byte)")
    mesh = _read_dtm_mesh(bin_path)
    pos = mesh["pos"]
    uvs = mesh["uvs"]
    faces = mesh["indices"].reshape(-1, 3)
    log(f"-- Giu mesh goc: {obj_path} ({obj_path.stat().st_size} byte)")
    log(f"-- Dinh {pos.shape[0]:,}  tam {faces.shape[0]:,}")

    lon0, lat0 = estimate_lonlat(output)
    x0 = 0.5 * (mesh["xmin"] + mesh["xmax"])
    y0 = 0.5 * (-mesh["zmax"] + -mesh["zmin"])
    east = (pos[:, 0] - x0).astype("float64")
    north = ((-pos[:, 2]) - y0).astype("float64")
    up = pos[:, 1].astype("float64")
    face_cx = ((east[faces[:, 0]] + east[faces[:, 1]] + east[faces[:, 2]]) / 3.0).astype(np.float32)
    face_cy = ((north[faces[:, 0]] + north[faces[:, 1]] + north[faces[:, 2]]) / 3.0).astype(np.float32)

    xmin, xmax = float(east.min()), float(east.max())
    ymin, ymax = float(north.min()), float(north.max())
    extent = max(xmax - xmin, ymax - ymin, 1.0)
    leaf_hint = pixel_size_for_zoom(args.max_zoom, lat0) * max(1, int(args.grid))
    log(
        f"-- Zoom max {args.max_zoom}: lat={lat0:.5f} lon={lon0:.5f}  "
        f"extent={extent:.1f} m  leaf_hint={leaf_hint:.4f} m"
    )

    if args.rebuild or True:
        _clear_exported_tiles(tiled_dir)
    data_dir = tiled_dir / "Data"
    data_dir.mkdir(parents=True, exist_ok=True)
    tex_side = max(1024, min(8192, int(args.texture)))
    tex = _prepare_texture(output, data_dir / "texture.jpg", tex_side)
    texture_uri = "texture.jpg" if tex is not None else None
    if tex is not None:
        n_pages = 1 + len(list(data_dir.glob("texture_*.jpg")))
        log(f"-- Texture tile: {tex} ({tex.stat().st_size} byte)  {n_pages} trang")

    per_tile = max(20_000, int(getattr(args, "faces_per_tile", LEAF_FACE_TARGET)))
    log(f"[START] cat quadtree -> b3dm  (toi da {per_tile:,} tam/tile)")
    root = build_quadtree(
        east, north, up, uvs, faces, face_cx, face_cy,
        xmin, xmax, ymin, ymax,
        0, MAX_TREE_DEPTH, per_tile,
        data_dir, texture_uri, [0], mesh.get("pages"),
    )
    if root is None:
        raise RuntimeError("Khong tao duoc tile b3dm tu model.obj")
    _fit_child_bounds(root)
    root["transform"] = enu_to_ecef_matrix(lon0, lat0, 0.0)
    root["refine"] = "REPLACE"
    payload = {
        "asset": {"version": "1.0", "gltfUpAxis": "Z"},
        "geometricError": float(max(4.0 * float(root.get("geometricError", 0.0)), extent * 0.02, 1.0)),
        "root": root,
    }
    tileset.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    n_b3dm = len(list(data_dir.glob("*.b3dm")))
    try:
        stamp.write_text(str(args.max_zoom) + "\n", encoding="utf-8")
    except OSError as exc:
        log(f"-- Khong ghi {stamp}: {exc}")
    log(f"[DONE ] 3D Tiles b3dm: {tileset}  ({n_b3dm} file)")
    return tileset


def write_pnts(pos, rgb=None) -> bytes:
    import numpy as np

    pos = np.asarray(pos, dtype=np.float64).reshape(-1, 3)
    n = int(pos.shape[0])
    # POSITION_QUANTIZED uint16: 6 byte/diem thay 12, sai so <= canh hop / 131070.
    lo = pos.min(axis=0) if n else np.zeros(3)
    span = np.maximum((pos.max(axis=0) - lo) if n else np.ones(3), 1e-6)
    q = np.rint((pos - lo) / span * 65535.0).clip(0, 65535).astype(np.uint16)
    ft: dict = {
        "POINTS_LENGTH": n,
        "POSITION_QUANTIZED": {"byteOffset": 0},
        "QUANTIZED_VOLUME_OFFSET": [float(v) for v in lo],
        "QUANTIZED_VOLUME_SCALE": [float(v) for v in span],
    }
    blob = q.tobytes()
    if rgb is not None and len(rgb):
        packed = np.ascontiguousarray(rgb, dtype=np.uint8).reshape(-1, 3)
        if packed.shape[0] != n:
            packed = packed[:n]
        ft["RGB"] = {"byteOffset": n * 6}
        blob += packed.tobytes()
    blob = _pad8(blob, b"\x00")
    js = _pad8(json.dumps(ft, separators=(",", ":")).encode("utf-8"), b" ")
    total = 28 + len(js) + len(blob)
    return PNTS_HEADER.pack(b"pnts", 1, total, len(js), len(blob), 0, 0) + js + blob


def _read_dtp1_points(path: Path):
    import numpy as np

    if path.stat().st_size < LAS_DTP1_HEADER.size:
        raise ValueError(f"LAS preview qua ngan: {path}")
    with path.open("rb") as handle:
        header = handle.read(LAS_DTP1_HEADER.size)
    magic, n, flags, _pad, xmin, xmax, ymin, ymax, zmin, zmax, _w, _s, _e, _n = (
        LAS_DTP1_HEADER.unpack(header)
    )
    if magic != b"DTP1":
        raise ValueError(f"Sai magic {magic!r} trong {path}")
    n = int(n)
    off = LAS_DTP1_HEADER.size
    xyz = np.fromfile(path, dtype=np.float32, count=n * 3, offset=off).reshape(n, 3)
    rgb = None
    if int(flags) & 1:
        rgb = np.fromfile(path, dtype=np.uint8, count=n * 3, offset=off + n * 12).reshape(n, 3)
    return xyz, rgb, {
        "xmin": float(xmin), "xmax": float(xmax),
        "ymin": float(ymin), "ymax": float(ymax),
        "zmin": float(zmin), "zmax": float(zmax),
    }


def _load_las_points(output: Path):
    import numpy as np

    las = output / "point_cloud" / "point_cloud.las"
    preview = output / ".preview" / "points-las-full.bin"
    preview_fresh = (
        preview.is_file()
        and preview.stat().st_size > 96
        and (not las.is_file() or preview.stat().st_mtime >= las.stat().st_mtime)
    )
    if preview_fresh:
        log(f"-- Doc LAS preview {preview.name} ({preview.stat().st_size:,} byte)")
        return _read_dtp1_points(preview)
    if preview.is_file():
        log(f"-- Bo qua {preview.name} (cu hon LAS), doc truc tiep LAS")
    if not las.is_file():
        raise FileNotFoundError(f"Khong thay LAS: {las}")
    import laspy

    log(f"[START] doc {las} ({las.stat().st_size:,} byte)")
    chunks_xyz = []
    chunks_rgb = []
    scale16 = None
    with laspy.open(str(las)) as reader:
        dim = set(reader.header.point_format.dimension_names)
        has_rgb = {"red", "green", "blue"}.issubset(dim)
        for pts in reader.chunk_iterator(400_000):
            count = int(len(pts))
            if count <= 0:
                continue
            xyz = np.empty((count, 3), dtype=np.float32)
            xyz[:, 0] = np.asarray(pts.x, dtype=np.float32)
            xyz[:, 1] = np.asarray(pts.y, dtype=np.float32)
            xyz[:, 2] = np.asarray(pts.z, dtype=np.float32)
            chunks_xyz.append(xyz)
            if has_rgb:
                r = np.asarray(pts.red)
                g = np.asarray(pts.green)
                b = np.asarray(pts.blue)
                if scale16 is None:
                    scale16 = int(np.max(r)) > 255 if r.size else False
                if scale16:
                    rgb = np.column_stack((r >> 8, g >> 8, b >> 8)).astype(np.uint8)
                else:
                    rgb = np.column_stack((r, g, b)).astype(np.uint8)
                chunks_rgb.append(rgb)
    if not chunks_xyz:
        raise ValueError("LAS rong")
    xyz = np.concatenate(chunks_xyz, axis=0)
    rgb = np.concatenate(chunks_rgb, axis=0) if chunks_rgb else None
    stats = {
        "xmin": float(xyz[:, 0].min()), "xmax": float(xyz[:, 0].max()),
        "ymin": float(xyz[:, 1].min()), "ymax": float(xyz[:, 1].max()),
        "zmin": float(xyz[:, 2].min()), "zmax": float(xyz[:, 2].max()),
    }
    log(f"-- Dinh LAS {xyz.shape[0]:,}")
    return xyz, rgb, stats


def build_point_quadtree(
    east, north, up, rgb, sel, xmin, xmax, ymin, ymax,
    depth: int, max_depth: int, point_target: int, data_dir: Path, tile_id: list[int],
    written: list[int],
) -> dict | None:
    """Refine ADD: tile giu toi da point_target diem (mau deu), phan con lai chia 4 xuong con.
    Moi diem chi nam trong dung 1 file .pnts → it file, khong trung, du 100% diem."""
    import numpy as np

    if sel.size == 0:
        return None
    width = xmax - xmin
    height = ymax - ymin
    split = (
        sel.size > point_target
        and depth < max_depth
        and max(width, height) > MIN_SPLIT_METERS
    )
    if split:
        step = int(math.ceil(sel.size / point_target))
        keep = np.zeros(sel.size, dtype=bool)
        keep[::step] = True
        own = sel[keep]
        rest = sel[~keep]
        del keep
    else:
        own = sel
        rest = sel[:0]

    sub_up = up[sel]
    zmin = float(sub_up.min())
    zmax = float(sub_up.max())
    del sub_up

    children = []
    if rest.size:
        e = east[rest]
        nrt = north[rest]
        mx = 0.5 * (xmin + xmax)
        my = 0.5 * (ymin + ymax)
        for x_closed, y_closed, q in (
            (False, False, (xmin, mx, ymin, my)),
            (True, False, (mx, xmax, ymin, my)),
            (False, True, (xmin, mx, my, ymax)),
            (True, True, (mx, xmax, my, ymax)),
        ):
            xe = (e <= q[1]) if x_closed else (e < q[1])
            yn = (nrt <= q[3]) if y_closed else (nrt < q[3])
            mask = (e >= q[0]) & xe & (nrt >= q[2]) & yn
            child = build_point_quadtree(
                east, north, up, rgb, rest[mask], q[0], q[1], q[2], q[3],
                depth + 1, max_depth, point_target, data_dir, tile_id, written,
            )
            if child is not None:
                children.append(child)
        del e, nrt

    pos = np.column_stack([east[own], north[own], up[own]]).astype(np.float32, copy=False)
    tile_rgb = rgb[own] if rgb is not None else None
    name = f"p{tile_id[0]:05d}.pnts"
    tile_id[0] += 1
    (data_dir / name).write_bytes(write_pnts(pos, tile_rgb))
    written[0] += int(own.size)
    pad = 2.0
    # ADD: sai so khi chua tai con = khoang cach diem cua tile nay.
    ge = 0.0
    if children:
        spacing = math.sqrt(max(width, 0.01) * max(height, 0.01) / max(int(own.size), 1))
        ge = max(spacing, 1.05 * max(float(c.get("geometricError", 0.0)) for c in children), 0.01)
    node = {
        "boundingVolume": {"box": _box_volume(xmin - pad, xmax + pad, ymin - pad, ymax + pad, zmin - pad, zmax + pad)},
        "geometricError": float(ge),
        "refine": "ADD",
        "content": {"uri": f"Data/{name}"},
    }
    if children:
        node["children"] = children
    log(f"-- tile {name}  points={pos.shape[0]:,}  depth={depth}  children={len(children)}")
    return node


def run_build_points(args: argparse.Namespace) -> Path:
    args.max_zoom = max(0, min(MAP_MAX_ZOOM, int(args.max_zoom)))
    output = Path(args.output).expanduser().resolve()
    las = output / "point_cloud" / "point_cloud.las"
    tiled_dir = output / "tiled-las"
    tileset = tiled_dir / "tileset.json"
    stamp = tiled_dir / ".max-zoom"
    src_mtime = las.stat().st_mtime if las.is_file() else 0
    preview = output / ".preview" / "points-las-full.bin"
    if preview.is_file():
        src_mtime = max(src_mtime, preview.stat().st_mtime)
    stamp_zoom = None
    if stamp.is_file():
        try:
            stamp_zoom = int(stamp.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            stamp_zoom = None
    if (
        tileset.is_file()
        and tileset.stat().st_size > 32
        and (tiled_dir / "Data").is_dir()
        and any((tiled_dir / "Data").glob("*.pnts"))
        and not args.rebuild
        and stamp_zoom == args.max_zoom
        and tileset.stat().st_mtime >= src_mtime
    ):
        log(f"-- RESUME: da co {tileset} (z{stamp_zoom} pnts), bo qua build")
        return tileset

    xyz, rgb, stats = _load_las_points(output)
    lon0, lat0 = estimate_lonlat(output)
    x0 = 0.5 * (stats["xmin"] + stats["xmax"])
    y0 = 0.5 * (stats["ymin"] + stats["ymax"])
    npts = int(xyz.shape[0])
    east = (xyz[:, 0] - x0).astype("float32", copy=False)
    north = (xyz[:, 1] - y0).astype("float32", copy=False)
    up = xyz[:, 2].astype("float32", copy=False)
    del xyz
    xmin, xmax = float(east.min()), float(east.max())
    ymin, ymax = float(north.min()), float(north.max())
    extent = max(xmax - xmin, ymax - ymin, 1.0)
    log(
        f"-- Zoom max {args.max_zoom}: lat={lat0:.5f} lon={lon0:.5f}  "
        f"extent={extent:.1f} m  points={npts:,}"
    )
    _clear_exported_tiles(tiled_dir)
    data_dir = tiled_dir / "Data"
    data_dir.mkdir(parents=True, exist_ok=True)
    log("[START] cat quadtree -> pnts")
    import numpy as np

    sel = np.arange(npts, dtype=np.int64 if npts > 2**31 - 1 else np.int32)
    per_tile = max(50_000, int(getattr(args, "points_per_tile", POINT_TILE_TARGET)))
    log(f"-- pnts: refine ADD, toi da {per_tile:,} diem/tile, moi diem ghi 1 lan")
    written = [0]
    root = build_point_quadtree(
        east, north, up, rgb, sel, xmin, xmax, ymin, ymax,
        0, POINT_MAX_TREE_DEPTH, per_tile, data_dir, [0], written,
    )
    if root is None:
        raise RuntimeError("Khong tao duoc tile pnts tu LAS")
    if written[0] != npts:
        raise RuntimeError(f"pnts thieu/thua diem: ghi {written[0]:,} / LAS {npts:,}")
    log(f"-- Kiem tra: da ghi {written[0]:,} / {npts:,} diem (du 100%)")
    _fit_child_bounds(root)
    root["transform"] = enu_to_ecef_matrix(lon0, lat0, 0.0)
    root["refine"] = "ADD"
    payload = {
        "asset": {"version": "1.0", "gltfUpAxis": "Z"},
        "geometricError": float(max(4.0 * float(root.get("geometricError", 0.0)), extent * 0.02, 1.0)),
        "root": root,
    }
    tileset.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    n_pnts = len(list(data_dir.glob("*.pnts")))
    try:
        stamp.write_text(str(args.max_zoom) + "\n", encoding="utf-8")
    except OSError as exc:
        log(f"-- Khong ghi {stamp}: {exc}")
    log(f"[DONE ] 3D Tiles pnts: {tileset}  ({n_pnts} file)")
    return tileset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chuyen model.obj / LAS thanh Cesium 3D Tiles")
    parser.add_argument("output", nargs="?", default=str(HERE / "output-3d"))
    parser.add_argument("--grid", type=int, default=256, help="Goi y kich thuoc tile (pixel Web Mercator)")
    parser.add_argument(
        "--max-zoom",
        type=int,
        default=28,
        help="Muc zoom toi da (mac dinh 28)",
    )
    parser.add_argument("--texture", type=int, default=8192, help="Canh texture JPEG trong tiled/Data (toi da 8192, Cesium)")
    parser.add_argument("--gpu-mask", default="10")
    parser.add_argument("--cpu-only", action="store_true")
    parser.add_argument("--rebuild", action="store_true", help="Xoa preview + tiled cu va build lai")
    parser.add_argument("--skip-preview", action="store_true", help="Khong chay build-bin-jpg")
    parser.add_argument("--skip-las", action="store_true", help="Preview: bo LAS")
    parser.add_argument("--skip-gzip", action="store_true", help="Preview: bo gzip")
    parser.add_argument(
        "--faces-per-tile",
        type=int,
        default=LEAF_FACE_TARGET,
        metavar="N",
        help=f"So tam giac toi da moi file .b3dm la (mac dinh {LEAF_FACE_TARGET:,}). Lon hon = it file hon.",
    )
    parser.add_argument(
        "--points-per-tile",
        type=int,
        default=POINT_TILE_TARGET,
        metavar="N",
        help=f"So diem toi da moi file .pnts (mac dinh {POINT_TILE_TARGET:,}). Lon hon = it file hon.",
    )
    parser.add_argument("--no-relaunch", action="store_true")
    parser.add_argument(
        "--points",
        action="store_true",
        help="Chi tile LAS -> tiled-las/*.pnts",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Tile ca mesh (tiled/*.b3dm) va LAS (tiled-las/*.pnts)",
    )
    parser.add_argument(
        "--fix-bounds",
        action="store_true",
        help="Chi sua hop bao trong tileset.json da co (cha chua tron con), khong build lai tile",
    )
    return parser.parse_args()


def fix_tileset_bounds(tileset: Path) -> None:
    if not tileset.is_file():
        log(f"-- Bo qua (khong co): {tileset}")
        return
    payload = json.loads(tileset.read_text(encoding="utf-8"))
    _fit_child_bounds(payload["root"])
    tileset.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    log(f"[DONE ] Sua hop bao: {tileset}")


def main() -> None:
    args = parse_args()
    if args.fix_bounds:
        output = Path(args.output).expanduser().resolve()
        fix_tileset_bounds(output / "tiled" / "tileset.json")
        fix_tileset_bounds(output / "tiled-las" / "tileset.json")
        return
    if args.all:
        mesh = run_build(args)
        log(f"=== XONG mesh === {mesh}")
        points = run_build_points(args)
        log(f"=== XONG las === {points}")
        return
    tileset = run_build_points(args) if args.points else run_build(args)
    log(f"=== XONG === {tileset}")


if __name__ == "__main__":
    main()
