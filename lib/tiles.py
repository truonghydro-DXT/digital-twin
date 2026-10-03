"""
Tạo và lưu XYZ tiles (z/x/y.png) từ GeoTIFF theo từng mức zoom.

Cấu trúc thư mục đầu ra:
    <output_dir>/<z>/<x>/<y>.png

Chạy độc lập:
    python tiles.py data/Demo_transparent_mosaic_group1.tif --zoom 12-15
    python tiles.py data/file.tif -o tiles/cache/file --min-zoom 10 --max-zoom 16
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import shutil
import threading
import time
import warnings
from io import BytesIO
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.errors import NotGeoreferencedWarning
from rasterio.transform import Affine, from_bounds
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import from_bounds as window_from_bounds


TILE_SIZE = 256
WEB_MERCATOR = "EPSG:3857"
DEFAULT_CACHE_ROOT = Path(__file__).resolve().parent / "tiles" / "cache"
# RGB <= ngưỡng này coi là nền ngoài dữ liệu (Metashape hay fill 0).
BLACK_FILL_MAX = 2
ORTHO_CACHE_DIR_NAME = ".ortho-tiles"


class RasterCorruptError(RuntimeError):
    """GeoTIFF không đọc được pixel (tile TIFF hỏng / I/O lỗi)."""


def _normalize_raster_path_str(raster_path: str | Path) -> str:
    """Chuẩn hoá path để so khớp ổn định trên Windows (normcase + abspath)."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(raster_path))))


def _raster_identity_key(raster_path: str | Path) -> str:
    """
    Khóa định danh file GeoTIFF — cùng một file vật lý luôn cho cùng khóa,
    kể cả khi chuỗi đường dẫn khác nhau (hoa/thường, slash, resolve...).
    """
    norm = _normalize_raster_path_str(raster_path)
    try:
        st = os.stat(norm)
        # Windows/NTFS và Unix: (dev, ino) nhận diện inode; kèm size để tránh trùng ino=0
        ino = getattr(st, "st_ino", 0) or 0
        dev = getattr(st, "st_dev", 0) or 0
        if ino:
            return f"ino:{dev}:{ino}:{st.st_size}"
        mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
        return f"meta:{norm}|{st.st_size}|{mtime_ns}"
    except OSError:
        return f"path:{norm}"


def cache_key_for_raster(raster_path: str | Path) -> str:
    """
    Một GeoTIFF ↔ một thư mục cache (khi không dùng .ortho-tiles phẳng).
    Chỉ hash đường dẫn chuẩn hoá — không inode/mtime/version để tránh nhân bản.
    """
    norm = _normalize_raster_path_str(raster_path)
    stem = Path(norm).stem
    safe_stem = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE).strip("_")[:80] or "raster"
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]
    return f"{safe_stem}_{digest}"


def is_flat_ortho_cache_root(cache_root: str | Path | None) -> bool:
    return cache_root is not None and Path(cache_root).name == ORTHO_CACHE_DIR_NAME


def cache_dir_for_raster(
    raster_path: str | Path, cache_root: str | Path | None = None
) -> Path:
    root = Path(cache_root) if cache_root else DEFAULT_CACHE_ROOT
    # Một GeoTIFF / một OUTPUT: tiles nằm thẳng .ortho-tiles/{z}/{x}/{y}.png
    if is_flat_ortho_cache_root(cache_root):
        return root
    return root / cache_key_for_raster(raster_path)


def flatten_ortho_cache_root(cache_root: str | Path) -> None:
    """Gộp thư mục hash cũ (orthomosaic_xxx, …_a1) vào .ortho-tiles/{z}/ rồi xóa."""
    root = Path(cache_root)
    if not root.is_dir():
        return
    legacy = [p for p in root.iterdir() if p.is_dir() and not p.name.isdigit()]
    if not legacy:
        return
    legacy.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for src in legacy:
        try:
            for zdir in src.iterdir():
                if not zdir.is_dir() or not zdir.name.isdigit():
                    continue
                dest = root / zdir.name
                if dest.exists():
                    _merge_zoom_dir(zdir, dest)
                else:
                    shutil.move(str(zdir), str(dest))
        except OSError:
            pass
        shutil.rmtree(src, ignore_errors=True)


