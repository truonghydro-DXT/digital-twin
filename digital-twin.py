#!/usr/bin/env python3
r"""
Dựng digital twin photogrammetry từ dữ liệu DJI L2 trong LiDAR-image.

Dùng thư viện UAV-digital-twin (Agisoft Metashape).
File .LDR là LiDAR thô của DJI — Metashape không đọc được.

Chạy từ thư mục digital-twin (ung dung doc lap):
  .\\venv\\Scripts\\python.exe digital-twin.py --cpu-only --quality medium
  .\\venv\\Scripts\\python.exe digital-twin.py --export-only --cpu-only
  .\\venv\\Scripts\\python.exe digital-twin.py --list-only

  """


from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent
DEFAULT_IMAGES = HERE / "LiDAR-image"
DEFAULT_OUTPUT = HERE / "output-new"
LIB_ROOT = HERE / "UAV-digital-twin"

# DJI RGB mapping: _D = L2 visible, _V = Mavic/Matrice 2D, _W = wide.
PHOTO_INDEX_RE = re.compile(r"_(\d{4})_([DVW])\.(jpe?g)$", re.IGNORECASE)
PHOTO_EXTS = {".jpg", ".jpeg"}
PHOTO_CAMERA_PRIORITY = ("D", "V", "W")
COMPATIBLE_METASHAPE = {"2.1", "2.2", "2.3"}
DEFAULT_PRO_EXE = Path(r"C:\Program Files\Agisoft\Metashape Pro\metashape.exe")


@dataclass(frozen=True)
class MrkRecord:
    index: int
    timestamp: float
    lat: float
    lon: float
    ellh: float
    vn: float
    ve: float
    vv: float


@dataclass
class PhotoRecord:
    path: str
    name: str
    index: int | None
    lat: float | None = None
    lon: float | None = None
    ellh: float | None = None


_LOG_PATH: Path | None = None
_REAL_STDOUT = sys.stdout
_REAL_STDERR = sys.stderr
_CONSOLE_QUIET = False


def _is_script_log_line(line: str) -> bool:
    text = line.strip("\r")
    return len(text) >= 10 and text[2] == ":" and text[5] == ":" and text[8:10] == "  "


class _ScriptOnlyStream:
    """Chi cho qua dong log cua script (HH:MM:SS  ...), an log Metashape."""

    def __init__(self, real) -> None:
        self._real = real
        self._buf = ""

    def write(self, data) -> int:
        if not isinstance(data, str):
            data = str(data)
        self._buf += data
        while True:
            cut_n = self._buf.find("\n")
            cut_r = self._buf.find("\r")
            if cut_n < 0 and cut_r < 0:
                break
            if cut_n < 0:
                cut = cut_r
            elif cut_r < 0:
                cut = cut_n
            else:
                cut = min(cut_n, cut_r)
            line = self._buf[:cut]
            self._buf = self._buf[cut + 1 :]
            if _is_script_log_line(line):
                self._real.write(line + "\n")
                self._real.flush()
        return len(data) if isinstance(data, str) else 0

    def flush(self) -> None:
        if self._buf and _is_script_log_line(self._buf):
            self._real.write(self._buf)
            self._real.flush()
        self._buf = ""

    def reconfigure(self, *args, **kwargs) -> None:
        return None

    @property
    def encoding(self):
        return getattr(self._real, "encoding", "utf-8")

    def isatty(self) -> bool:
        return False


def quiet_metashape_console() -> None:
    global _CONSOLE_QUIET
    if _CONSOLE_QUIET:
        return
    sys.stdout = _ScriptOnlyStream(_REAL_STDOUT)
    sys.stderr = _ScriptOnlyStream(_REAL_STDERR)
    _CONSOLE_QUIET = True


def _ascii(text: str) -> str:
    text = text.replace("đ", "d").replace("Đ", "D")
    stripped = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in stripped if not unicodedata.combining(ch)).encode("ascii", "replace").decode("ascii")


def metashape_runtime() -> Any:
    import Metashape  # type: ignore[reportMissingImports]

    return cast(Any, Metashape)


def init_logger(output: Path) -> Path:
    global _LOG_PATH
    output.mkdir(parents=True, exist_ok=True)
    _LOG_PATH = output / "run.log"
    return _LOG_PATH


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    raw = f"{stamp}  {message}"
    print(_ascii(raw), flush=True)
    if _LOG_PATH is not None:
        with _LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(raw + "\n")


def die(message: str, code: int = 1) -> None:
    log(f"[ERROR] {message}")
    raise SystemExit(code)


class StepProgress:
    """In % theo dong moi (console Metashape khong hien \\r)."""

    def __init__(self, name: str, hint: str = "", every: float = 5.0) -> None:
        self.name = name
        self.every = every
        self.last = -every
        self.t0 = time.monotonic()
        extra = f"  -- {hint}" if hint else ""
        log(f"[START] {name}{extra}")

    def __call__(self, percent) -> None:
        try:
            pct = float(percent)
        except (TypeError, ValueError):
            return
        if 0.0 <= pct <= 1.0:
            pct *= 100.0
        if pct - self.last < self.every and pct < 99.5:
            return
        self.last = pct
        elapsed = time.monotonic() - self.t0
        log(f"[RUN  ] {self.name}: {pct:5.1f}%   {elapsed:.0f}s")

    def close(self) -> None:
        elapsed = time.monotonic() - self.t0
        log(f"[DONE ] {self.name}   {elapsed:.0f}s")


def with_progress(name: str, hint: str, action) -> None:
    progress = StepProgress(name, hint)
    try:
        action(progress)
    finally:
        progress.close()


