#!/usr/bin/env python3
"""Tile LAS / preview may diem thanh Cesium 3D Tiles (*.pnts) trong output/tiled-las."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.argv = [str(HERE / "build-3dtiles.py"), *sys.argv[1:]]
if "--points" not in sys.argv:
    sys.argv.append("--points")
runpy.run_path(str(HERE / "build-3dtiles.py"), run_name="__main__")