def _merge_zoom_dir(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for xdir in src.iterdir():
        if not xdir.is_dir():
            continue
        dx = dest / xdir.name
        dx.mkdir(parents=True, exist_ok=True)
        for tile in xdir.iterdir():
            if tile.is_file() and not (dx / tile.name).exists():
                try:
                    shutil.move(str(tile), str(dx / tile.name))
                except OSError:
                    pass


def get_tile_path(output_dir: str | Path, z: int, x: int, y: int) -> Path:
    return Path(output_dir) / str(z) / str(x) / f"{y}.png"


def punch_black_background(rgba: np.ndarray, max_value: int = BLACK_FILL_MAX) -> np.ndarray:
    """Alpha = 0 với pixel RGB gần đen (nền ngoài vùng ảnh thật)."""
    if rgba.ndim != 3 or rgba.shape[2] < 4:
        return rgba
    fill = np.all(rgba[:, :, :3] <= max_value, axis=2)
    rgba[fill, 3] = 0
    return rgba


def empty_tile_buffer() -> BytesIO:
    buffer = BytesIO()
    Image.fromarray(np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype=np.uint8), mode="RGBA").save(
        buffer, format="PNG"
    )
    buffer.seek(0)
    return buffer


def zoom_has_tiles(
    raster_path: str | Path, z: int, cache_root: str | Path | None = None
) -> bool:
    """True nếu mức zoom z đã có ít nhất 1 tile PNG được lưu."""
    zoom_dir = cache_dir_for_raster(raster_path, cache_root) / str(z)
    if not zoom_dir.is_dir():
        return False
    try:
        next(zoom_dir.rglob("*.png"))
        return True
    except StopIteration:
        return False


def list_cached_zooms(
    raster_path: str | Path, cache_root: str | Path | None = None
) -> list[int]:
    """Danh sách các mức zoom đã có tiles lưu trên disk."""
    cache_dir = cache_dir_for_raster(raster_path, cache_root)
    if not cache_dir.is_dir():
        return []

    zooms: list[int] = []
    for child in cache_dir.iterdir():
        if not child.is_dir() or not child.name.isdigit():
            continue
        z = int(child.name)
        if zoom_has_tiles(raster_path, z, cache_root):
            zooms.append(z)
    return sorted(zooms)


def clear_raster_cache(raster_path: str | Path, cache_root: str | Path | None = None) -> bool:
    """Xóa thư mục cache tiles của raster. Trả True nếu đã xóa."""
    cache_dir = cache_dir_for_raster(raster_path, cache_root)
    if not cache_dir.exists():
        return False
    shutil.rmtree(cache_dir, ignore_errors=True)
    return True


def get_or_render_tile(
    src,
    raster_path: str | Path,
    z: int,
    x: int,
    y: int,
    cache_root: str | Path | None = None,
    write_cache: bool = True,
):
    """
    Cơ chế cache theo từng tile:
    - Đã có file tile trên disk -> đọc tile đã lưu
    - Chưa có -> render trực tiếp từ GeoTIFF; nếu write_cache=True thì lưu lại
      để lần load sau dùng cache

    Returns:
        (payload, from_cache)
    """
    out_dir = cache_dir_for_raster(raster_path, cache_root)
    path = get_tile_path(out_dir, z, x, y)
    if path.is_file():
        return path, True

    png = render_tile(src, z, x, y)
    if write_cache:
        # Không cache tile hoàn toàn trong suốt (tránh "mất ảnh" ở một số mức zoom)
        try:
            png.seek(0)
            img = Image.open(png)
            arr = np.array(img)
            png.seek(0)
            if arr.ndim == 3 and arr.shape[2] >= 4 and not np.any(arr[:, :, 3]):
                return png, False
        except Exception:
            png.seek(0)
        saved = Path(save_tile(png, out_dir, z, x, y))
        return saved, False
    return png, False