def ensure_lib_on_path() -> Path:
    if not LIB_ROOT.is_dir():
        die(f"Không thấy thư viện UAV-digital-twin: {LIB_ROOT}")
    root = str(LIB_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return LIB_ROOT


def photos_in_dir(folder: Path) -> list[PhotoRecord]:
    by_cam: dict[str, list[PhotoRecord]] = {}
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_file() or entry.suffix.lower() not in PHOTO_EXTS:
            continue
        match = PHOTO_INDEX_RE.search(entry.name)
        if match is None:
            continue
        cam = match.group(2).upper()
        by_cam.setdefault(cam, []).append(
            PhotoRecord(
                path=str(entry.resolve()),
                name=entry.name,
                index=int(match.group(1)),
            )
        )
    for cam in PHOTO_CAMERA_PRIORITY:
        photos = by_cam.get(cam, [])
        if len(photos) >= 2:
            return photos
    if by_cam:
        return max(by_cam.values(), key=len)
    return []


def collect_mission_dirs(folder: Path, depth: int = 0, max_depth: int = 2) -> list[Path]:
    """Thu thập thư mục mission có ảnh RGB. DJI dump có thể lồng 1–2 cấp."""
    if not folder.is_dir():
        die(f"Không thấy thư mục ảnh: {folder}")
    if len(photos_in_dir(folder)) >= 2:
        return [folder]
    if depth >= max_depth:
        return []
    found: list[Path] = []
    for child in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        found.extend(collect_mission_dirs(child, depth + 1, max_depth))
    return found


def collect_all_mission_dirs(folders: list[Path]) -> list[Path]:
    dirs: list[Path] = []
    seen: set[str] = set()
    for folder in folders:
        resolved = folder.resolve()
        for mission in collect_mission_dirs(resolved):
            key = str(mission).lower()
            if key in seen:
                continue
            seen.add(key)
            dirs.append(mission)
    if not dirs:
        names = ", ".join(str(p) for p in folders)
        die(f"Cần ít nhất 2 ảnh DJI *_NNNN_D/V/W.JPG trong {names} (tìm thấy 0).")
    return dirs


def find_photos(folder: Path) -> list[PhotoRecord]:
    photos = photos_in_dir(folder)
    if len(photos) < 2:
        die(
            f"Cần ít nhất 2 ảnh DJI *_NNNN_D/V/W.JPG trong {folder} "
            f"(tìm thấy {len(photos)})."
        )
    return photos


def load_photos_from_missions(dirs: list[Path]) -> tuple[list[PhotoRecord], list[Path]]:
    """Đọc ảnh từng flight và gắn GPS từ .MRK của chính flight đó (index 0001 lặp lại)."""
    all_photos: list[PhotoRecord] = []
    mrk_paths: list[Path] = []
    seen: set[str] = set()
    for folder in dirs:
        photos = photos_in_dir(folder)
        if len(photos) < 2:
            continue
        mrk_path = find_mrk(folder)
        if mrk_path is not None:
            mrk_paths.append(mrk_path)
            attach_gps(photos, parse_mrk(mrk_path))
        for photo in photos:
            key = photo.path.lower()
            if key in seen:
                continue
            seen.add(key)
            all_photos.append(photo)
    all_photos.sort(key=lambda p: (p.name.lower(), p.path.lower()))
    if len(all_photos) < 2:
        die(
            "Cần ít nhất 2 ảnh DJI *_NNNN_D/V/W.JPG trong các thư mục mission "
            f"(tìm thấy {len(all_photos)})."
        )
    return all_photos, mrk_paths


def find_mrk(folder: Path) -> Path | None:
    matches = sorted(folder.glob("*.MRK"))
    return matches[0] if matches else None


def parse_mrk(path: Path) -> dict[int, MrkRecord]:
    records: dict[int, MrkRecord] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = [p.strip() for p in line.strip().split("\t") if p.strip()]
            if len(parts) < 8:
                continue
            try:
                index = int(parts[0])
                timestamp = float(parts[1])
                vn = float(parts[3].split(",")[0])
                ve = float(parts[4].split(",")[0])
                vv = float(parts[5].split(",")[0])
                lat = float(parts[6].split(",")[0])
                lon = float(parts[7].split(",")[0])
                ellh = float(parts[8].split(",")[0])
            except (ValueError, IndexError):
                continue
            records[index] = MrkRecord(index, timestamp, lat, lon, ellh, vn, ve, vv)
    return records


def attach_gps(photos: list[PhotoRecord], mrk: dict[int, MrkRecord]) -> int:
    linked = 0
    for photo in photos:
        if photo.index is None:
            continue
        rec = mrk.get(photo.index)
        if rec is None:
            continue
        photo.lat = rec.lat
        photo.lon = rec.lon
        photo.ellh = rec.ellh
        linked += 1
    return linked


def lidar_note(folders: Path | list[Path]) -> dict:
    roots = folders if isinstance(folders, list) else [folders]
    ldr: list[str] = []
    ldrt: list[str] = []
    for folder in roots:
        ldr.extend(p.name for p in sorted(folder.glob("*.LDR")))
        ldrt.extend(p.name for p in sorted(folder.glob("*.LDRT")))
    return {
        "ldr_files": ldr,
        "ldrt_files": ldrt,
        "usable_in_metashape": False,
        "note": (
            "DJI L2 .LDR/.LDRT là định dạng LiDAR thô. "
            "UAV-digital-twin / Metashape không đọc được. "
            "Chuyển sang LAS/LAZ bằng DJI Terra nếu cần ghép LiDAR."
        ),
    }


def write_catalog(
    output: Path,
    images: Path | list[Path],
    photos: list[PhotoRecord],
    mrk_path: Path | list[Path] | None,
) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    image_dirs = images if isinstance(images, list) else [images]
    mrk_paths = (
        []
        if mrk_path is None
        else (mrk_path if isinstance(mrk_path, list) else [mrk_path])
    )
    catalog = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "library": str(LIB_ROOT),
        "images": [str(p) for p in image_dirs],
        "photo_count": len(photos),
        "photos_with_gps": sum(1 for p in photos if p.lat is not None),
        "mrk": [str(p) for p in mrk_paths],
        "lidar": lidar_note(image_dirs),
        "photos": [asdict(p) for p in photos],
    }
    path = output / "mission_catalog.json"
    path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_reference_csv(output: Path, photos: list[PhotoRecord]) -> Path | None:
    rows = [p for p in photos if p.lat is not None and p.lon is not None]
    if not rows:
        return None
    path = output / "camera_reference.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Label", "Longitude", "Latitude", "Altitude"])
        for photo in rows:
            writer.writerow([Path(photo.name).stem, photo.lon, photo.lat, photo.ellh])
    return path


def print_inventory(
    photos: list[PhotoRecord],
    mrk_path: Path | list[Path] | None,
    images: Path | list[Path],
) -> None:
    gps = sum(1 for p in photos if p.lat is not None)
    image_dirs = images if isinstance(images, list) else [images]
    mrk_paths = (
        []
        if mrk_path is None
        else (mrk_path if isinstance(mrk_path, list) else [mrk_path])
    )
    log("=== LiDAR-image ===")
    log(f"  Thu muc     : {len(image_dirs)}")
    for folder in image_dirs:
        log(f"    - {folder}")
    log(f"  Anh RGB DJI : {len(photos)}")
    log(f"  Co GPS MRK  : {gps}")
    if not mrk_paths:
        log("  MRK         : khong co")
    elif len(mrk_paths) == 1:
        log(f"  MRK         : {mrk_paths[0].name}")
    else:
        log(f"  MRK         : {len(mrk_paths)} file")
        for path in mrk_paths:
            log(f"    - {path.name}")
    lidar = lidar_note(image_dirs)
    if lidar["ldr_files"]:
        log(f"  LiDAR .LDR  : {', '.join(lidar['ldr_files'])} (Metashape khong doc duoc)")
    if photos:
        first, last = photos[0], photos[-1]
        log(f"  Anh dau     : {first.name}")
        log(f"  Anh cuoi    : {last.name}")
        if first.lat is not None:
            log(f"  GPS dau     : {first.lat:.8f}, {first.lon:.8f}, {first.ellh:.3f} m")


def resolve_mesh_surface(args: argparse.Namespace) -> str:
    if getattr(args, "mesh_3d", False):
        return "arbitrary"
    surface = getattr(args, "surface_type", None)
    return surface or "heightfield"


def quality_params(level: str) -> dict:
    presets = {
        "low": {
            "match_downscale": 4,
            "depth_downscale": 8,
            "face_count": "Metashape.MediumFaceCount",
            "texture_size": 4096,
            "texture_pages": 1,
            "build_tiled": False,
        },
        "medium": {
            "match_downscale": 2,
            "depth_downscale": 4,
            "face_count": "Metashape.HighFaceCount",
            "texture_size": 8192,
            "texture_pages": 2,
            "build_tiled": False,
        },
        "high": {
            "match_downscale": 1,
            "depth_downscale": 2,
            "face_count": "Metashape.HighFaceCount",
            "texture_size": 16384,
            "texture_pages": 4,
            "build_tiled": False,
        },
    }
    if level not in presets:
        die(f"Quality không hợp lệ: {level}. Dùng low | medium | high.")
    return presets[level]


DEPTH_DOWNSCALE = {"ultra": 1, "high": 2, "medium": 4, "low": 8}


