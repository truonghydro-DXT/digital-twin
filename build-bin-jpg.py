#!/usr/bin/env python3
"""Tao file xem truoc trong output/.preview (cung format viewer).

  .\\venv\\Scripts\\python.exe -u build-bin-jpg.py output-3d
  .\\venv\\Scripts\\python.exe -u build-bin-jpg.py output-3d --rebuild

Sinh:
  model-mesh-full.bin / -view.bin / -fast.bin / -medium.bin
  model-mesh-full.jpg
  model-mesh-full-view.bin.gz  model-mesh-full-fast.bin.gz
  points-las-full.bin / -view.bin / -fast.bin
  points-las-view.bin.gz  points-las-fast.bin.gz
"""

from __future__ import annotations

import argparse
import gzip
import importlib.util
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GZIP_TARGETS = (
    "model-mesh-full-fast.bin",
    "model-mesh-full-view.bin",
    "points-las-fast.bin",
    "points-las-view.bin",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def load_viewer():
    path = HERE / "view-output.py"
    if not path.is_file():
        raise FileNotFoundError(f"Khong thay {path}")
    spec = importlib.util.spec_from_file_location("view_output", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Khong nap duoc view-output.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["view_output"] = mod
    spec.loader.exec_module(mod)
    return mod


def unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def write_gzip(src: Path) -> Path | None:
    if not src.is_file() or src.stat().st_size < 64:
        return None
    dest = Path(str(src) + ".gz")
    if dest.is_file() and dest.stat().st_mtime >= src.stat().st_mtime and dest.stat().st_size > 32:
        if dest.stat().st_size < src.stat().st_size:
            log(f"-- RESUME: {dest.name} ({dest.stat().st_size:,} byte)")
            return dest
        unlink_quiet(dest)
        return None
    tmp = Path(str(dest) + ".tmp")
    log(f"[START] gzip {src.name}  ({src.stat().st_size:,} byte)")
    with src.open("rb") as handle, gzip.open(tmp, "wb", compresslevel=4) as out:
        shutil.copyfileobj(handle, out, length=8 * 1024 * 1024)
    tmp.replace(dest)
    if dest.stat().st_size >= src.stat().st_size:
        unlink_quiet(dest)
        log(f"-- Bo gzip {src.name}: file nen khong nho hon goc")
        return None
    log(f"[DONE ] {dest.name}  ({dest.stat().st_size:,} byte)")
    return dest


def clear_mesh_preview(vo, output: Path) -> None:
    preview = output / ".preview"
    names = (
        "model-mesh-full.bin",
        "model-mesh-full-view.bin",
        "model-mesh-full-fast.bin",
        "model-mesh-full-medium.bin",
        "model-mesh-full.jpg",
        "model-mesh-fast.jpg",
        "model-mesh-medium.jpg",
    )
    for name in names:
        unlink_quiet(preview / name)
        unlink_quiet(Path(str(preview / name) + ".gz"))


def clear_las_preview(output: Path) -> None:
    preview = output / ".preview"
    names = (
        "points-las-full.bin",
        "points-las-view.bin",
        "points-las-fast.bin",
        "points-las-medium.bin",
    )
    for name in names:
        unlink_quiet(preview / name)
        unlink_quiet(Path(str(preview / name) + ".gz"))


def build_mesh(vo, rebuild: bool) -> list[Path]:
    st = vo._obj_preview_fresh()
    obj_path = st["obj"]
    if not obj_path.is_file():
        log(f"-- Bo mesh: khong thay {obj_path}")
        return []
    if rebuild:
        log("[START] xoa mesh preview cu")
        clear_mesh_preview(vo, Path(vo.OUTPUT))
        st = vo._obj_preview_fresh()
    log(f"[START] mesh OBJ -> .preview  ({obj_path.stat().st_size:,} byte)")
    vo._convert_obj_to_preview(st)
    full = st["paths"]["full"]
    if not vo._obj_bin_magic_ok(full):
        st = vo._obj_preview_fresh()
        full = st["paths"]["full"]
    if vo._obj_bin_magic_ok(full):
        vo.ensure_obj_fast_lod(full)
        vo.ensure_obj_medium_lod(full)
        vo.ensure_obj_view_full(full)
    vo.ensure_obj_fast_texture()
    vo.ensure_obj_medium_texture()
    st = vo._obj_preview_fresh()
    made = []
    for key in ("full", "view", "fast", "medium", "jpg"):
        path = st["paths"][key]
        if path.is_file():
            log(f"[DONE ] {path.name}  ({path.stat().st_size:,} byte)")
            made.append(path)
    return made


def build_las(vo, rebuild: bool) -> list[Path]:
    las = Path(vo.OUTPUT) / "point_cloud" / "point_cloud.las"
    if not las.is_file():
        log(f"-- Bo LAS: khong thay {las}")
        return []
    if rebuild:
        log("[START] xoa LAS preview cu")
        clear_las_preview(Path(vo.OUTPUT))
    log(f"[START] LAS -> .preview  ({las.stat().st_size:,} byte)")
    made = [
        vo.build_las_fast_bin(las),
        vo.build_las_full_bin(las),
        vo.build_las_view_bin(las),
    ]
    for path in made:
        if path.is_file():
            log(f"[DONE ] {path.name}  ({path.stat().st_size:,} byte)")
    return [p for p in made if p.is_file()]


def build_gzip(preview: Path) -> list[Path]:
    made = []
    for name in GZIP_TARGETS:
        path = preview / name
        gz = write_gzip(path)
        if gz is not None:
            made.append(gz)
    return made


def run_preview(
    output: Path | str,
    rebuild: bool = False,
    skip_mesh: bool = False,
    skip_las: bool = False,
    skip_gzip: bool = False,
) -> Path:
    output = Path(output).expanduser().resolve()
    preview = output / ".preview"
    preview.mkdir(parents=True, exist_ok=True)
    vo = load_viewer()
    vo.OUTPUT = output
    log(f"-- Output {output}")
    log(f"-- Preview {preview}")
    if not skip_mesh:
        build_mesh(vo, rebuild)
    if not skip_las:
        build_las(vo, rebuild)
    if not skip_gzip:
        build_gzip(preview)
    log(f"[DONE ] preview {preview}")
    return preview


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build .preview *.bin *.bin.gz *.jpg")
    parser.add_argument("output", nargs="?", default=str(HERE / "output-3d"))
    parser.add_argument("--rebuild", action="store_true", help="Xoa preview cu roi build lai")
    parser.add_argument("--skip-mesh", action="store_true")
    parser.add_argument("--skip-las", action="store_true")
    parser.add_argument("--skip-gzip", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    preview = run_preview(
        args.output,
        rebuild=args.rebuild,
        skip_mesh=args.skip_mesh,
        skip_las=args.skip_las,
        skip_gzip=args.skip_gzip,
    )
    log(f"=== XONG === {preview}")


if __name__ == "__main__":
    main()