def tile_bounds_3857(z: int, x: int, y: int):
    """Bounds (minx, miny, maxx, maxy) của tile XYZ trong EPSG:3857."""
    n = 2 ** z

    def lon_from_tile(tile_x):
        return tile_x / n * 360.0 - 180.0

    def lat_from_tile(tile_y):
        mercator = math.pi * (1 - 2 * tile_y / n)
        return math.degrees(math.atan(math.sinh(mercator)))

    west = lon_from_tile(x)
    east = lon_from_tile(x + 1)
    north = lat_from_tile(y)
    south = lat_from_tile(y + 1)

    def lonlat_to_mercator(lon, lat):
        x_merc = lon * 20037508.342789244 / 180.0
        y_merc = math.log(math.tan((90.0 + lat) * math.pi / 360.0))
        y_merc = y_merc * 20037508.342789244 / math.pi
        return x_merc, y_merc

    minx, miny = lonlat_to_mercator(west, south)
    maxx, maxy = lonlat_to_mercator(east, north)
    return minx, miny, maxx, maxy


def lonlat_to_tile(lon: float, lat: float, z: int):
    """Chuyển lon/lat (EPSG:4326) sang chỉ số tile XYZ tại zoom z."""
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    x = max(0, min(n - 1, x))
    y = max(0, min(n - 1, y))
    return x, y


def lonlat_bounds_to_tile_range(
    west: float, south: float, east: float, north: float, z: int
) -> tuple[int, int, int, int]:
    """Tile range (x_min, x_max, y_min, y_max) bao phủ bbox lon/lat tại zoom z."""
    west = max(-180.0, min(180.0, west))
    east = max(-180.0, min(180.0, east))
    south = max(-85.05112878, min(85.05112878, south))
    north = max(-85.05112878, min(85.05112878, north))

    x_min, y_max = lonlat_to_tile(west, south, z)
    x_max, y_min = lonlat_to_tile(east, north, z)
    if x_min > x_max:
        x_min, x_max = x_max, x_min
    if y_min > y_max:
        y_min, y_max = y_max, y_min
    return x_min, x_max, y_min, y_max


def ensure_viewport_tiles(
    src,
    raster_path: str | Path,
    z: int,
    west: float,
    south: float,
    east: float,
    north: float,
    cache_root: str | Path | None = None,
    write_cache: bool = True,
    max_tiles: int = 128,
) -> dict:
    """
    Sinh/lưu tiles giao với khung nhìn (và giao với extent raster).

    Returns:
        dict: total, from_cache, rendered, skipped_cap, tile_range
    """
    x_min, x_max, y_min, y_max = lonlat_bounds_to_tile_range(west, south, east, north, z)

    # Chỉ xử lý phần giao với footprint GeoTIFF
    try:
        rx_min, rx_max, ry_min, ry_max = raster_tile_range(src, z)
        x_min = max(x_min, rx_min)
        x_max = min(x_max, rx_max)
        y_min = max(y_min, ry_min)
        y_max = min(y_max, ry_max)
    except Exception:
        pass

    if x_min > x_max or y_min > y_max:
        return {
            "total": 0,
            "from_cache": 0,
            "rendered": 0,
            "skipped_cap": 0,
            "tile_range": None,
        }

    tiles = [(x, y) for x in range(x_min, x_max + 1) for y in range(y_min, y_max + 1)]
    skipped_cap = 0
    if len(tiles) > max_tiles:
        skipped_cap = len(tiles) - max_tiles
        tiles = tiles[:max_tiles]

    from_cache = 0
    rendered = 0
    for x, y in tiles:
        _, hit = get_or_render_tile(
            src,
            raster_path,
            z,
            x,
            y,
            cache_root=cache_root,
            write_cache=write_cache,
        )
        if hit:
            from_cache += 1
        else:
            rendered += 1

    return {
        "total": len(tiles),
        "from_cache": from_cache,
        "rendered": rendered,
        "skipped_cap": skipped_cap,
        "tile_range": {
            "x_min": x_min,
            "x_max": x_max,
            "y_min": y_min,
            "y_max": y_max,
        },
    }