def parse_face_count(value: str | None) -> int | None:
    """None = preset (HighFaceCount); 0 = khong decimate; N = CustomFaceCount N."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", "high", "preset"):
        return None
    if text in ("max", "full", "0"):
        return 0
    try:
        n = int(float(text))
    except ValueError:
        die(f"--face-count khong hop le: {value}. Dung high | max | so mat (vd 30000000).")
    return max(0, n)


def parse_gpu_mask(value: str) -> int:
    """Nhan '10' (nhi phan, AMD o device 1) hoac so nguyen 2."""
    text = str(value).strip().lower()
    if text.startswith("0b"):
        return int(text, 2)
    if len(text) >= 2 and set(text) <= {"0", "1"}:
        return int(text, 2)
    if text.isdigit():
        return int(text, 10)
    die(f"gpu-mask khong hop le: {value}. Dung 10 (AMD), 01 (Intel), hoac 1.")
    return 0


class CompatibleSettings:
    """Settings UAV-digital-twin. Gan GPU mask truc tiep, khong enumGPUDevices."""

    def __init__(self, use_gpu: bool = True, gpu_mask: int = 2, list_gpu: bool = False) -> None:
        from src.settings import Settings  # type: ignore[reportMissingImports]

        self._impl = Settings.__new__(Settings)
        self._check_license()
        self._check_version()
        self._apply_compute(use_gpu, gpu_mask, list_gpu)

    def set_log(self, path_log: str) -> None:
        del path_log
        meta = metashape_runtime()

        try:
            meta.Application.Settings.log_enable = False
        except Exception:
            pass
        log("-- Tat log Metashape (chi giu run.log cua script)")

    @staticmethod
    def _apply_compute(use_gpu: bool, gpu_mask: int, list_gpu: bool) -> None:
        meta = metashape_runtime()

        meta.app.cpu_enable = True
        log("-- CPU enable: True (CPU van chay cac buoc khong GPU)")
        if not use_gpu:
            meta.app.gpu_mask = 0
            log("-- GPU mask: 0 (CPU only)")
            return

        if list_gpu:
            log("-- Liet ke GPU (enumGPUDevices co the treo)...")
            devices = meta.app.enumGPUDevices()
            log(f"-- GPU tim thay: {len(devices)}")
            for index, device in enumerate(devices):
                name = device.get("name", device) if isinstance(device, dict) else device
                log(f"   [{index}] {name}")

        meta.app.gpu_mask = gpu_mask
        bits = format(gpu_mask, "b")
        log(f"-- GPU mask: {gpu_mask} (binary {bits})")
        if gpu_mask == 1:
            log("-- Canh bao: mask 1 = device 0. Tren laptop Intel+AMD do thuong la Intel HD.")
            log("-- Intel HD hay TREO o 'preparing cameras images'. May nay nen --gpu-mask 10 (AMD) hoac --cpu-only.")
        else:
            log("-- mask 10 = device 1 (AMD/NVIDIA), tat device 0 (Intel HD).")

    @staticmethod
    def _check_license() -> None:
        meta = metashape_runtime()

        if meta.app.activated:
            log(f"-- Metashape license: activated ({meta.app.activated})")
            return
        raise RuntimeError(
            "Module Python Metashape chưa thấy license.\n"
            "Bản Pro đã cài trên máy (thường 2.3) không dùng chung license với wheel 2.2 trong venv.\n"
            "Chạy qua GUI đã active:\n"
            f'  "{DEFAULT_PRO_EXE}" -r "{Path(__file__).resolve()}"\n'
            "Hoặc kích hoạt wheel bằng key Agisoft: Metashape.license.activate('XXXX-...')."
        )

    @staticmethod
    def _check_version() -> None:
        meta = metashape_runtime()

        found = ".".join(str(meta.app.version).split(".")[:2])
        if found not in COMPATIBLE_METASHAPE:
            raise RuntimeError(
                f"Metashape {meta.app.version} không tương thích "
                f"(cần {sorted(COMPATIBLE_METASHAPE)})."
            )
        log(f"-- Metashape version OK: {meta.app.version}")


def find_metashape_pro_exe() -> Path | None:
    override = os.environ.get("METASHAPE_EXE")
    if override and Path(override).is_file():
        return Path(override)
    if DEFAULT_PRO_EXE.is_file():
        return DEFAULT_PRO_EXE
    return None


def launched_by_metashape() -> bool:
    exe = Path(sys.executable).resolve()
    name = exe.name.lower()
    return name.startswith("metashape") or "agisoft" in str(exe).lower()


def maybe_relaunch_via_pro() -> None:
    """Wheel 2.2 không đọc license của Pro 2.3. Chạy lại bằng metashape.exe."""
    if launched_by_metashape():
        return
    try:
        meta = metashape_runtime()
        if meta.app.activated:
            return
    except ModuleNotFoundError:
        pass

    exe = find_metashape_pro_exe()
    if exe is None:
        return

    script = Path(__file__).resolve()
    cmd = [str(exe), "-r", str(script), *sys.argv[1:]]
    log(f"-- License wheel khong khop GUI. Chay lai qua Metashape Pro: {exe}")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    raise SystemExit(subprocess.call(cmd, env=env))


def find_metashape_wheel() -> Path | None:
    assets = LIB_ROOT / "assets"
    if not assets.is_dir():
        return None
    wheels = sorted(assets.glob("Metashape-*-win_amd64.whl"))
    return wheels[0] if wheels else None


def ensure_metashape():
    meta: Any = None
    try:
        meta = metashape_runtime()
        return meta
    except ModuleNotFoundError:
        pass

    wheel = find_metashape_wheel()
    if wheel is None:
        die(
            "Chưa cài module Metashape trong venv.\n"
            f"Đặt wheel Windows vào {LIB_ROOT / 'assets'} rồi chạy lại, hoặc:\n"
            "  .\\digital-twin\\venv\\Scripts\\python.exe -m pip install "
            "<Metashape-*-win_amd64.whl>"
        )

    assert wheel is not None
    log(f"-- Cai Metashape tu {wheel.name} ...")
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", str(wheel)],
        check=False,
    )
    if result.returncode != 0:
        die(f"Không cài được Metashape từ {wheel}")

    try:
        meta = metashape_runtime()
    except ModuleNotFoundError:
        die("Đã cài wheel nhưng vẫn không import được Metashape. Kiểm tra Python 3.11 và venv.")
    return meta


def import_uav_modules():
    ensure_lib_on_path()
    ensure_metashape()
    Project = cast(Any, object())
    PhotoProcessor = cast(Any, object())
    PointCloudProcessor = cast(Any, object())
    MeshProcessor = cast(Any, object())
    GeographicProjection = cast(Any, object())
    try:
        from src.geographic_projection import GeographicProjection  # type: ignore[reportMissingImports]
        from src.mesh_processor import MeshProcessor  # type: ignore[reportMissingImports]
        from src.photo_processor import PhotoProcessor  # type: ignore[reportMissingImports]
        from src.point_cloud_processor import PointCloudProcessor  # type: ignore[reportMissingImports]
        from src.project import Project  # type: ignore[reportMissingImports]
    except ModuleNotFoundError as exc:
        die(f"Thieu module {exc.name} trong UAV-digital-twin.")
    return {
        "Project": Project,
        "PhotoProcessor": PhotoProcessor,
        "PointCloudProcessor": PointCloudProcessor,
        "MeshProcessor": MeshProcessor,
        "GeographicProjection": GeographicProjection,
    }


def apply_mrk_to_cameras(chunk, photos: list[PhotoRecord]) -> int:
    meta = metashape_runtime()

    by_label = {Path(p.name).stem: p for p in photos if p.lat is not None}
    applied = 0
    chunk.crs = meta.CoordinateSystem("EPSG::4326")
    for camera in chunk.cameras:
        photo = by_label.get(camera.label)
        if photo is None:
            continue
        camera.reference.location = meta.Vector([photo.lon, photo.lat, photo.ellh])
        camera.reference.enabled = True
        applied += 1
    return applied


def aligned_count(chunk) -> int:
    return sum(1 for cam in chunk.cameras if cam.transform is not None)


def chunk_is_georeferenced(chunk) -> bool:
    transform = chunk.transform
    return bool(transform.scale and transform.rotation and transform.translation)


def chunk_has_depth_maps(chunk) -> bool:
    return getattr(chunk, "depth_maps", None) is not None


def chunk_has_point_cloud(chunk) -> bool:
    return getattr(chunk, "point_cloud", None) is not None


def chunk_has_model(chunk) -> bool:
    return getattr(chunk, "model", None) is not None


def chunk_has_elevation(chunk) -> bool:
    return getattr(chunk, "elevation", None) is not None


def chunk_has_orthomosaic(chunk) -> bool:
    return getattr(chunk, "orthomosaic", None) is not None


def export_exists(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def dense_cloud_count(chunk) -> int:
    cloud = getattr(chunk, "point_cloud", None)
    if cloud is None:
        return 0
    for attr in ("point_count", "npoints"):
        try:
            n = int(getattr(cloud, attr))
        except (TypeError, ValueError, AttributeError):
            continue
        if n > 0:
            return n
    return 0


def mesh_build_plan(chunk, preset: dict, mesh_surface: str) -> list[tuple[str, str, dict]]:
    """Arbitrary: tat noi suy (tranh meo), uu tien depth maps; HeightField: bat noi suy."""
    n_cloud = dense_cloud_count(chunk)
    # EnabledInterpolation keo mat o vung thua diem → vo / meo (nhat la Arbitrary).
    # Disabled + closeHoles(level) vac lo nho, giu goc canh.
    use_interp = mesh_surface != "arbitrary"
    extra: dict[str, object] = {
        "interpolation": (
            "Metashape.EnabledInterpolation"
            if use_interp
            else "Metashape.DisabledInterpolation"
        ),
        "keep_depth": False,
        "split_in_blocks": False,
        "build_texture": False,
        # Mau dinh chi dung khi thieu texture; buildTexture luon chay sau → tat de bo
        # luot colorizing tung khoi (chon anh 1 luong + decimate LOD), tiet kiem ~1/2 buildModel.
        "vertex_colors": False,
        "vertex_confidence": True,
        "subdivide_task": True,
    }
    custom_faces = preset.get("face_count_custom")
    if mesh_surface == "arbitrary" and custom_faces is not None:
        # CustomFaceCount 0 = khong decimate, giu nguyen goc/canh tu depth maps.
        extra["face_count"] = "Metashape.CustomFaceCount"
        extra["face_count_custom"] = int(custom_faces)
    elif mesh_surface == "arbitrary":
        # HighFaceCount on dinh hon CustomFaceCount = n_cloud (de loe/vo khi remesh).
        extra["face_count"] = preset.get("face_count") or "Metashape.HighFaceCount"
    elif n_cloud >= 1_000_000:
        extra["face_count"] = "Metashape.CustomFaceCount"
        extra["face_count_custom"] = int(min(max(n_cloud // 2, 2_000_000), 40_000_000))
    else:
        extra["face_count"] = preset["face_count"]
    sources: list[tuple[str, str, dict]] = []
    if mesh_surface == "arbitrary":
        if chunk_has_depth_maps(chunk):
            depth_extra = dict(extra)
            # Giu depth maps de --rebuild-cloud / --rebuild-mesh khong phai dung lai depth.
            depth_extra["keep_depth"] = True
            depth_extra["split_in_blocks"] = True
            sources.append(("depth maps", "Metashape.DepthMapsData", depth_extra))
        if chunk_has_point_cloud(chunk):
            sources.append(("dense cloud (cung nguon LAS)", "Metashape.PointCloudData", extra))
    else:
        if chunk_has_point_cloud(chunk):
            sources.append(("dense cloud (cung nguon LAS)", "Metashape.PointCloudData", extra))
        if chunk_has_depth_maps(chunk):
            depth_extra = dict(extra)
            depth_extra["keep_depth"] = True
            depth_extra["split_in_blocks"] = True
            sources.append(("depth maps", "Metashape.DepthMapsData", depth_extra))
    if not sources:
        sources.append(("tie points", "Metashape.TiePointsData", extra))
    return sources


def remove_point_clouds(chunk, log_fn=None) -> None:
    clouds = list(getattr(chunk, "point_clouds", None) or [])
    if not clouds and getattr(chunk, "point_cloud", None) is not None:
        clouds = [chunk.point_cloud]
    if not clouds:
        return
    try:
        chunk.remove(clouds)
        if log_fn:
            log_fn(f"-- Xoa {len(clouds)} dense cloud cu")
    except Exception as exc:
        if log_fn:
            log_fn(f"-- Khong xoa duoc dense cloud cu: {exc}")


def close_model_holes(chunk, log_fn=None, level: int = 50) -> bool:
    """Vac lo nho/trung binh sau buildModel. level 0-100 (Metashape); thap = it keo meo."""
    model = getattr(chunk, "model", None)
    if model is None:
        return False
    closer = getattr(model, "closeHoles", None)
    if not callable(closer):
        closer = getattr(chunk, "closeHoles", None)
    if not callable(closer):
        return False
    level = int(min(100, max(0, level)))
    try:
        if log_fn:
            log_fn(f"[START] closeHoles  -- level={level}")
        try:
            closer(level=level)
        except TypeError:
            closer(level)
        if log_fn:
            log_fn("[DONE ] closeHoles")
        return True
    except Exception as exc:
        if log_fn:
            log_fn(f"-- Bo qua closeHoles: {exc}")
        return False


def existing_export_labels(output: Path) -> list[str]:
    items = [
        "digital_twin.psx",
        "point_cloud/point_cloud.las",
        "model/model.obj",
        "texture/texture.jpg",
        "dem/dem.tif",
        "orthomosaic/orthomosaic.tif",
        "report.pdf",
        "run.log",
    ]
    return [name for name in items if (output / name).is_file()]


def export_las(chunk, dest: Path) -> bool:
    """Xuat LAS: uu tien dense cloud, khong co thi dung tie points."""
    import Metashape  # type: ignore[reportMissingImports]

    dest.parent.mkdir(parents=True, exist_ok=True)
    if getattr(chunk, "point_cloud", None):
        source = Metashape.DataSource.PointCloudData
        log("[START] exportLAS  -- dense cloud")
    elif getattr(chunk, "tie_points", None):
        source = Metashape.DataSource.TiePointsData
        log("[START] exportLAS  -- tie points (chua co dense cloud)")
    else:
        log("-- Khong co point cloud / tie points de xuat LAS")
        return False
    try:
        chunk.exportPointCloud(
            path=str(dest),
            source_data=source,
            format=Metashape.PointCloudFormat.PointCloudFormatLAS,
            save_point_color=True,
        )
    except Exception as exc:
        log(f"-- exportLAS loi: {exc}")
        return False
    if dest.is_file() and dest.stat().st_size > 0:
        log(f"[DONE ] exportLAS  {dest} ({dest.stat().st_size} byte)")
        return True
    log("-- exportLAS khong tao file")
    return False


def export_dem(chunk, dest: Path) -> bool:
    meta = metashape_runtime()

    dest.parent.mkdir(parents=True, exist_ok=True)
    if not getattr(chunk, "elevation", None):
        log("-- Chua co DEM de xuat")
        return False
    log("[START] exportDEM  -- xuat dem.tif")
    try:
        chunk.exportRaster(
            path=str(dest),
            source_data=meta.DataSource.ElevationData,
            image_format=meta.ImageFormatTIFF,
        )
    except Exception as exc:
        log(f"-- exportDEM loi: {exc}")
        return False
    if dest.is_file() and dest.stat().st_size > 0:
        log(f"[DONE ] exportDEM  {dest} ({dest.stat().st_size} byte)")
        return True
    log("-- exportDEM khong tao file")
    return False


def build_and_export_ortho(chunk, dest: Path) -> bool:
    """Dung ortho bang enum Metashape (khong truyen chuoi surface_data)."""
    meta = metashape_runtime()

    dest.parent.mkdir(parents=True, exist_ok=True)
    surfaces: list[tuple[str, object]] = []
    if getattr(chunk, "elevation", None):
        surfaces.append(("DEM", meta.DataSource.ElevationData))
    if getattr(chunk, "model", None):
        surfaces.append(("mesh", meta.DataSource.ModelData))
    if not surfaces:
        log("-- Khong co DEM/mesh de dung orthomosaic")
        return False

    built = bool(getattr(chunk, "orthomosaic", None))
    if not built:
        for label, surface in surfaces:
            log(f"[START] buildOrtho  -- {label}")
            try:
                chunk.buildOrthomosaic(
                    surface_data=surface,
                    fill_holes=True,
                    blending_mode=meta.BlendingMode.MosaicBlending,
                    subdivide_task=True,
                )
                built = bool(getattr(chunk, "orthomosaic", None))
                if built:
                    log(f"[DONE ] buildOrtho  -- {label}")
                    break
                log(f"-- buildOrtho {label} khong tao orthomosaic")
            except Exception as exc:
                log(f"-- buildOrtho {label} loi: {exc}")
    if not getattr(chunk, "orthomosaic", None):
        log("-- Khong tao duoc orthomosaic")
        return False

    log("[START] exportOrtho  -- xuat orthomosaic.tif")
    try:
        chunk.exportRaster(
            path=str(dest),
            source_data=meta.DataSource.OrthomosaicData,
            image_format=meta.ImageFormatTIFF,
            split_in_blocks=False,
        )
    except Exception as exc:
        log(f"-- exportOrtho loi: {exc}")
        return False
    if dest.is_file() and dest.stat().st_size > 0:
        log(f"[DONE ] exportOrtho  {dest} ({dest.stat().st_size} byte)")
        return True
    log("-- exportOrtho khong tao file")
    return False


def write_result_summary(output: Path, extra: dict | None = None) -> None:
    written = existing_export_labels(output)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "exports": {name: str(output / name) for name in written},
    }
    if extra:
        payload.update(extra)
    (output / "twin_summary.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log("=== XONG ===")
    log(f"  Ket qua: {output}")
    if written:
        for name in written:
            log(f"  - {name}")
    else:
        log("  - (chua co file xuat)")
    missing = [
        name
        for name in (
            "point_cloud/point_cloud.las",
            "dem/dem.tif",
            "orthomosaic/orthomosaic.tif",
        )
        if name not in written
    ]
    if missing:
        log(f"  Thieu: {', '.join(missing)}")


def metashape_process_count() -> int:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq metashape.exe"],
            text=True,
            errors="replace",
        )
    except Exception:
        return 0
    return sum(1 for line in out.splitlines() if line.lower().startswith("metashape.exe"))


def clear_stale_project_lock(project_path: Path) -> None:
    lock = project_path.with_name(project_path.stem + ".files") / "lock"
    if not lock.is_file():
        return
    others = metashape_process_count()
    # Khi chay qua metashape.exe -r, chinh process nay cung la metashape.exe.
    # Lock con lai cua job treo truoc do van phai xoa neu khong con instance khac.
    if others > 1:
        return
    if others == 1 and not launched_by_metashape():
        return
    lock.unlink(missing_ok=True)
    log(f"-- Xoa lock mo coi: {lock}")


def project_locked_message(project_path: Path) -> str:
    return (
        f"Project dang bi khoa read-only: {project_path}\n"
        "Mot tien trinh Metashape khac (job cu) dang mo file nay.\n"
        "1) Task Manager: tat tat ca metashape.exe\n"
        "2) Chay lai cung lenh\n"
        "Hoac dung thu muc khac: --output digital-twin\\output-gpu"
    )


def ensure_project_writable(project, project_path: Path) -> None:
    doc = project.doc
    if doc is None:
        return
    if bool(getattr(doc, "read_only", False)):
        die(project_locked_message(project_path))
    try:
        project.save_project(version="writable-check")
    except OSError as exc:
        text = str(exc).lower()
        if "read-only" in text or "disabled" in text:
            die(project_locked_message(project_path))
        raise


def run_pipeline(args: argparse.Namespace, photos: list[PhotoRecord]) -> None:
    modules = import_uav_modules()
    meta = metashape_runtime()
    Project = modules["Project"]
    PhotoProcessor = modules["PhotoProcessor"]
    PointCloudProcessor = modules["PointCloudProcessor"]
    MeshProcessor = modules["MeshProcessor"]
    GeographicProjection = modules["GeographicProjection"]

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    init_logger(output)
    for sub in ("point_cloud", "model", "texture", "dem", "orthomosaic", "tiled"):
        (output / sub).mkdir(parents=True, exist_ok=True)

    preset = quality_params(args.quality)
    mesh_surface = resolve_mesh_surface(args)
    if getattr(args, "texture_size", None):
        preset["texture_size"] = int(args.texture_size)
    if getattr(args, "texture_pages", None):
        preset["texture_pages"] = max(1, min(16, int(args.texture_pages)))
    elif mesh_surface == "arbitrary":
        # Generic UV canh ContextCapture: nhieu atlas hon 1 trang nadir.
        preset["texture_pages"] = max(int(preset.get("texture_pages") or 1), 4)
    if getattr(args, "depth_quality", None):
        preset["depth_downscale"] = DEPTH_DOWNSCALE[args.depth_quality]
    preset["face_count_custom"] = parse_face_count(getattr(args, "face_count", None))
    rebuild_depth = bool(getattr(args, "rebuild_depth", False))
    rebuild_cloud = bool(getattr(args, "rebuild_cloud", False)) or rebuild_depth
    cloud_min_conf = getattr(args, "cloud_min_conf", None)
    if cloud_min_conf is not None:
        cloud_min_conf = max(0, int(cloud_min_conf))
    depth_label = next(
        (k for k, v in DEPTH_DOWNSCALE.items() if v == preset["depth_downscale"]),
        str(preset["depth_downscale"]),
    )
    faces_custom = preset["face_count_custom"]
    if faces_custom is None:
        faces_label = "High (preset)"
    elif faces_custom == 0:
        faces_label = "khong decimate"
    else:
        faces_label = f"{faces_custom:,}"
    project_path = output / "digital_twin.psx"
    uv_mode = (
        "Metashape.GenericMapping"
        if mesh_surface == "arbitrary"
        else "Metashape.AdaptiveOrthophotoMapping"
    )
    uv_label = "Generic" if mesh_surface == "arbitrary" else "Adaptive"

    log("=== UAV-digital-twin / Metashape ===")
    log(f"  Anh         : {len(photos)}")
    log(f"  Quality     : {args.quality}")
    log(f"  Texture     : {preset['texture_size']} x {preset.get('texture_pages', 1)} trang  UV={uv_label}")
    log(f"  Mesh        : {mesh_surface}  faces={faces_label}")
    log(f"  Depth       : {depth_label} (downscale {preset['depth_downscale']})"
        + ("  REBUILD" if rebuild_depth else ""))
    log(f"  Project     : {project_path}")
    log(f"  Log file    : {output / 'run.log'}")

    log("[START] Init Metashape compute")
    settings = CompatibleSettings(
        use_gpu=not args.cpu_only,
        gpu_mask=parse_gpu_mask(args.gpu_mask),
        list_gpu=args.list_gpu,
    )
    settings.set_log(str(output))
    log("[DONE ] Init Metashape compute")

    log("[START] Mo / tao project .psx")
    clear_stale_project_lock(project_path)
    project = Project(project_path=str(project_path), enable_monitoring=args.monitor)
    if project_path.is_file() and not args.reset:
        project.load_project()
        log(f"[DONE ] Load project: {project_path}")
    else:
        project.new_project()
        log(f"[DONE ] New project: {project_path}")
    ensure_project_writable(project, project_path)
    orig_save = project.save_project

    def safe_save_project(version: str, path: str | None = None) -> None:
        try:
            if path is None:
                orig_save(version=version)
            else:
                orig_save(version=version, path=path)
        except Exception as exc:
            log(f"-- save_project({version}) loi: {exc}")

    project.save_project = safe_save_project

    photo_proc = PhotoProcessor(photos_path=[p.path for p in photos])
    if not project.chunk.cameras:
        with_progress("addPhotos", f"them {len(photos)} anh", photo_proc.addPhotos)
    log(f"-- Cameras in chunk: {len(project.chunk.cameras)}")

    already_aligned = (
        aligned_count(project.chunk) >= 2 and chunk_is_georeferenced(project.chunk)
    )
    resume = already_aligned and not args.reset
    if args.resume and not already_aligned and not args.reset:
        log("-- --resume nhung project chua align (moi/bi xoa) → chay day du match/align.")
    if resume:
        log(
            f"-- RESUME: giu align {aligned_count(project.chunk)}/"
            f"{len(project.chunk.cameras)}, bo qua filter/match/align, reuse depth."
        )
    else:
        linked = apply_mrk_to_cameras(project.chunk, photos)
        log(f"-- Gan GPS MRK cho {linked} camera")
        project.save_project(version="importReference")

        if not args.skip_filter:
            with_progress("filterImageQuality", "loc anh mo", photo_proc.filterImageQuality)

        with_progress(
            "matchPhotos",
            "khop diem anh, thuong la buoc lau",
            lambda p: photo_proc.matchPhotos(
                progress_printer=p,
                downscale=preset["match_downscale"],
                keypoint_limit=40000,
                tiepoint_limit=10000,
                generic_preselection=True,
                reference_preselection=True,
                filter_stationary_points=True,
                keep_keypoints=True,
                guided_matching=False,
                subdivide_task=True,
            ),
        )
        with_progress(
            "alignCameras",
            "can chinh camera",
            lambda p: photo_proc.alignCameras(
                progress_printer=p,
                adaptive_fitting=False,
                reset_alignment=True,
                subdivide_task=True,
            ),
        )
        log(f"-- Anh align duoc: {aligned_count(project.chunk)} / {len(project.chunk.cameras)}")

        with_progress(
            "optimizeCameras",
            "toi uu tham so camera",
            lambda p: photo_proc.optimizeCameras(
                progress_printer=p,
                fit_f=True,
                fit_cx=True,
                fit_cy=True,
                fit_k1=True,
                fit_k2=True,
                fit_k3=True,
                fit_p1=True,
                fit_p2=True,
            ),
        )

    if not chunk_is_georeferenced(project.chunk):
        die("Chunk chua georeference sau align. Kiem tra GPS EXIF/MRK va chay lai --reset.")

    if getattr(args, "export_only", False):
        log("-- Chi xuat LAS/DEM/ortho tu project hien co (--export-only)")
        export_las(project.chunk, output / "point_cloud" / "point_cloud.las")
        if not args.skip_ortho:
            if not getattr(project.chunk, "elevation", None):
                geo = GeographicProjection()
                dem_source = (
                    "Metashape.PointCloudData"
                    if getattr(project.chunk, "point_cloud", None)
                    else "Metashape.TiePointsData"
                )
                try:
                    with_progress(
                        "buildDem",
                        "DEM",
                        lambda p: geo.buildDem(
                            progress_printer=p,
                            source_data=dem_source,
                            interpolation="Metashape.EnabledInterpolation",
                            subdivide_task=True,
                        ),
                    )
                except Exception as exc:
                    log(f"-- Bo qua buildDem: {exc}")
            export_dem(project.chunk, output / "dem" / "dem.tif")
            build_and_export_ortho(
                project.chunk, output / "orthomosaic" / "orthomosaic.tif"
            )
        write_result_summary(
            output,
            {
                "metashape": meta.app.version,
                "quality": args.quality,
                "cameras": len(project.chunk.cameras),
                "aligned": aligned_count(project.chunk),
                "project": str(project_path),
                "mode": "export-only",
            },
        )
        return

    cloud_proc = PointCloudProcessor()
    has_depth = chunk_has_depth_maps(project.chunk)
    if args.skip_depth:
        log("-- Bo qua estimating depth / buildDepthMaps (--skip-depth)")
    elif resume and has_depth and not rebuild_depth:
        log("-- RESUME: da co depth maps, bo qua buildDepthMaps")
    elif (
        resume
        and not rebuild_depth
        and not rebuild_cloud
        and not getattr(args, "rebuild_mesh", False)
        and chunk_has_model(project.chunk)
    ):
        log("-- RESUME: da co mesh, khong can depth maps (chi texture), bo qua buildDepthMaps")
    else:
        reuse_depth = resume and not rebuild_depth
        if rebuild_depth and has_depth:
            log(
                f"-- rebuild-depth: dung lai depth maps {depth_label} "
                "(giu align; dense cloud/LAS cu giu nguyen, mesh dung tu depth moi)"
            )
        with_progress(
            "buildDepthMaps",
            f"depth map {depth_label}, reuse_depth=" + ("on" if reuse_depth else "off"),
            lambda p: cloud_proc.buildDepthMaps(
                progress_printer=p,
                downscale=preset["depth_downscale"],
                filter_mode="Metashape.FilterMode.MildFiltering",
                reuse_depth=reuse_depth,
                subdivide_task=True,
            ),
        )
        has_depth = chunk_has_depth_maps(project.chunk)

    has_cloud = chunk_has_point_cloud(project.chunk)
    las_path = output / "point_cloud" / "point_cloud.las"
    if has_cloud and not rebuild_cloud:
        log("-- RESUME: da co dense cloud, bo qua buildPointCloud/colorize")
    elif has_depth:
        if has_cloud:
            conf_label = "mac dinh UAV (>= 2)" if cloud_min_conf is None else f">= {cloud_min_conf}"
            log(f"-- rebuild-cloud: dung lai dense cloud (giu confidence {conf_label})")
            remove_point_clouds(project.chunk, log_fn=log)
            if las_path.is_file():
                las_path.unlink()
        with_progress(
            "buildPointCloud",
            "dense cloud",
            lambda p: cloud_proc.buildPointCloud(
                progress_printer=p,
                source_data="Metashape.DataSource.DepthMapsData",
                point_colors=True,
                point_confidence=True,
                keep_depth=True,
                subdivide_task=True,
            ),
        )
        if cloud_min_conf is None:
            try:
                log("[START] filterPointCloud  -- mac dinh UAV-digital-twin (xoa confidence 0-1)")
                cloud_proc.filterPointCloud()
                log("[DONE ] filterPointCloud")
            except Exception as exc:
                log(f"-- Bo qua loc confidence: {exc}")
        elif cloud_min_conf > 0:
            try:
                log(f"[START] filterPointCloud  -- xoa confidence 0-{cloud_min_conf - 1}")
                cloud_proc.filterPointCloud(cloud_min_conf - 1)
                log("[DONE ] filterPointCloud")
            except Exception as exc:
                log(f"-- Bo qua loc confidence: {exc}")
        else:
            log("-- Giu toan bo diem dense cloud (--cloud-min-conf 0)")
        try:
            with_progress("colorizePointCloud", "to mau point cloud", cloud_proc.colorizePointCloud)
        except Exception as exc:
            log(f"-- Bo qua to mau point cloud: {exc}")
        has_cloud = chunk_has_point_cloud(project.chunk)
    else:
        log("-- Bo qua dense cloud (khong co depth map).")

    if export_exists(las_path):
        log(f"-- RESUME: da co {las_path} ({las_path.stat().st_size} byte), bo qua exportLAS")
    else:
        export_las(project.chunk, las_path)

    if not args.skip_mesh:
        mesh_proc = MeshProcessor()
        obj_path = output / "model" / "model.obj"
        tex_path = output / "texture" / "texture.jpg"
        tex_tif = output / "texture" / "texture.tif"
        model_tif = output / "model" / "model.tif"
        rebuild_mesh = bool(getattr(args, "rebuild_mesh", False)) or rebuild_depth
        rebuild_texture = bool(getattr(args, "rebuild_texture", False)) or rebuild_mesh
        have_model = chunk_has_model(project.chunk)
        if have_model and not rebuild_mesh:
            log("-- RESUME: da co mesh trong project, bo qua buildModel")
        else:
            if rebuild_mesh and have_model:
                log(
                    "-- rebuild-mesh: Arbitrary=DisabledInterpolation + closeHoles "
                    "(tranh meo noi suy; uu tien depth maps)"
                )
            surface_key = (
                "Metashape.Arbitrary"
                if mesh_surface == "arbitrary"
                else "Metashape.HeightField"
            )
            plans = mesh_build_plan(project.chunk, preset, mesh_surface)
            for label, mesh_source, mesh_kwargs in plans:
                n_faces = mesh_kwargs.get("face_count_custom")
                if n_faces == 0:
                    n_faces = "khong decimate"
                log(
                    f"-- Mesh {label}: interpolation={mesh_kwargs.get('interpolation')} "
                    f"faces={n_faces or mesh_kwargs.get('face_count')}"
                )
                try:
                    with_progress(
                        "buildModel",
                        f"mesh {mesh_surface} tu {label}",
                        lambda p, src=mesh_source, kw=mesh_kwargs: mesh_proc.buildModel(
                            progress_printer=p,
                            surface_type=surface_key,
                            source_data=src,
                            **kw,
                        ),
                    )
                except Exception as exc:
                    log(f"-- buildModel tu {label} loi: {exc}")
                    continue
                if chunk_has_model(project.chunk):
                    log(f"-- Mesh san sang (nguon: {label})")
                    if mesh_surface == "arbitrary":
                        close_model_holes(project.chunk, log_fn=log, level=50)
                    try:
                        project.save_project(version="buildModel")
                    except Exception as exc:
                        log(f"-- Luu mesh loi (van dung model trong RAM): {exc}")
                    break
            if not chunk_has_model(project.chunk):
                log("-- Khong co mesh, bo qua texture/export model.")
                mesh_proc = None

        if mesh_proc is not None and chunk_has_model(project.chunk):
            obj_missing = not export_exists(obj_path)
            tex_missing = not (
                export_exists(tex_path) or export_exists(tex_tif) or export_exists(model_tif)
            )
            if export_exists(obj_path) and project_path.is_file():
                if obj_path.stat().st_mtime + 1.0 < project_path.stat().st_mtime:
                    log("-- model.obj cu hon project, xuat lai UV/texture/OBJ")
                    obj_missing = True
                    tex_missing = True
            if not obj_missing and not tex_missing and not rebuild_texture:
                log("-- RESUME: da co model.obj va texture.jpg, bo qua UV/texture/export")
            else:
                tex_pages = int(preset.get("texture_pages") or 1)
                if rebuild_texture and not rebuild_mesh:
                    log(
                        f"-- rebuild-texture: UV {uv_label} {preset['texture_size']} x {tex_pages} trang"
                    )
                for name, hint, action in (
                    (
                        "buildUV",
                        f"UV {uv_label} mapping",
                        lambda p: mesh_proc.buildUV(
                            progress_printer=p,
                            mapping_mode=uv_mode,
                            page_count=tex_pages,
                            texture_size=preset["texture_size"],
                        ),
                    ),
                    (
                        "buildTexture",
                        "texture tu anh",
                        lambda p: mesh_proc.buildTexture(
                            progress_printer=p,
                            blending_mode="Metashape.MosaicBlending",
                            texture_size=preset["texture_size"],
                            fill_holes=True,
                            ghosting_filter=True,
                        ),
                    ),
                    (
                        "exportModel",
                        "xuat OBJ",
                        lambda p: mesh_proc.exportModel(progress_printer=p, path=str(output)),
                    ),
                    (
                        "exportTexture",
                        "xuat texture",
                        lambda p: mesh_proc.exportTexture(progress_printer=p, path=str(output)),
                    ),
                ):
                    if not rebuild_texture:
                        if name == "exportModel" and not obj_missing:
                            log("-- RESUME: da co model.obj, bo qua exportModel")
                            continue
                        if name == "exportTexture" and not tex_missing:
                            log("-- RESUME: da co texture.jpg, bo qua exportTexture")
                            continue
                    try:
                        with_progress(name, hint, action)
                    except Exception as exc:
                        log(f"-- Bo qua {name}: {exc}")

    if not args.skip_ortho:
        dem_path = output / "dem" / "dem.tif"
        ortho_path = output / "orthomosaic" / "orthomosaic.tif"
        have_dem = export_exists(dem_path)
        have_ortho = export_exists(ortho_path)
        if have_dem and have_ortho:
            log("-- RESUME: da co DEM va orthomosaic, bo qua build/export DEM/ortho")
        else:
            geo = GeographicProjection()
            dem_source = (
                "Metashape.PointCloudData"
                if chunk_has_point_cloud(project.chunk)
                else "Metashape.TiePointsData"
            )
            if have_dem:
                log(f"-- RESUME: da co {dem_path}, bo qua buildDem/exportDEM")
            else:
                try:
                    with_progress(
                        "buildDem",
                        "DEM",
                        lambda p: geo.buildDem(
                            progress_printer=p,
                            source_data=dem_source,
                            interpolation="Metashape.EnabledInterpolation",
                            subdivide_task=True,
                        ),
                    )
                except Exception as exc:
                    log(f"-- Bo qua buildDem: {exc}")
                export_dem(project.chunk, dem_path)
            if have_ortho:
                log(f"-- RESUME: da co {ortho_path}, bo qua build/export ortho")
            else:
                build_and_export_ortho(project.chunk, ortho_path)
            try:
                project.save_project(version="exportOrtho")
            except Exception:
                pass

    report_path = output / "report.pdf"
    if export_exists(report_path):
        log(f"-- RESUME: da co {report_path}, bo qua exportReport")
    else:
        try:
            log("[START] exportReport")
            project.chunk.exportReport(path=str(report_path), title="UAV Digital Twin")
            log(f"[DONE ] Report: {report_path}")
        except Exception as exc:
            log(f"-- Bo qua report.pdf: {exc}")

    write_result_summary(
        output,
        {
            "metashape": meta.app.version,
            "quality": args.quality,
            "surface_type": mesh_surface,
            "cameras": len(project.chunk.cameras),
            "aligned": aligned_count(project.chunk),
            "project": str(project_path),
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dựng digital twin từ LiDAR-image bằng UAV-digital-twin (Metashape)."
    )
    parser.add_argument(
        "--images",
        nargs="+",
        default=[str(DEFAULT_IMAGES)],
        help="Mot hoac nhieu thu muc anh DJI (nhan thu muc cha gom nhieu flight raw)",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Thư mục xuất")
    parser.add_argument("--quality", choices=("low", "medium", "high"), default="medium")
    parser.add_argument(
        "--texture-size",
        type=int,
        choices=(4096, 8192, 16384),
        default=None,
        help="Kich thuoc moi trang texture. Mac dinh: low 4096, medium 8192, high 16384.",
    )
    parser.add_argument(
        "--texture-pages",
        type=int,
        default=None,
        metavar="N",
        help="So trang atlas UV (1-16). Mac dinh: low 1, medium 2, high 4. Nhieu trang = net hon ContextCapture.",
    )
    parser.add_argument("--list-only", action="store_true", help="Chỉ liệt kê dữ liệu, không chạy Metashape")
    parser.add_argument(
        "--cpu-only",
        action="store_true",
        help="Tat GPU, chi dung CPU",
    )
    parser.add_argument(
        "--gpu-mask",
        default="10",
        help="Mask GPU nhi phan. Mac dinh 10 = chi AMD (device 1), tat Intel HD. Thu 1 neu AMD la device 0.",
    )
    parser.add_argument(
        "--list-gpu",
        action="store_true",
        help="Goi enumGPUDevices (co the treo tren Intel HD)",
    )
    parser.add_argument("--monitor", action="store_true", help="Ghi monitor.csv của UAV-digital-twin")
    parser.add_argument("--reset", action="store_true", help="Tạo project .psx mới, bỏ project cũ")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Chay tiep project da co: bo qua match/align/depth/cloud/DEM/ortho neu da xong, chi lam buoc con thieu",
    )
    parser.add_argument("--skip-filter", action="store_true", help="Không lọc ảnh mờ")
    parser.add_argument(
        "--skip-depth",
        action="store_true",
        help="Bo qua estimating depth map. Dung depth da co, hoac tie points neu chua co.",
    )
    parser.add_argument(
        "--depth-quality",
        choices=tuple(DEPTH_DOWNSCALE),
        default=None,
        help="Do phan giai depth map (quyet dinh do net goc/canh/tuong): "
        "ultra=1/1, high=1/2, medium=1/4, low=1/8. Mac dinh theo --quality.",
    )
    parser.add_argument(
        "--rebuild-depth",
        action="store_true",
        help="Dung lai depth maps theo --depth-quality tren project da align, roi dung lai mesh/texture.",
    )
    parser.add_argument(
        "--cloud-min-conf",
        type=int,
        default=None,
        metavar="N",
        help="Giu diem dense cloud co confidence >= N. Mac dinh: loc nhu UAV-digital-twin "
        "(xoa confidence 0-1, giu tuong/mat dung). 4 = loc manh (xoa 0-3); 0 = giu het.",
    )
    parser.add_argument(
        "--rebuild-cloud",
        action="store_true",
        help="Dung lai dense cloud + LAS tu depth maps (dung depth co san, thieu thi dung lai).",
    )
    parser.add_argument(
        "--face-count",
        default=None,
        metavar="high|max|N",
        help="So mat mesh 3D: high (preset), max (khong decimate, net canh nhat), hoac so mat N.",
    )
    parser.add_argument("--skip-mesh", action="store_true", help="Không dựng mesh/texture")
    parser.add_argument(
        "--rebuild-mesh",
        action="store_true",
        help="Dung lai mesh/texture/OBJ. Arbitrary: DisabledInterpolation + closeHoles (it meo).",
    )
    parser.add_argument(
        "--rebuild-texture",
        action="store_true",
        help="Giu mesh, chi dung lai UV + texture + xuat OBJ (de tang do net).",
    )
    parser.add_argument(
        "--surface-type",
        choices=("heightfield", "arbitrary"),
        default=None,
        help="Kieu mesh: heightfield (2.5D, mac dinh) hoac arbitrary (3D that).",
    )
    parser.add_argument(
        "--mesh-3d",
        action="store_true",
        help="Anh quet 3D / oblique: dung mesh Arbitrary + UV GenericMapping.",
    )
    parser.add_argument("--skip-ortho", action="store_true", help="Không dựng DEM/orthomosaic")
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="Chi xuat LAS/DEM/ortho tu .psx hien co, khong match/align/depth/mesh",
    )
    parser.add_argument("--license-check", action="store_true", help="Chỉ kiểm tra license Metashape rồi thoát")
    parser.add_argument("--no-relaunch", action="store_true", help="Không tự chạy lại qua metashape.exe")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    quiet_metashape_console()
    output = Path(args.output).resolve()
    init_logger(output)
    log(f"-- Log UTF-8: {output / 'run.log'}")

    if not args.list_only and not args.no_relaunch:
        maybe_relaunch_via_pro()

    requested = [Path(p).resolve() for p in args.images]
    images = collect_all_mission_dirs(requested)
    if images != requested:
        log(f"-- Tim thay {len(images)} thu muc mission raw")
        for folder in images:
            log(f"   {folder}")

    photos, mrk_paths = load_photos_from_missions(images)
    linked = sum(1 for p in photos if p.lat is not None)

    print_inventory(photos, mrk_paths, images)
    catalog = write_catalog(output, images, photos, mrk_paths)
    reference = write_reference_csv(output, photos)
    log(f"-- Catalog : {catalog}")
    if reference:
        log(f"-- GPS CSV : {reference} ({linked} anh)")

    if args.list_only:
        log("-- Dung o --list-only (khong goi Metashape).")
        return

    if args.license_check:
        meta = metashape_runtime()

        log(f"-- Metashape {meta.app.version}")
        log(f"-- activated={bool(meta.app.activated)} valid={bool(meta.license.valid)}")
        return

    try:
        run_pipeline(args, photos)
    except RuntimeError as exc:
        die(str(exc))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        log("-- Dung boi nguoi dung (Ctrl+C). Chay lai cung lenh + --resume de tiep tuc.")
        raise SystemExit(130)
    except BaseException:
        import traceback

        # metashape.exe -r khong in traceback ra terminal cha → ghi vao run.log + stdout.
        for line in traceback.format_exc().rstrip().splitlines():
            log(f"[LOI  ] {line}")
        sys.stdout.flush()
        raise SystemExit(1)
