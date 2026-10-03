"""
DEM 3D — ứng dụng độc lập xem GeoTIFF/DEM bằng Three.js.

Chạy:
  python app.py
  → http://<IP-local>:5002
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
import traceback
from io import BytesIO
from pathlib import Path

import numpy as np
import rasterio
from flask import Flask, jsonify, make_response, render_template, request, send_file
from PIL import Image
from rasterio.enums import Resampling
from rasterio.warp import transform, transform_bounds

from basemap import fetch_tile_image, probe_osm_connectivity

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DEM3D_DATA_DIR", str(ROOT / "data")))
TILE_CACHE_ROOT = Path(os.environ.get("TILE_CACHE_ROOT", str(ROOT / "tiles" / "cache")))
REGISTRY_FILE = DATA_DIR / "uploaded_rasters.json"
GDAL_CACHEMAX = int(os.environ.get("GDAL_CACHEMAX", "128"))
HOST = os.environ.get("DEM3D_HOST", os.environ.get("HOST", "0.0.0.0"))
PORT = int(os.environ.get("DEM3D_PORT", os.environ.get("PORT", "5002")))
ALLOWED_EXT = {".tif", ".tiff", ".vrt"}
BUNDLED_NAMES = frozenset({
    "na-san-dem.tif",
    "dem-song-hong.tif",
    "dem15_18m1.tif",
    "quang-ngai-dem.tif",
    "na-san-3d.tif",
})

app = Flask(
    __name__,
    template_folder=str(ROOT / "templates"),
    static_folder=str(ROOT / "static"),
)

_THREAD_LOCAL = threading.local()
_RASTER_LOCKS: dict[str, threading.RLock] = {}
_RASTER_LOCKS_GUARD = threading.Lock()
_REGISTRY: dict[str, str] = {}
_REGISTRY_LOCK = threading.Lock()


def _normalize_path(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _raster_identity_key(raster_path: str | Path) -> str:
    norm = _normalize_path(raster_path)
    try:
        st = os.stat(norm)
        ino = getattr(st, "st_ino", 0) or 0
        dev = getattr(st, "st_dev", 0) or 0
        if ino:
            return f"ino:{dev}:{ino}:{st.st_size}"
        mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
        return f"meta:{norm}|{st.st_size}|{mtime_ns}"
    except OSError:
        return f"path:{norm}"


def _stable_file_id(raster_path: str | Path) -> str:
    return hashlib.sha1(_raster_identity_key(raster_path).encode("utf-8")).hexdigest()[:32]


def _is_geotiff_filename(filename: str) -> bool:
    name = os.path.basename(filename or "")
    lower = name.lower()
    if not lower or ".partial." in lower:
        return False
    if ".tif." in lower or ".tiff." in lower:
        return False
    return os.path.splitext(lower)[1] in ALLOWED_EXT


def _is_under_data(path: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(DATA_DIR.resolve())
        return True
    except (ValueError, OSError):
        return False


def _thread_dataset_cache() -> dict:
    cache = getattr(_THREAD_LOCAL, "datasets", None)
    if cache is None:
        cache = {}
        _THREAD_LOCAL.datasets = cache
    return cache


def _get_raster_lock(path: str) -> threading.RLock:
    key = _normalize_path(path)
    with _RASTER_LOCKS_GUARD:
        lock = _RASTER_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _RASTER_LOCKS[key] = lock
        return lock


def _open_cached_raster(path: str):
    cache = _thread_dataset_cache()
    dataset = cache.get(path)
    if dataset is not None and not dataset.closed:
        return dataset
    with rasterio.Env(GDAL_CACHEMAX=GDAL_CACHEMAX, GTIFF_IGNORE_READ_ERRORS="YES"):
        dataset = rasterio.open(path, num_threads=1)
    cache[path] = dataset
    return dataset


def _close_raster(path: str) -> None:
    target = _normalize_path(path)
    cache = _thread_dataset_cache()
    for cache_path, dataset in list(cache.items()):
        if _normalize_path(cache_path) != target:
            continue
        try:
            if dataset is not None and not dataset.closed:
                dataset.close()
        except Exception:
            pass
        del cache[cache_path]


def _persist_registry() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        fid: path
        for fid, path in _REGISTRY.items()
        if isinstance(fid, str) and isinstance(path, str) and os.path.isfile(path)
    }
    tmp = REGISTRY_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(REGISTRY_FILE)


def _register_raster(raster_path: str, file_id: str | None = None) -> str:
    raster_path = _normalize_path(raster_path)
    file_id = file_id or _stable_file_id(raster_path)
    with _REGISTRY_LOCK:
        _REGISTRY[file_id] = raster_path
        _persist_registry()
    return file_id


def _unregister_raster(raster_path: str) -> None:
    target = _normalize_path(raster_path)
    with _REGISTRY_LOCK:
        for fid, path in list(_REGISTRY.items()):
            if _normalize_path(path) == target:
                _REGISTRY.pop(fid, None)
        _persist_registry()


def _resolve_uploaded_raster_path(file_id: str) -> str | None:
    with _REGISTRY_LOCK:
        path = _REGISTRY.get(file_id)
    if path and os.path.isfile(path):
        return path
    return None


def _scan_data_geotiffs() -> list[str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    for walk_root, dirs, names in os.walk(DATA_DIR, topdown=True, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in {"tiles", "cache"}]
        for name in names:
            if not _is_geotiff_filename(name):
                continue
            files.append(str(Path(walk_root) / name))
    files.sort(key=lambda p: os.path.basename(p).lower())
    return files


def _rel_data_path(abs_path: str) -> str:
    try:
        return str(Path(abs_path).resolve().relative_to(DATA_DIR.resolve())).replace("\\", "/")
    except ValueError:
        return os.path.basename(abs_path)


def _resolve_user_path(user_path: str) -> str | None:
    user_path = (user_path or "").strip()
    if not user_path:
        return None
    if os.path.isabs(user_path):
        candidate = Path(os.path.normpath(user_path)).resolve()
    else:
        safe = user_path.replace("/", os.sep).replace("..", "")
        candidate = (DATA_DIR / safe).resolve()
    if not _is_under_data(candidate) or not candidate.is_file():
        return None
    if not _is_geotiff_filename(candidate.name):
        return None
    return str(candidate)


def _is_dem_raster(src) -> bool:
    return src.count == 1


def _get_raster_metadata(raster_path: str) -> dict:
    src = _open_cached_raster(raster_path)
    left, bottom, right, top = src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top

    if src.crs and (src.crs.to_epsg() == 4326 or src.crs.is_geographic):
        center_lon = (left + right) / 2
        center_lat = (bottom + top) / 2
    elif src.crs:
        lon, lat = transform(src.crs, "EPSG:4326", [left, right], [bottom, top])
        center_lon = sum(lon) / 2
        center_lat = sum(lat) / 2
    else:
        raise RuntimeError("Raster CRS không xác định")

    bounds_wgs84 = None
    try:
        if src.crs:
            w, s, e, n = transform_bounds(src.crs, "EPSG:4326", left, bottom, right, top, densify_pts=5)
            bounds_wgs84 = {"west": w, "south": s, "east": e, "north": n}
    except Exception:
        bounds_wgs84 = None

    return {
        "crs": str(src.crs),
        "bounds": {"left": left, "bottom": bottom, "right": right, "top": top},
        "bounds_wgs84": bounds_wgs84,
        "center": [center_lat, center_lon],
        "band_count": src.count,
        "dtype": src.dtypes[0] if src.count else None,
        "is_dem": _is_dem_raster(src),
        "width": src.width,
        "height": src.height,
    }


def _downsample_shape(src, max_size: int):
    max_size = max(32, min(int(max_size), 512))
    scale = max(src.width, src.height) / float(max_size)
    out_w = max(2, int(round(src.width / max(scale, 1.0))))
    out_h = max(2, int(round(src.height / max(scale, 1.0))))
    return out_w, out_h


def _bootstrap_registry() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TILE_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    if REGISTRY_FILE.is_file():
        try:
            with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for fid, path in data.items():
                    if isinstance(fid, str) and isinstance(path, str) and os.path.isfile(path) and _is_under_data(path):
                        _REGISTRY[fid] = _normalize_path(path)
        except Exception:
            traceback.print_exc()
    for path in _scan_data_geotiffs():
        _register_raster(path)


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ip", methods=["GET"])
def api_ip():
    return jsonify({"ip": get_local_ip(), "hostname": socket.gethostname()})


@app.route("/api/geotiff/list", methods=["GET"])
def list_geotiffs():
    files = []
    for path in _scan_data_geotiffs():
        file_id = _register_raster(path)
        files.append({
            "file_id": file_id,
            "path": _rel_data_path(path),
            "filename": os.path.basename(path),
            "size": os.path.getsize(path),
        })
    return jsonify({
        "success": True,
        "base_dir": str(DATA_DIR),
        "files": files,
    })


@app.route("/api/geotiff/upload-single", methods=["POST"])
def upload_geotiff_single():
    if "geotiff" not in request.files:
        return jsonify({"success": False, "message": "Vui lòng chọn tệp GeoTIFF"}), 400

    file = request.files["geotiff"]
    if not file or file.filename == "":
        return jsonify({"success": False, "message": "Không có tệp được chọn"}), 400

    original_name = os.path.basename(file.filename)
    _, ext = os.path.splitext(original_name)
    ext = ext.lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"success": False, "message": "Chọn tệp GeoTIFF (.tif, .tiff, .vrt)"}), 400

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_stem = os.path.splitext(original_name)[0].replace(os.sep, "_")
    target_filename = f"{safe_stem}{ext}"
    target_path = os.path.join(str(DATA_DIR), target_filename)
    if os.path.exists(target_path):
        return jsonify({
            "success": False,
            "message": f"Trùng tên file '{target_filename}' trong thư mục data/",
        }), 409

    file.save(target_path)
    file_id = _register_raster(target_path)
    try:
        metadata = _get_raster_metadata(target_path)
        return jsonify({
            "success": True,
            "file_id": file_id,
            "filename": target_filename,
            "path": target_filename,
            **metadata,
        })
    except Exception as exc:
        traceback.print_exc()
        try:
            _close_raster(target_path)
            os.remove(target_path)
            _unregister_raster(target_path)
        except Exception:
            pass
        return jsonify({"success": False, "message": f"Lỗi metadata GeoTIFF: {exc}"}), 500


@app.route("/api/geotiff/load-path", methods=["POST"])
def load_geotiff_path():
    body = request.get_json(silent=True) or {}
    raster_path = _resolve_user_path(body.get("path") or "")
    if not raster_path:
        return jsonify({"success": False, "message": "Không tìm thấy file GeoTIFF trong data/"}), 404
    file_id = _register_raster(raster_path)
    try:
        metadata = _get_raster_metadata(raster_path)
        return jsonify({
            "success": True,
            "file_id": file_id,
            "path": _rel_data_path(raster_path),
            "filename": os.path.basename(raster_path),
            **metadata,
        })
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Lỗi metadata GeoTIFF: {exc}"}), 500


@app.route("/api/geotiff/delete", methods=["POST"])
def delete_geotiff():
    body = request.get_json(silent=True) or {}
    raster_path = _resolve_user_path(body.get("path") or "")
    if not raster_path:
        return jsonify({"success": False, "message": "Không tìm thấy tệp cần xóa"}), 404
    if os.path.basename(raster_path).lower() in BUNDLED_NAMES:
        return jsonify({"success": False, "message": "Không xóa file dữ liệu đi kèm ứng dụng."}), 403
    try:
        _close_raster(raster_path)
        os.remove(raster_path)
        _unregister_raster(raster_path)
        return jsonify({"success": True, "path": _rel_data_path(raster_path)})
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Không xóa được file: {exc}"}), 500


@app.route("/api/geotiff/info/file/<file_id>", methods=["GET"])
def geotiff_info_uploaded(file_id):
    raster_path = _resolve_uploaded_raster_path(file_id)
    if not raster_path:
        return jsonify({"success": False, "message": "Không tìm thấy GeoTIFF"}), 404
    try:
        metadata = _get_raster_metadata(raster_path)
        return jsonify({
            "success": True,
            "file_id": file_id,
            "raster_path": raster_path,
            "filename": os.path.basename(raster_path),
            **metadata,
        })
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Lỗi metadata GeoTIFF: {exc}"}), 500


@app.route("/api/geotiff/dem/file/<file_id>/terrain", methods=["GET"])
def dem_terrain(file_id):
    """Trả lưới cao độ (downsample) để render DEM 3D."""
    raster_path = _resolve_uploaded_raster_path(file_id)
    if not raster_path:
        return jsonify({"success": False, "message": "Không tìm thấy GeoTIFF đã tải lên"}), 404

    max_size = request.args.get("max_size", default=256, type=int)
    with _get_raster_lock(raster_path):
        try:
            src = _open_cached_raster(raster_path)
            out_w, out_h = _downsample_shape(src, max_size)
            is_dem = _is_dem_raster(src)
            elev = src.read(1, out_shape=(out_h, out_w), resampling=Resampling.bilinear).astype(np.float32)
            nodata = src.nodata
            nearest = src.read(1, out_shape=(out_h, out_w), resampling=Resampling.nearest).astype(np.float32)
            mask = np.isfinite(elev) & np.isfinite(nearest)
            if nodata is not None:
                mask &= nearest != nodata
                mask &= elev != nodata
            try:
                if src.count >= 4:
                    alpha = src.read(4, out_shape=(out_h, out_w), resampling=Resampling.nearest)
                    mask &= alpha > 0
                elif hasattr(src, "dataset_mask"):
                    dm = src.dataset_mask(out_shape=(out_h, out_w), resampling=Resampling.nearest)
                    mask &= dm > 0
            except Exception:
                pass

            if mask.any():
                zmin = float(np.min(elev[mask]))
                zmax = float(np.max(elev[mask]))
            else:
                zmin, zmax = 0.0, 1.0

            elev_out = elev.copy()
            elev_out[~mask] = zmin
            meta = _get_raster_metadata(raster_path)
            return jsonify({
                "success": True,
                "file_id": file_id,
                "is_dem": is_dem,
                "width": out_w,
                "height": out_h,
                "zmin": zmin,
                "zmax": zmax,
                "nodata": nodata,
                "bounds": meta.get("bounds"),
                "bounds_wgs84": meta.get("bounds_wgs84"),
                "center": meta.get("center"),
                "crs": meta.get("crs"),
                "band_count": src.count,
                "elevations_b64": base64.b64encode(elev_out.astype("<f4").tobytes()).decode("ascii"),
                "mask_b64": base64.b64encode(mask.astype(np.uint8).tobytes()).decode("ascii"),
                "texture_url": f"/api/geotiff/preview/file/{file_id}.png?max_size={max(out_w, out_h)}",
            })
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"success": False, "message": f"Lỗi đọc DEM: {exc}"}), 500


@app.route("/api/geotiff/preview/file/<file_id>.png", methods=["GET"])
def geotiff_preview_png(file_id):
    raster_path = _resolve_uploaded_raster_path(file_id)
    if not raster_path:
        return jsonify({"success": False, "message": "Không tìm thấy GeoTIFF đã tải lên"}), 404

    max_size = request.args.get("max_size", default=512, type=int)
    with _get_raster_lock(raster_path):
        try:
            src = _open_cached_raster(raster_path)
            out_w, out_h = _downsample_shape(src, max_size)
            if src.count >= 3:
                rgb = src.read([1, 2, 3], out_shape=(3, out_h, out_w), resampling=Resampling.bilinear)
                arr = np.transpose(np.clip(rgb, 0, 255), (1, 2, 0)).astype(np.uint8)
                if src.count >= 4:
                    alpha = src.read(4, out_shape=(out_h, out_w), resampling=Resampling.nearest)
                    image = Image.fromarray(np.dstack([arr, np.clip(alpha, 0, 255).astype(np.uint8)]), mode="RGBA")
                else:
                    image = Image.fromarray(arr, mode="RGB")
            else:
                band = src.read(1, out_shape=(out_h, out_w), resampling=Resampling.bilinear).astype(np.float32)
                nodata = src.nodata
                valid = np.isfinite(band)
                if nodata is not None:
                    valid &= band != nodata
                if valid.any():
                    vmin = float(np.min(band[valid]))
                    vmax = float(np.max(band[valid]))
                else:
                    vmin, vmax = 0.0, 1.0
                if vmax > vmin:
                    norm = (band - vmin) / (vmax - vmin)
                else:
                    norm = np.zeros_like(band)
                rgb = np.clip(norm * 255.0, 0, 255).astype(np.uint8)
                alpha = np.where(valid, 255, 0).astype(np.uint8)
                image = Image.fromarray(np.dstack([rgb, rgb, rgb, alpha]), mode="RGBA")

            buf = BytesIO()
            image.save(buf, format="PNG")
            buf.seek(0)
            return send_file(buf, mimetype="image/png", max_age=60)
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"success": False, "message": f"Lỗi tạo ảnh xem trước: {exc}"}), 500


@app.route("/api/basemap/osm/health", methods=["GET"])
def osm_basemap_health():
    try:
        info = probe_osm_connectivity()
        status = 200 if info.get("ok") else 503
        return jsonify({"success": bool(info.get("ok")), **info}), status
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/basemap/osm/<int:z>/<int:x>/<int:y>.png", methods=["GET"])
def osm_tile_proxy(z, x, y):
    try:
        img = fetch_tile_image(z, x, y, TILE_CACHE_ROOT / "basemap")
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png", max_age=604800)
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Không tải được tile basemap: {exc}"}), 502


@app.route("/api/basemap/osm/mosaic.png", methods=["GET"])
def osm_mosaic():
    from basemap import build_mosaic_png

    try:
        west = float(request.args["west"])
        south = float(request.args["south"])
        east = float(request.args["east"])
        north = float(request.args["north"])
        out_size = int(request.args.get("size", 1024))
    except (KeyError, TypeError, ValueError):
        return jsonify({"success": False, "message": "Thiếu hoặc sai tham số west,south,east,north"}), 400

    if east <= west or north <= south:
        return jsonify({"success": False, "message": "Bbox không hợp lệ"}), 400

    try:
        data, mw, ms, me, mn = build_mosaic_png(west, south, east, north, out_size, str(ROOT))
        resp = make_response(data)
        resp.headers["Content-Type"] = "image/png"
        resp.headers["Cache-Control"] = "public, max-age=86400"
        resp.headers["X-Osm-West"] = f"{mw:.10f}"
        resp.headers["X-Osm-South"] = f"{ms:.10f}"
        resp.headers["X-Osm-East"] = f"{me:.10f}"
        resp.headers["X-Osm-North"] = f"{mn:.10f}"
        resp.headers["Access-Control-Expose-Headers"] = "X-Osm-West, X-Osm-South, X-Osm-East, X-Osm-North"
        return resp
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Lỗi mosaic basemap: {exc}"}), 500


def main():
    _bootstrap_registry()
    geotiffs = _scan_data_geotiffs()
    local_ip = get_local_ip()
    print(f"DEM 3D data: {DATA_DIR} ({len(geotiffs)} GeoTIFF)")
    for path in geotiffs:
        print(f"  - {os.path.basename(path)}")
    print(f"Open browser: http://{local_ip}:{PORT}/")
    if HOST in {"0.0.0.0", "::"}:
        print(f"Localhost:    http://127.0.0.1:{PORT}/")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
else:
    _bootstrap_registry()