def raster_tile_range(src, z: int):
    """
    Trả về (x_min, x_max, y_min, y_max) bao phủ bounds raster tại zoom z.
    Bounds được chuyển sang EPSG:4326 trước khi map sang tile index.
    """
    if src.crs is None:
        raise RuntimeError("Raster CRS không xác định")

    left, bottom, right, top = transform_bounds(
        src.crs, "EPSG:4326", *src.bounds, densify_pts=21
    )

    # Clamp để tránh ra ngoài Web Mercator
    left = max(-180.0, min(180.0, left))
    right = max(-180.0, min(180.0, right))
    bottom = max(-85.05112878, min(85.05112878, bottom))
    top = max(-85.05112878, min(85.05112878, top))

    x_min, y_max = lonlat_to_tile(left, bottom, z)
    x_max, y_min = lonlat_to_tile(right, top, z)

    if x_min > x_max:
        x_min, x_max = x_max, x_min
    if y_min > y_max:
        y_min, y_max = y_max, y_min

    return x_min, x_max, y_min, y_max


def render_tile(src, z: int, x: int, y: int) -> BytesIO:
    """Render một tile PNG 256x256 (RGBA) từ raster đã mở."""
    if src.crs is None:
        raise RuntimeError("Raster CRS không xác định")

    webmerc_bounds = tile_bounds_3857(z, x, y)
    # Zoom rất cao: bounds có thể suy biến -> không invert được geotransform
    if not (webmerc_bounds[0] < webmerc_bounds[2] and webmerc_bounds[1] < webmerc_bounds[3]):
        return empty_tile_buffer()

    tile_transform = from_bounds(*webmerc_bounds, TILE_SIZE, TILE_SIZE)
    if abs(tile_transform.determinant) < 1e-30:
        return empty_tile_buffer()

    try:
        src_bounds = transform_bounds(WEB_MERCATOR, src.crs, *webmerc_bounds, densify_pts=21)
    except Exception:
        return empty_tile_buffer()

    overlap_left = max(src_bounds[0], src.bounds.left)
    overlap_bottom = max(src_bounds[1], src.bounds.bottom)
    overlap_right = min(src_bounds[2], src.bounds.right)
    overlap_top = min(src_bounds[3], src.bounds.top)

    if overlap_left >= overlap_right or overlap_bottom >= overlap_top:
        return empty_tile_buffer()

    src_window = window_from_bounds(
        overlap_left, overlap_bottom, overlap_right, overlap_top, transform=src.transform
    )
    src_window = src_window.round_offsets().round_lengths()
    src_height = int(src_window.height)
    src_width = int(src_window.width)

    # Zoom > độ phân giải gốc: window làm tròn còn 0 pixel -> không đọc/reproject được
    if src_width <= 0 or src_height <= 0:
        return empty_tile_buffer()

    downsample = max(src_width / float(TILE_SIZE), src_height / float(TILE_SIZE), 1.0)
    read_height = max(1, int(round(src_height / downsample)))
    read_width = max(1, int(round(src_width / downsample)))

    # rasterio + NumPy 2.5: đọc window/out_shape emit DeprecationWarning nội bộ (chưa fix)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="Setting the shape on a NumPy array has been deprecated.*",
                category=DeprecationWarning,
            )
            warnings.filterwarnings(
                "ignore",
                message="The dataset's nodata attribute is shadowing the alpha band.*",
                category=UserWarning,
            )
            data = src.read(
                window=src_window,
                out_shape=(src.count, read_height, read_width),
                resampling=Resampling.nearest,
            )
            # RGBA / nodata=0: read_masks(band1) bị nodata che alpha → tile trống ở zoom thấp
            if src.count >= 4:
                mask = np.clip(data[3], 0, 255).astype(np.uint8)
                rgb_ok = np.any(data[:3] > BLACK_FILL_MAX, axis=0)
                mask = np.where(rgb_ok, mask, 0).astype(np.uint8)
            elif src.count >= 3:
                mask = np.where(np.any(data[:3] > BLACK_FILL_MAX, axis=0), 255, 0).astype(np.uint8)
            else:
                mask = src.read_masks(
                    1,
                    window=src_window,
                    out_shape=(read_height, read_width),
                    resampling=Resampling.nearest,
                )
    except Exception as exc:
        msg = str(exc).lower()
        if any(
            key in msg
            for key in (
                "read failed",
                "tiffread",
                "tifffill",
                "ireadblock",
                "got 0 bytes",
                "cannot read",
            )
        ) or exc.__class__.__name__ in ("RasterioIOError", "CPLE_AppDefinedError"):
            raise RasterCorruptError(
                f"GeoTIFF bị hỏng / không đọc được tile nội bộ: {exc}"
            ) from exc
        return empty_tile_buffer()

    src_transform = src.window_transform(src_window)
    if downsample > 1.0:
        scale_affine = Affine.scale(src_width / float(read_width), src_height / float(read_height))
        src_transform = src_transform * scale_affine

    if abs(src_transform.determinant) < 1e-30:
        return empty_tile_buffer()

    dst_data = np.zeros((src.count, TILE_SIZE, TILE_SIZE), dtype=np.float32)
    dst_mask = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint8)
    # Orthophoto RGB(A) với nodata=0: không dùng nodata khi reproject (tránh nuốt pixel tối)
    if src.count >= 3 and (src.count >= 4 or (src.nodata is not None and float(src.nodata) == 0.0)):
        src_nodata = None
    else:
        src_nodata = src.nodata if src.nodata is not None else None

    try:
        with warnings.catch_warnings():
            # NumPy destination không có geotransform; dst_transform/dst_crs đã truyền rõ.
            warnings.filterwarnings('ignore', category=NotGeoreferencedWarning)
            for band_index in range(src.count):
                reproject(
                    source=data[band_index],
                    destination=dst_data[band_index],
                    src_transform=src_transform,
                    src_crs=src.crs,
                    dst_transform=tile_transform,
                    dst_crs=WEB_MERCATOR,
                    resampling=Resampling.nearest,
                    src_nodata=src_nodata,
                    dst_nodata=None,
                )

            reproject(
                source=mask,
                destination=dst_mask,
                src_transform=src_transform,
                src_crs=src.crs,
                dst_transform=tile_transform,
                dst_crs=WEB_MERCATOR,
                resampling=Resampling.nearest,
                src_nodata=0,
                dst_nodata=0,
            )
    except Exception:
        # VD: CPLE_AppDefinedError: Cannot invert geotransform ở zoom quá cao
        return empty_tile_buffer()

    # Ưu tiên alpha band sau reproject nếu có
    if src.count >= 4:
        alpha = np.clip(dst_data[3], 0, 255).astype(np.uint8)
        # Kết hợp với mask (vùng ngoài footprint)
        alpha = np.where(dst_mask > 0, alpha, 0).astype(np.uint8)
    else:
        alpha = np.where(dst_mask > 0, 255, 0).astype(np.uint8)
    rgba = np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype=np.uint8)

    if src.count >= 3:
        rgb = np.transpose(dst_data[:3], (1, 2, 0))
        rgba[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8)
        rgba[:, :, 3] = alpha
    elif src.count == 1:
        band = dst_data[0]
        valid = alpha > 0
        if valid.any():
            values = band[valid]
            min_value = float(np.min(values))
            max_value = float(np.max(values))
        else:
            min_value = 0.0
            max_value = 1.0

        if max_value > min_value:
            normalized = (band - min_value) / (max_value - min_value) * 255.0
        else:
            normalized = np.zeros_like(band, dtype=np.float32)
        grey = np.clip(np.nan_to_num(normalized), 0, 255).astype(np.uint8)
        rgba[:, :, 0] = grey
        rgba[:, :, 1] = grey
        rgba[:, :, 2] = grey
        rgba[:, :, 3] = alpha
    else:
        first_band = np.clip(dst_data[0], 0, 255).astype(np.uint8)
        rgba[:, :, 0] = first_band
        rgba[:, :, 1] = first_band
        rgba[:, :, 2] = first_band
        rgba[:, :, 3] = alpha

    if src.count >= 3:
        punch_black_background(rgba)

    buffer = BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def save_tile(png_buffer: BytesIO, output_dir: str | Path, z: int, x: int, y: int) -> str:
    """Lưu tile vào <output_dir>/<z>/<x>/<y>.png (ghi atomic) và trả về đường dẫn file."""
    path = get_tile_path(output_dir, z, x, y)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Tên tmp unique tránh WinError 32 khi nhiều thread ghi cùng tile
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(png_buffer.getbuffer())
        for attempt in range(5):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt == 4:
                    # File đích đang bị đọc — bỏ qua cache, vẫn trả PNG trong RAM
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return str(path) if path.is_file() else str(tmp)
                time.sleep(0.02 * (attempt + 1))
    finally:
        try:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        except Exception:
            pass
    return str(path)


def generate_tiles(
    raster_path: str | Path,
    output_dir: str | Path,
    min_zoom: int = 10,
    max_zoom: int = 16,
    skip_empty: bool = True,
    overwrite: bool = False,
) -> dict:
    """
    Sinh và lưu toàn bộ tiles cho các mức zoom từ min_zoom đến max_zoom.

    Returns:
        dict thống kê: zoom_levels, saved, skipped, empty
    """
    raster_path = Path(raster_path)
    output_dir = Path(output_dir)
    if not raster_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy GeoTIFF: {raster_path}")
    if min_zoom > max_zoom:
        raise ValueError("min_zoom phải <= max_zoom")

    stats = {
        "raster": str(raster_path),
        "output_dir": str(output_dir),
        "zoom_levels": {},
        "saved": 0,
        "skipped": 0,
        "empty": 0,
    }

    with rasterio.open(raster_path) as src:
        for z in range(min_zoom, max_zoom + 1):
            x_min, x_max, y_min, y_max = raster_tile_range(src, z)
            level_saved = 0
            level_skipped = 0
            level_empty = 0
            total = (x_max - x_min + 1) * (y_max - y_min + 1)
            print(f"[z={z}] tiles {x_min}..{x_max} x {y_min}..{y_max} ({total})")

            for x in range(x_min, x_max + 1):
                for y in range(y_min, y_max + 1):
                    path = get_tile_path(output_dir, z, x, y)
                    if path.exists() and not overwrite:
                        level_skipped += 1
                        continue

                    png = render_tile(src, z, x, y)

                    if skip_empty:
                        img = Image.open(png)
                        arr = np.array(img)
                        if arr.ndim == 3 and arr.shape[2] >= 4 and not np.any(arr[:, :, 3]):
                            level_empty += 1
                            continue
                        png.seek(0)

                    save_tile(png, output_dir, z, x, y)
                    level_saved += 1

            stats["zoom_levels"][z] = {
                "x_min": x_min,
                "x_max": x_max,
                "y_min": y_min,
                "y_max": y_max,
                "saved": level_saved,
                "skipped": level_skipped,
                "empty": level_empty,
            }
            stats["saved"] += level_saved
            stats["skipped"] += level_skipped
            stats["empty"] += level_empty
            print(
                f"[z={z}] saved={level_saved} skipped={level_skipped} empty={level_empty}"
            )

    return stats


def _parse_zoom(value: str) -> tuple[int, int]:
    """Hỗ trợ --zoom 12 hoặc --zoom 12-16."""
    value = value.strip()
    if "-" in value:
        parts = value.split("-", 1)
        return int(parts[0]), int(parts[1])
    z = int(value)
    return z, z


def main():
    parser = argparse.ArgumentParser(
        description="Generate and save XYZ tiles from a GeoTIFF by zoom level"
    )
    parser.add_argument("raster", help="Path to GeoTIFF (.tif/.tiff/.vrt)")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output directory (default: tiles/<filename>)",
    )
    parser.add_argument("--min-zoom", type=int, default=10, help="Minimum zoom (default: 10)")
    parser.add_argument("--max-zoom", type=int, default=16, help="Maximum zoom (default: 16)")
    parser.add_argument(
        "--zoom",
        default=None,
        help="Single zoom or range, e.g. 13 or 12-15 (overrides min/max)",
    )
    parser.add_argument(
        "--keep-empty",
        action="store_true",
        help="Also save fully transparent tiles (skipped by default)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing tiles",
    )
    args = parser.parse_args()

    if args.zoom:
        min_zoom, max_zoom = _parse_zoom(args.zoom)
    else:
        min_zoom, max_zoom = args.min_zoom, args.max_zoom

    raster = Path(args.raster)
    output = Path(args.output) if args.output else cache_dir_for_raster(raster)

    stats = generate_tiles(
        raster_path=raster,
        output_dir=output,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        skip_empty=not args.keep_empty,
        overwrite=args.overwrite,
    )
    print(
        f"Xong. saved={stats['saved']} skipped={stats['skipped']} "
        f"empty={stats['empty']} -> {stats['output_dir']}"
    )


if __name__ == "__main__":
    main()
