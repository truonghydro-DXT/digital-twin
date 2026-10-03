#!/usr/bin/env python3
"""Web UI Digital Twin doc lap (khong can ung dung GIS goc).

   digital-twin\\venv\\Scripts\\python.exe digital-twin\\view-output.py
   hoac chay tu trong thu muc digital-twin:
   .\\venv\\Scripts\\python.exe view-output.py
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

HERE = Path(__file__).resolve().parent
LIB = HERE / "lib"
DEFAULT_IMAGES = HERE / "LiDAR-image"
DEFAULT_OUTPUT = HERE / "output-new"
PATHS_FILE = HERE / ".viewer-paths.json"
DEM3D_ROOT = LIB / "dem-3d"
GIS_STATIC = LIB / "static"
MAX_POINTS = 2_000_000
LAS_FAST_POINT_TARGET = 200_000
LAS_MED_POINT_TARGET = 2_000_000
MAX_RASTER = 1800
MAX_ORTHO_RASTER = 8192
MAX_OBJ_TEXTURE = 16384
MAX_OBJ_TEXTURE_FAST = 2048
MAX_OBJ_TEXTURE_MED = 4096
OBJ_FAST_FACE_TARGET = 160_000
OBJ_MED_FACE_TARGET = 1_200_000
OBJ_MESH_MAGIC = b"DTM2"
GZIP_BIN_MAX = 32 * 1024 * 1024
MAP_MAX_ZOOM = 32
TILES_MOD = None

PRODUCTS = [
    ("overview", "Dashboard", None),
    ("ortho", "Orthomosaic", "orthomosaic/orthomosaic.tif"),
    ("dem", "DEM", "dem/dem.tif"),
    ("dtwin", "Digital Twin", None),
    ("las", "May diem LAS", "point_cloud/point_cloud.las"),
    # ("obj", "Mesh OBJ", "model/model.obj"),
    ("meshtiles", "Mesh Tiles", "tiled/tileset.json"),
    # ("texture", "Texture", "texture/texture.jpg"),
    # ("cameras", "Camera GPS", "camera_reference.csv"),
    # ("catalog", "Catalog", "mission_catalog.json"),
    # ("log", "Log", "run.log"),
    ("report", "Report PDF", "report.pdf"),
]

app = Flask(
    __name__,
    static_folder=str(DEM3D_ROOT / "static"),
    static_url_path="/static",
)
OUTPUT = DEFAULT_OUTPUT
IMAGES = DEFAULT_IMAGES
DEM3D = None
TILE_CACHE_ROOT = HERE / ".dem3d-tiles"


PAGE = r"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Digital twin</title>
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate"/>
  <meta http-equiv="Pragma" content="no-cache"/>
  <link rel="stylesheet" href="/gis-static/vendors/leaflet/leaflet.css"/>
  <link rel="stylesheet" href="/gis-static/vendors/leaflet/draw/leaflet.draw.css"/>
  <link rel="stylesheet" href="/gis-static/vendors/cesium/Cesium/Widgets/widgets.css"/>
  <script>window.CESIUM_BASE_URL = "/gis-static/vendors/cesium/Cesium/";</script>
  <script src="/gis-static/vendors/plotly/plotly-2.35.2.min.js"></script>
  <script src="/gis-static/vendors/three/three.min.js"></script>
  <script src="/gis-static/vendors/three/OrbitControls.js"></script>
  <script src="/static/dem-3d.js?v=20260916lasstyle"></script>
  <script src="/gis-static/vendors/leaflet/leaflet.js"></script>
  <script src="/gis-static/vendors/leaflet/draw/leaflet.draw.js"></script>
  <script src="/gis-static/vendors/cesium/Cesium/Cesium.js"></script>
  <style>
    :root { --bg:#0f172a; --panel:#111827; --line:#1f2937; --text:#e5e7eb; --muted:#94a3b8; --ok:#34d399; --miss:#f87171; --acc:#38bdf8; }
    * { box-sizing: border-box; }
    html, body { margin:0; height:100%; background:var(--bg); color:var(--text); font: 14px/1.45 "Segoe UI", sans-serif; }
    .app { display:grid; grid-template-columns: 280px 1fr; height:100%; }
    aside { background:var(--panel); border-right:1px solid var(--line); padding:16px; overflow:hidden; display:flex; flex-direction:column; min-height:0; }
    h1 { font-size:16px; margin:0 0 14px; }
    #nav { flex:1 1 auto; overflow:auto; min-height:0; }
    #camPreview { display:none; margin-top:auto; padding-top:10px; border-top:1px solid var(--line); flex:0 0 auto; }
    #camPreview.visible { display:block; }
    #camPreviewMeta { color:var(--muted); font-size:11px; margin-bottom:8px; word-break:break-all; white-space:pre-wrap; }
    #camPreviewFrame { position:relative; min-height:148px; border-radius:8px; background:#0b1220; overflow:hidden; }
    #camPreviewImg { width:100%; max-height:220px; object-fit:contain; display:block; background:#0b1220; }
    #camPreviewEmpty { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; padding:12px; text-align:center; color:var(--muted); font-size:12px; }
    #camPreviewFrame.has-img #camPreviewEmpty { display:none; }
    #pathBox { margin:0; display:flex; flex-direction:column; gap:8px; flex:0 0 auto; }
    #pathBox > div > span, #pathBox label { display:block; color:var(--muted); font-size:11px; }
    #pathBox input {
      display:block; width:100%; margin-top:4px; border:1px solid var(--line);
      border-radius:8px; padding:7px 8px; background:#0b1220; color:var(--text);
      font:12px/1.35 Consolas,monospace;
    }
    #pathBox input.missing { border-color:#b45309; }
    .pathRow { display:flex; gap:6px; margin-top:4px; align-items:stretch; }
    .pathRow input { flex:1; min-width:0; margin-top:0 !important; }
    .pathRow button {
      flex:0 0 auto; border:0; border-radius:8px; padding:7px 8px; cursor:pointer;
      background:#334155; color:#e5e7eb; font-size:11px; font-weight:600;
      display:inline-flex; align-items:center;
    }
    .pathRow button:hover { background:#475569; color:#fff; }
    #pathApply {
      border:0; border-radius:8px; padding:7px 8px; cursor:pointer;
      background:#1e293b; color:#e5e7eb; font-weight:600;
    }
    #pathApply:hover { background:#334155; color:#fff; }
    #pathHint { color:var(--muted); font-size:11px; margin:6px 0 10px; min-height:0; }
    #pathHint:empty { display:none; }
    #runBar { display:flex; gap:8px; margin:8px 0 12px; flex:0 0 auto; flex-wrap:wrap; }
    #runBar button {
      flex:1; border:0; border-radius:8px; padding:8px 6px; cursor:pointer;
      background:#1e293b; color:#e5e7eb; font-weight:600;
    }
    #runBar button:hover { background:#334155; color:#fff; }
    #runBar button:disabled { opacity:.45; cursor:wait; }
    #dtwComputeMode {
      flex:0 0 auto; border:1px solid var(--line); border-radius:8px;
      padding:8px 6px; background:#0b1220; color:var(--text); font-weight:600;
    }
    #dtwMesh3dLabel {
      flex:0 0 100%; display:flex; align-items:center; gap:6px; margin:0;
      color:#e5e7eb; font:12px/1.3 "Segoe UI",sans-serif; cursor:pointer; user-select:none;
    }
    #dtwMesh3d, #dtwTex16k { width:14px; height:14px; accent-color:#38bdf8; }
    #dtwTex16kLabel {
      flex:0 0 100%; display:flex; align-items:center; gap:6px; margin:0;
      color:#e5e7eb; font:12px/1.3 "Segoe UI",sans-serif; cursor:pointer; user-select:none;
    }
    #dtwOptions {
      flex:0 0 100%; display:grid; grid-template-columns:auto 1fr; gap:4px 8px; align-items:center;
      color:#e5e7eb; font:12px/1.3 "Segoe UI",sans-serif;
    }
    #dtwOptions select {
      border:1px solid var(--line); border-radius:6px; padding:4px 6px;
      background:#0b1220; color:var(--text); font:12px/1.3 "Segoe UI",sans-serif;
    }
    #runModal {
      position:fixed; inset:0; z-index:50; background:rgba(2,6,23,.68);
      display:flex; align-items:center; justify-content:center; padding:18px;
    }
    #runModal[hidden], #fsModal[hidden] { display:none !important; }
    #fsModal {
      position:fixed; inset:0; z-index:52; background:rgba(2,6,23,.68);
      display:flex; align-items:center; justify-content:center; padding:18px;
    }
    .fsBox {
      width:min(640px,94vw); max-height:80vh; background:#111827; color:#e5e7eb;
      border:1px solid #1f2937; border-radius:12px; padding:14px;
      display:flex; flex-direction:column; min-height:0;
    }
    .fsHead { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:8px; }
    .fsHead button { border:0; border-radius:8px; padding:6px 10px; background:#1e293b; color:#e5e7eb; cursor:pointer; }
    #fsPath { color:var(--muted); font:12px Consolas,monospace; word-break:break-all; margin-bottom:8px; }
    #fsList { flex:1; overflow:auto; min-height:220px; border:1px solid #1f2937; border-radius:8px; background:#0b1220; }
    #fsList button {
      display:block; width:100%; text-align:left; border:0; background:transparent;
      color:#e5e7eb; padding:8px 10px; cursor:pointer; font:13px/1.35 "Segoe UI",sans-serif;
    }
    #fsList button:hover { background:#1e293b; }
    .fsCreate { display:flex; gap:8px; margin-top:10px; }
    .fsCreate input {
      flex:1; min-width:0; border:1px solid #1f2937; border-radius:8px;
      padding:7px 8px; background:#0b1220; color:#e5e7eb; font:13px/1.35 "Segoe UI",sans-serif;
    }
    .fsActions { display:flex; gap:8px; margin-top:8px; }
    .fsActions button, .fsCreate button { border:0; border-radius:8px; padding:7px 10px; cursor:pointer; background:#1e293b; color:#e5e7eb; font-weight:600; }
    .fsActions button.ok { background:#028afa; color:#fff; }
    #fsMsg { color:#fbbf24; font-size:12px; margin-top:6px; min-height:0; }
    #fsMsg:empty { display:none; }
    .runModalBox {
      width:min(760px,94vw); max-height:82vh; background:#111827; color:#e5e7eb;
      border:1px solid #1f2937; border-radius:12px; padding:16px 16px 14px;
      display:flex; flex-direction:column; min-height:0; box-shadow:0 16px 48px rgba(0,0,0,.45);
    }
    .runModalHead { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:8px; }
    .runModalHead b { color:var(--acc); }
    #runModalClose { border:0; border-radius:8px; padding:6px 10px; background:#1e293b; color:#e5e7eb; cursor:pointer; }
    #runModalStatus { color:var(--muted); font-size:13px; margin-bottom:8px; }
    #runModalTrack { height:10px; background:#0b1220; border-radius:999px; overflow:hidden; margin-bottom:10px; }
    #runModalBar { height:100%; width:0%; background:linear-gradient(90deg,#38bdf8,#34d399); transition:width .15s linear; }
    #runModalLog {
      margin:0; flex:1; min-height:220px; max-height:48vh; overflow:auto;
      background:#0b1220; border:1px solid #1f2937; border-radius:8px; padding:10px;
      white-space:pre-wrap; font:12px/1.45 Consolas,monospace;
    }
    #folder, aside .path { display:none !important; }
    .item { display:flex; justify-content:space-between; gap:8px; padding:8px 10px; border-radius:8px; cursor:pointer; color:var(--text); text-decoration:none; }
    .item:hover, .item.active { background:#1e293b; }
    .ok { color:var(--ok); } .miss { color:var(--miss); }
    main { display:flex; flex-direction:column; min-width:0; }
    header { padding:12px 18px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center; gap:12px; }
    header b { color:var(--acc); }
    .headerRight { display:flex; align-items:center; gap:10px; min-width:0; }
    #dtwOpenShp[hidden], #btnDrawRegion[hidden], #btnOptimizeData[hidden], #btnSaveMesh[hidden] { display:none !important; }
    #stage { flex:1; min-height:0; padding:12px 18px 18px; }
    #plot { width:100%; height:100%; min-height:520px; background:#0b1220; border:1px solid var(--line); border-radius:12px; }
    #demHost, #orthoHost { position:relative; width:100%; height:100%; min-height:560px; background:#0b1a2b; border:1px solid var(--line); border-radius:12px; overflow:hidden; }
    #orthoMap { position:absolute; inset:0; }
    #cesiumContainer { position:absolute; inset:0; }
    #cesiumContainer .cesium-viewer, #cesiumContainer .cesium-widget,
    #cesiumContainer .cesium-widget canvas { width:100%; height:100%; }
    #view3d { position:absolute; inset:0; }
    #view3d canvas { display:block; }
    #dem3dControls { position:absolute; z-index:5; right:12px; bottom:12px; min-width:240px; padding:10px 12px; background:#028afa; color:#fff; border-radius:8px; font-size:12px; }
    #dem3dControls input[type=range] { width:100%; }
    #lasPointControls {
      position:absolute; z-index:8; right:12px; bottom:12px; min-width:260px;
      padding:10px 12px; background:#028afa; color:#fff; border-radius:8px; font-size:12px;
      pointer-events:auto; display:flex; flex-direction:column; gap:8px;
    }
    #lasPointControls .lasCtlRow { display:flex; align-items:center; gap:8px; }
    #lasPointControls .lasCtlRow > span:first-child { flex:0 0 78px; }
    #lasPointControls input[type=range] { flex:1; min-width:0; }
    #lasPointControls select {
      flex:1; min-width:0; border:0; border-radius:6px; padding:4px 6px;
      background:#0b1220; color:#fff;
    }
    #lasPointControls input[type=color] {
      width:36px; height:26px; padding:0; border:0; background:transparent; cursor:pointer;
    }
    #lasPointSizeVal { flex:0 0 28px; text-align:right; font-variant-numeric:tabular-nums; }
    #demHost #lasPointControls { bottom:86px; }
    #dem3dLegend { position:absolute; z-index:5; left:12px; bottom:12px; padding:10px 12px; background:rgba(12,28,48,.88); border-radius:8px; font-size:12px; }
    .dem3d-legend-body { display:flex; gap:8px; }
    .dem3d-legend-bar { width:18px; height:140px; border-radius:3px; background:linear-gradient(to top,#0d33d9 0%,#0dbfd9 20%,#26d940 40%,#f2e626 60%,#f27314 80%,#e61a1a 100%); }
    .dem3d-legend-labels { display:flex; flex-direction:column; justify-content:space-between; font-family:Consolas,monospace; }
    img.view { max-width:100%; max-height:calc(100vh - 120px); display:block; margin:0 auto; border-radius:8px; }
    pre { margin:0; height:100%; overflow:auto; background:#0b1220; border:1px solid var(--line); border-radius:12px; padding:14px; white-space:pre-wrap; }
    .empty { color:var(--muted); padding:40px; text-align:center; }
    #lasProgress { position:absolute; z-index:20; left:50%; top:50%; transform:translate(-50%,-50%); width:min(440px,88%); padding:18px 20px; background:rgba(12,28,48,.94); border:1px solid #1f2937; border-radius:12px; box-shadow:0 12px 40px rgba(0,0,0,.35); pointer-events:none; }
    #lasProgressLabel { font-size:14px; margin-bottom:4px; }
    #lasProgressPct { color:var(--acc); font-variant-numeric:tabular-nums; font-weight:600; }
    #lasProgressTrack { height:12px; background:#0b1220; border-radius:999px; overflow:hidden; margin:12px 0 8px; }
    #lasProgressBar { height:100%; width:0%; background:linear-gradient(90deg,#38bdf8,#34d399); transition:width .1s linear; }
    #lasProgressMeta { color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
    #drawHost { position:absolute; inset:0; z-index:6; pointer-events:none; }
    #drawHost.drawing, #drawHost.editing { pointer-events:auto; cursor:crosshair; }
    #drawMap { position:absolute; inset:0; background:transparent !important; }
    #drawMap.leaflet-container, #orthoMap.leaflet-container { background:transparent; }
    #orthoMap .leaflet-tile, #orthoMap .ortho-xyz { background:transparent !important; }
    #drawHost .leaflet-control-container { pointer-events:none; }
    #drawToolbar {
      position:absolute; left:10px; top:10px; transform:none;
      z-index:10; pointer-events:auto; display:flex; flex-direction:column; gap:8px;
    }
    #drawToolbar .leaflet-draw-toolbar { margin-top:0; box-shadow:0 2px 8px rgba(0,0,0,.35); }
    #drawToolbar .leaflet-bar a {
      width:34px; height:34px; line-height:34px;
      background-color:#fff !important;
      background-image:url("/gis-static/vendors/leaflet/draw/images/spritesheet.svg");
      background-repeat:no-repeat; background-size:300px 30px;
      border-bottom:1px solid #ccc;
    }
    #drawToolbar .leaflet-draw-draw-polyline { background-position:-2px -2px; }
    #drawToolbar .leaflet-draw-draw-polygon { background-position:-31px -2px; }
    #drawToolbar .leaflet-draw-draw-rectangle { background-position:-62px -2px; }
    #drawToolbar .leaflet-draw-draw-circle { background-position:-92px -2px; }
    #drawToolbar .leaflet-draw-draw-marker { background-position:-122px -2px; }
    #drawToolbar .leaflet-draw-edit-edit { background-position:-152px -2px; }
    #drawToolbar .leaflet-draw-edit-remove { background-position:-182px -2px; }
    #drawToolbar .leaflet-bar a:hover { background-color:#f4f4f4 !important; }
    #drawToolbar .leaflet-bar a.active { background-color:#dbeafe !important; }
    #drawToolbarDone {
      display:none; border:0; border-radius:6px; padding:6px 8px;
      background:#028afa; color:#fff; font:12px/1.2 "Segoe UI",sans-serif; cursor:pointer;
    }
    #drawToolbar.busy #drawToolbarDone { display:block; }
    #drawHost .leaflet-control-attribution, #drawHost .leaflet-control-zoom { display:none; }
    #drawHost .leaflet-overlay-pane, #drawHost .leaflet-marker-pane { pointer-events:none; }
    #drawHost.drawing .leaflet-overlay-pane, #drawHost.drawing .leaflet-marker-pane,
    #drawHost.editing .leaflet-overlay-pane, #drawHost.editing .leaflet-marker-pane { pointer-events:auto; }
    #dtwOpenShp {
      border:1px solid rgba(56,189,248,.55); border-radius:8px; padding:8px 12px;
      background:#028afa; color:#fff;
      font:600 13px "Segoe UI",sans-serif; cursor:pointer;
      box-shadow:0 8px 24px rgba(0,0,0,.35); white-space:nowrap;
    }
    #dtwOpenShp:hover { background:#38bdf8; color:#0f172a; }
    #dtwOpenShp:disabled { opacity:.55; cursor:wait; }
    #btnDrawRegion, #btnOptimizeData, #btnSaveMesh {
      border:1px solid rgba(56,189,248,.55); border-radius:8px; padding:8px 12px;
      color:#fff; font:600 13px "Segoe UI",sans-serif; cursor:pointer;
      box-shadow:0 8px 24px rgba(0,0,0,.35); white-space:nowrap;
    }
    #btnDrawRegion { background:#028afa; }
    #btnDrawRegion:hover { background:#38bdf8; color:#0f172a; }
    #btnOptimizeData { background:#059669; border-color:rgba(52,211,153,.55); }
    #btnOptimizeData:hover { background:#34d399; color:#0f172a; }
    #btnSaveMesh { background:#b45309; border-color:rgba(251,191,36,.55); }
    #btnSaveMesh:hover { background:#f59e0b; color:#0f172a; }
    #btnDrawRegion:disabled, #btnOptimizeData:disabled, #btnSaveMesh:disabled { opacity:.55; cursor:wait; }
    #regionDrawHost { position:absolute; inset:0; z-index:8; background:#0b1a2b; }
    #regionDrawHost[hidden] { display:none !important; }
    #regionDrawMap { position:absolute; inset:0; }
    #orthoHost .leaflet-control-layers {
      border:1px solid #1f2937; border-radius:8px;
      background:rgba(15,23,42,.94); color:#e5e7eb;
      box-shadow:0 8px 24px rgba(0,0,0,.35);
    }
    #orthoHost .leaflet-control-layers-expanded {
      min-width:260px; max-width:min(360px,70vw); padding:10px 10px 8px;
    }
    #orthoHost .leaflet-control-layers-overlays { max-height:42vh; overflow:auto; }
    #orthoHost .leaflet-control-layers label {
      display:flex; align-items:center; gap:6px;
      margin:0 0 6px; color:#e5e7eb; font:13px/1.3 "Segoe UI",sans-serif;
    }
    #orthoHost .leaflet-control-layers label > span { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; }
    #orthoHost .leaflet-control-layers-separator { border-top-color:#334155; }
    .dtw-layer-btn {
      flex:0 0 auto; width:22px; height:22px; padding:0; line-height:20px;
      border-radius:5px; background:#fff; cursor:pointer; text-align:center;
    }
    .dtw-layer-locate-btn { border:1px solid rgba(29,78,216,.4); color:#1d4ed8; }
    .dtw-layer-locate-btn:hover { background:#eaf2ff; border-color:rgba(37,99,235,.75); }
    .dtw-layer-fields-btn { border:1px solid rgba(22,101,52,.4); color:#166534; }
    .dtw-layer-fields-btn:hover, .dtw-layer-fields-btn.is-active {
      background:#ecfdf3; border-color:rgba(22,163,74,.75);
    }
    .dtw-layer-remove-btn { border:1px solid rgba(185,28,28,.4); color:#b91c1c; }
    .dtw-layer-remove-btn:hover { background:#fef2f2; border-color:rgba(185,28,28,.75); }
    .dtw-shp-filter-pop {
      position:fixed; z-index:5000; width:min(320px,92vw); max-height:70vh;
      display:flex; flex-direction:column; gap:8px;
      padding:10px; border-radius:10px;
      background:#111827; color:#e5e7eb; border:1px solid #334155;
      box-shadow:0 16px 40px rgba(0,0,0,.45);
    }
    .dtw-shp-filter-pop b { color:#38bdf8; font-size:13px; }
    .dtw-shp-filter-values { overflow:auto; max-height:36vh; display:flex; flex-direction:column; gap:4px; }
    .dtw-shp-filter-option {
      display:flex; align-items:center; gap:6px; margin:0;
      color:#e5e7eb; font:12px/1.3 "Segoe UI",sans-serif;
    }
    .dtw-shp-filter-option span { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; }
    .dtw-shp-filter-empty { color:#94a3b8; font-size:12px; }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <h1>Digital twin</h1>
      <div id="pathBox">
        <div>
          <span>Input image directory (scans subfolders)</span>
          <div class="pathRow">
            <input id="imagesPath" type="text" spellcheck="false" autocomplete="off" placeholder="Parent folder, e.g. D:\\WORK\\digital-twin\\data\\danphuong3D"/>
            <button id="browseImages" type="button">Server</button>
          </div>
        </div>
        <div>
          <span>Output Directory</span>
          <div class="pathRow">
            <input id="outputPath" type="text" spellcheck="false" autocomplete="off" placeholder="Folder on the server or \\\\server\\share\\out"/>
            <button id="browseOutput" type="button">Server</button>
          </div>
        </div>
        <button id="pathApply" type="button">Apply</button>
      </div>
      <div id="runBar">
        <select id="dtwComputeMode" aria-label="DTW compute mode" title="DTW compute mode">
          <option value="gpu" selected>GPU</option>
          <option value="cpu">CPU</option>
        </select>
        <button id="btnRunDtw" type="button">Run DTW</button>
        <button id="btnRunTiles" type="button">Run Tiles</button>
        <label id="dtwMesh3dLabel" title="Mesh Arbitrary + UV GenericMapping (anh 3D / oblique)">
          <input id="dtwMesh3d" type="checkbox" checked />
          Mesh 3D (Arbitrary)
        </label>
        <label id="dtwTex16kLabel" title="Moi trang atlas 16384; Mesh 3D dung 4 trang Generic UV (gan ContextCapture, cham hon)">
          <input id="dtwTex16k" type="checkbox" checked />
          Texture 16384
        </label>
        <div id="dtwOptions">
          <label for="dtwQuality" title="--quality: match/align + preset chung">Quality</label>
          <select id="dtwQuality">
            <option value="low">low</option>
            <option value="medium" selected>medium</option>
            <option value="high">high</option>
          </select>
          <label for="dtwDepthQuality" title="--depth-quality: do phan giai depth map (net goc/canh/tuong)">Depth</label>
          <select id="dtwDepthQuality">
            <option value="">theo Quality</option>
            <option value="ultra">ultra (1/1)</option>
            <option value="high" selected>high (1/2)</option>
            <option value="medium">medium (1/4)</option>
            <option value="low">low (1/8)</option>
          </select>
          <label for="dtwCloudMinConf" title="--cloud-min-conf: giu diem dense cloud co confidence >= N">Cloud conf</label>
          <select id="dtwCloudMinConf">
            <option value="">mac dinh (xoa 0-1)</option>
            <option value="0">0 - giu het</option>
            <option value="2" selected>2 - xoa 0-1</option>
            <option value="3">3 - xoa 0-2</option>
            <option value="4">4 - loc manh (xoa 0-3)</option>
          </select>
          <label for="dtwFaceCount" title="--face-count: so mat mesh 3D">Faces</label>
          <select id="dtwFaceCount">
            <option value="">mac dinh</option>
            <option value="high">high (preset)</option>
            <option value="max" selected>max (khong decimate)</option>
          </select>
        </div>
      </div>
      <div id="pathHint"></div>
      <nav id="nav"></nav>
      <div id="camPreview">
        <div id="camPreviewMeta">Click mot diem tren duong bay de xem anh UAV</div>
        <div id="camPreviewFrame">
          <div id="camPreviewEmpty">Click diem tren duong bay</div>
          <img id="camPreviewImg" alt="Anh UAV"/>
        </div>
      </div>
    </aside>
    <main>
      <header>
        <div id="title">Dashboard</div>
        <div class="headerRight">
          <button id="btnDrawRegion" type="button" hidden title="Ve polygon tren ban do">Vẽ vùng</button>
          <button id="btnOptimizeData" type="button" hidden title="Cat bo phan nam ngoai vung da ve">Tối ưu dữ liệu</button>
          <button id="btnSaveMesh" type="button" hidden title="Luu mesh da cat ra model/model.obj">Lưu dữ liệu</button>
          <button id="dtwOpenShp" type="button" hidden title="Mở shapefile (*.shp.zip)">Mở *.shp</button>
          <div id="meta"></div>
        </div>
      </header>
      <input id="dtwShpInput" type="file" accept=".zip,.shp,.shx,.dbf,.prj,application/zip" hidden multiple/>
      <div id="stage"><div class="empty">Dang tai...</div></div>
    </main>
  </div>
  <div id="fsModal" hidden>
    <div class="fsBox">
      <div class="fsHead">
        <b id="fsTitle">Server folder</b>
        <button id="fsClose" type="button">Close</button>
      </div>
      <div id="fsPath"></div>
      <div id="fsList"></div>
      <div class="fsCreate">
        <input id="fsNewName" type="text" spellcheck="false" autocomplete="off" placeholder="New folder name"/>
        <button id="fsMkdir" type="button">Create folder</button>
      </div>
      <div id="fsMsg"></div>
      <div class="fsActions">
        <button id="fsUp" type="button">Up</button>
        <button id="fsPick" type="button" class="ok">Select this folder</button>
      </div>
    </div>
  </div>
  <div id="runModal" hidden>
    <div class="runModalBox">
      <div class="runModalHead">
        <b id="runModalTitle">Tien trinh</b>
        <button id="runModalClose" type="button">Dong</button>
      </div>
      <div id="runModalStatus">San sang</div>
      <div id="runModalTrack"><div id="runModalBar"></div></div>
      <pre id="runModalLog"></pre>
    </div>
  </div>
<script>
const PRODUCTS = __PRODUCTS__;
const MAP_MAX_ZOOM = 32;
let current = "overview";
let orthoMap = null;
let cesiumViewer = null;
let lasPointTileset = null;
let lasPointStyle = { size: 4, colorMode: "rgb", color: "#38bdf8" };
let drawMap = null;
let drawItems = null;
let drawControl = null;
let drawGeojson = null;
let drawHandler = null;
let drawOwnsMap = false;
let drawSyncTimer = null;
let importedShpLayers = [];
let activeShpFieldsPopover = null;
let activeShpFieldsButton = null;

function destroyOrthoMap() {
  closeShpFieldsPopover();
  importedShpLayers = [];
  if (orthoMap) {
    try { orthoMap.remove(); } catch (e) {}
    orthoMap = null;
  }
}

function snapshotDrawings() {
  try {
    if (drawItems) drawGeojson = drawItems.toGeoJSON();
  } catch (e) {}
}

function syncDigitalTwinDrawings() {
  window.drawnItems = drawItems || null;
  const mapRef = drawMap || orthoMap;
  const imported = importedShpLayers.filter(function (entry) {
    return entry && entry.layer && mapRef && mapRef.hasLayer(entry.layer);
  }).map(function (entry) {
    return {
      name: entry.name,
      features: (entry && entry.features) || [],
      layer: entry.layer
    };
  });
  if (drawItems && typeof drawItems.toGeoJSON === "function") {
    try {
      const gj = drawItems.toGeoJSON();
      if (gj && gj.features && gj.features.length) {
        imported.push({ name: "drawings", features: gj.features, layer: drawItems });
      }
    } catch (e) {}
  }
  window._importedFileLayers = imported;
  if (typeof window.refreshDigitalTwinViewerData === "function") {
    window.refreshDigitalTwinViewerData();
  }
}

function bindDigitalTwinMap(map, host) {
  window.map = map || null;
  window.drawnItems = drawItems || null;
  syncDigitalTwinDrawings();
}

function unbindDigitalTwin() {
  if (typeof disableDigitalTwinOverlay === "function") {
    try { disableDigitalTwinOverlay(); } catch (e) {}
  }
  closeShpFieldsPopover();
  importedShpLayers = [];
  window.map = null;
  window.drawnItems = null;
  window._importedFileLayers = [];
}

const DTW_OVERLAY_EVENT = "population3d:overlay-data";
const DTW_OVERLAY_LAYER_NAME = "digital-twin-current-data";
const DTW_LAYER_COLORS = ["#1d4ed8", "#0f766e", "#b45309", "#7c3aed", "#be123c", "#15803d"];
const DTW_INTERVAL_COUNT = 5;
const DTW_SURFACE_LIFT = 18;
const DTW_SOURCE_MIN_HEIGHT = 420;
const DTW_SOURCE_MAX_HEIGHT = 4600;
const DTW_HEIGHT_SCALE = { exponent: 1.55, highTierThreshold: 0.76, highTierBoost: 1.2 };
const DTW_COLOR_STOPS = [
  { stop: 0, color: "#1d4ed8" },
  { stop: 0.35, color: "#0ea5e9" },
  { stop: 0.6, color: "#10b981" },
  { stop: 0.82, color: "#f59e0b" },
  { stop: 1, color: "#dc2626" }
];
let digitalTwinScriptPromise = null;
let digitalTwinRebuildPromise = null;

function getDigitalTwinApi() {
  if (!window.population3DSimulator) return null;
  if (typeof window.population3DSimulator.enable !== "function") return null;
  if (typeof window.population3DSimulator.disable !== "function") return null;
  if (typeof window.population3DSimulator.isEnabled !== "function") return null;
  return window.population3DSimulator;
}

function disableDigitalTwinOverlay() {
  const api = getDigitalTwinApi();
  if (api && api.isEnabled()) {
    try { api.disable(); } catch (e) {}
  }
  window.dispatchEvent(new CustomEvent(DTW_OVERLAY_EVENT, {
    detail: { layerName: DTW_OVERLAY_LAYER_NAME, removed: true }
  }));
}

function hashDigitalTwinLayerName(value) {
  const text = String(value || "");
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) {
    hash = ((hash << 5) - hash) + text.charCodeAt(i);
    hash |= 0;
  }
  return Math.abs(hash);
}

function colorForDigitalTwinLayer(layerName) {
  if (!layerName) return DTW_LAYER_COLORS[0];
  return DTW_LAYER_COLORS[hashDigitalTwinLayerName(layerName) % DTW_LAYER_COLORS.length];
}

function clampDigitalTwinValue(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function easeDigitalTwinHeight(value) {
  const normalized = clampDigitalTwinValue(value, 0, 1);
  const scaled = Math.pow(normalized, DTW_HEIGHT_SCALE.exponent);
  if (normalized < DTW_HEIGHT_SCALE.highTierThreshold) return scaled;
  return clampDigitalTwinValue(scaled * DTW_HEIGHT_SCALE.highTierBoost, 0, 1);
}

function hexToRgbDigitalTwin(hex) {
  const normalized = String(hex || "").replace("#", "");
  const fullHex = normalized.length === 3
    ? normalized.split("").map(function (ch) { return ch + ch; }).join("")
    : normalized;
  return {
    r: parseInt(fullHex.slice(0, 2), 16),
    g: parseInt(fullHex.slice(2, 4), 16),
    b: parseInt(fullHex.slice(4, 6), 16)
  };
}

function mixDigitalTwinColor(startHex, endHex, ratio) {
  const start = hexToRgbDigitalTwin(startHex);
  const end = hexToRgbDigitalTwin(endHex);
  const t = clampDigitalTwinValue(ratio, 0, 1);
  return "rgb(" +
    Math.round(start.r + (end.r - start.r) * t) + "," +
    Math.round(start.g + (end.g - start.g) * t) + "," +
    Math.round(start.b + (end.b - start.b) * t) + ")";
}

function getDigitalTwinGradientColor(normalizedValue) {
  const value = clampDigitalTwinValue(normalizedValue, 0, 1);
  for (let i = 0; i < DTW_COLOR_STOPS.length - 1; i += 1) {
    const current = DTW_COLOR_STOPS[i];
    const next = DTW_COLOR_STOPS[i + 1];
    if (value >= current.stop && value <= next.stop) {
      return mixDigitalTwinColor(current.color, next.color, (value - current.stop) / (next.stop - current.stop || 1));
    }
  }
  return DTW_COLOR_STOPS[DTW_COLOR_STOPS.length - 1].color;
}

function parseDigitalTwinNumber(value) {
  if (typeof value === "number") return value;
  if (value == null || value === "") return NaN;
  const text = String(value).trim().replace(/\s/g, "").replace(",", ".");
  return Number(text);
}

function numericDigitalTwinValueFromProps(props, preferredFields) {
  const data = props || {};
  const fields = Array.isArray(preferredFields) ? preferredFields : [];
  if (fields.length) {
    for (let i = 0; i < fields.length; i += 1) {
      const fieldName = String(fields[i] || "").trim();
      if (!fieldName) continue;
      const selectedValue = parseDigitalTwinNumber(data[fieldName]);
      if (Number.isFinite(selectedValue) && selectedValue > 0) {
        if (/(floors?|levels?|storeys?)/i.test(fieldName) && selectedValue <= 120) {
          return { fieldName: fieldName, rawValue: selectedValue, displayValue: Math.max(3, selectedValue * 3.2), usesPreferredField: true };
        }
        return { fieldName: fieldName, rawValue: selectedValue, displayValue: selectedValue, usesPreferredField: true };
      }
    }
    return null;
  }
  const candidates = [
    ["height", data.height], ["building_height", data.building_height],
    ["extrusion_height", data.extrusion_height], ["h", data.h],
    ["floors", data.floors], ["levels", data.levels], ["storeys", data.storeys]
  ];
  for (let i = 0; i < candidates.length; i += 1) {
    const fieldName = candidates[i][0];
    const rawValue = parseDigitalTwinNumber(candidates[i][1]);
    if (!Number.isFinite(rawValue) || rawValue <= 0) continue;
    if (/(floors?|levels?|storeys?)/i.test(fieldName) && rawValue <= 120) {
      return { fieldName: fieldName, rawValue: rawValue, displayValue: Math.max(3, rawValue * 3.2), usesPreferredField: false };
    }
    return { fieldName: fieldName, rawValue: rawValue, displayValue: rawValue, usesPreferredField: false };
  }
  return null;
}

function buildDigitalTwinIntervalClassifier(features, preferredFields) {
  const fields = Array.isArray(preferredFields) ? preferredFields.filter(Boolean) : [];
  if (!fields.length) return null;
  const values = [];
  (Array.isArray(features) ? features : []).forEach(function (feature) {
    const resolved = numericDigitalTwinValueFromProps(feature && feature.properties, fields);
    if (!resolved || !resolved.usesPreferredField) return;
    if (Number.isFinite(resolved.rawValue) && resolved.rawValue > 0) values.push(resolved.rawValue);
  });
  if (!values.length) return null;
  const min = Math.min.apply(null, values);
  const max = Math.max.apply(null, values);
  const spread = max - min;
  const step = max > min ? (max - min) / DTW_INTERVAL_COUNT : 0;
  return function (resolvedValue) {
    if (!resolvedValue || !resolvedValue.usesPreferredField) return null;
    const rawValue = Number(resolvedValue.rawValue);
    if (!Number.isFinite(rawValue) || rawValue <= 0) return null;
    const normalizedValue = spread === 0 ? 0.65 : (rawValue - min) / spread;
    let bucketIndex = step > 0
      ? Math.min(DTW_INTERVAL_COUNT - 1, Math.max(0, Math.floor((rawValue - min) / step)))
      : DTW_INTERVAL_COUNT - 1;
    return {
      bucketIndex: bucketIndex,
      bucketCount: DTW_INTERVAL_COUNT,
      bucketStart: step > 0 ? (min + bucketIndex * step) : min,
      bucketEnd: step > 0 ? (bucketIndex === DTW_INTERVAL_COUNT - 1 ? max : (min + (bucketIndex + 1) * step)) : max,
      normalizedValue: normalizedValue,
      displayHeight: Math.round(DTW_SOURCE_MIN_HEIGHT + easeDigitalTwinHeight(normalizedValue) * (DTW_SOURCE_MAX_HEIGHT - DTW_SOURCE_MIN_HEIGHT)),
      displayColor: getDigitalTwinGradientColor(normalizedValue)
    };
  };
}

function featureFromDrawLayer(layer) {
  if (!layer) return null;
  if (layer.feature && layer.feature.geometry) return layer.feature;
  if (typeof layer.toGeoJSON === "function") {
    try { return layer.toGeoJSON(); } catch (e) { return null; }
  }
  return null;
}

function buildDigitalTwinOverlayFeatureCollection() {
  const features = [];
  const seen = new Set();
  const appendFeature = function (feature, layerName, preferredFields, intervalClassifier, allowDefault) {
    if (!feature || !feature.geometry) return;
    const geomType = feature.geometry.type;
    if (geomType !== "Polygon" && geomType !== "MultiPolygon") return;
    if (seen.has(feature)) return;
    const props = Object.assign({}, feature.properties || {});
    let resolvedValue = numericDigitalTwinValueFromProps(props, preferredFields);
    if (!resolvedValue || !(resolvedValue.displayValue > 0)) {
      if (!allowDefault) return;
      resolvedValue = { fieldName: "height", rawValue: 12, displayValue: 12, usesPreferredField: false };
    }
    const bucketMeta = intervalClassifier ? intervalClassifier(resolvedValue) : null;
    const height = bucketMeta && bucketMeta.displayHeight > 0 ? bucketMeta.displayHeight : resolvedValue.displayValue;
    const color = bucketMeta && bucketMeta.displayColor
      ? bucketMeta.displayColor
      : (props.color || props.fill || props.fillColor || props.extrusion_color || colorForDigitalTwinLayer(layerName));
    features.push({
      type: "Feature",
      geometry: feature.geometry,
      properties: Object.assign({}, props, {
        surface_base_height: DTW_SURFACE_LIFT,
        surface_top_height: DTW_SURFACE_LIFT + height,
        extrusion_height: DTW_SURFACE_LIFT + height,
        extrusion_color: color,
        extrusion_label: props.name || props.ten || props.id || layerName || "DT object",
        dtw_value_field: resolvedValue.fieldName || "",
        dtw_value_raw: resolvedValue.rawValue,
        dtw_interval_index: bucketMeta ? bucketMeta.bucketIndex + 1 : null,
        dtw_interval_count: bucketMeta ? bucketMeta.bucketCount : null,
        dtw_interval_start: bucketMeta ? bucketMeta.bucketStart : null,
        dtw_interval_end: bucketMeta ? bucketMeta.bucketEnd : null,
        extrusion_rank: bucketMeta ? bucketMeta.normalizedValue : null
      })
    });
    seen.add(feature);
  };

  const imported = Array.isArray(window._importedFileLayers) ? window._importedFileLayers : [];
  imported.forEach(function (entry) {
    const layerName = entry && entry.name ? entry.name : "imported";
    const preferredFields = entry && entry.layer && Array.isArray(entry.layer._visibleLabelFields)
      ? entry.layer._visibleLabelFields.slice() : [];
    const allFeatures = entry && entry.layer && Array.isArray(entry.layer._importSourceFeatures)
      ? entry.layer._importSourceFeatures
      : ((entry && entry.features) || []);
    const sourceFeatures = Array.isArray(entry && entry.features) && entry.features.length
      ? entry.features
      : allFeatures;
    const intervalClassifier = buildDigitalTwinIntervalClassifier(allFeatures, preferredFields);
    sourceFeatures.forEach(function (feature) {
      appendFeature(feature, layerName, preferredFields, intervalClassifier, false);
    });
  });

  if (window.drawnItems && typeof window.drawnItems.eachLayer === "function") {
    window.drawnItems.eachLayer(function (layer) {
      const preferredFields = Array.isArray(layer && layer._visibleLabelFields) ? layer._visibleLabelFields.slice() : [];
      appendFeature(featureFromDrawLayer(layer), "drawn", preferredFields, null, true);
    });
  }

  return { type: "FeatureCollection", features: features };
}

window.getDigitalTwinOverlayFeatureCollection = buildDigitalTwinOverlayFeatureCollection;
window.refreshDigitalTwinViewerData = function () {
  buildDigitalTwinOverlayFeatureCollection();
  const api = getDigitalTwinApi();
  if (!api || !api.isEnabled()) return Promise.resolve();
  if (digitalTwinRebuildPromise) return digitalTwinRebuildPromise;
  digitalTwinRebuildPromise = Promise.resolve().then(async function () {
    try { api.disable(); } catch (e) {}
    window.dispatchEvent(new CustomEvent(DTW_OVERLAY_EVENT, {
      detail: { layerName: DTW_OVERLAY_LAYER_NAME, removed: true }
    }));
    await api.enable();
  }).catch(function (err) {
    console.warn("refreshDigitalTwinViewerData:", err);
  }).finally(function () {
    digitalTwinRebuildPromise = null;
  });
  return digitalTwinRebuildPromise;
};

function ensureDigitalTwinScript() {
  if (getDigitalTwinApi()) return Promise.resolve(window.population3DSimulator);
  if (digitalTwinScriptPromise) return digitalTwinScriptPromise;
  digitalTwinScriptPromise = new Promise(function (resolve, reject) {
    if (!document.getElementById("dtw-maplibre-css")) {
      const css = document.createElement("link");
      css.id = "dtw-maplibre-css";
      css.rel = "stylesheet";
      css.href = "/gis-static/vendors/maplibre-gl/maplibre-gl.css";
      document.head.appendChild(css);
    }
    const loadSimulator = function () {
      const script = document.createElement("script");
      script.src = "/api/digital-twin/view3d-simulator.js?v=20260903f";
      script.async = true;
      script.onload = function () {
        const api = getDigitalTwinApi();
        if (api) { resolve(api); return; }
        digitalTwinScriptPromise = null;
        reject(new Error("Digital Twin simulator loaded but population3DSimulator is unavailable."));
      };
      script.onerror = function () {
        digitalTwinScriptPromise = null;
        reject(new Error("Could not load Digital Twin simulator."));
      };
      document.head.appendChild(script);
    };
    if (typeof window.maplibregl !== "undefined") {
      loadSimulator();
      return;
    }
    const maplibreScript = document.createElement("script");
    maplibreScript.id = "dtw-maplibre-js";
    maplibreScript.src = "/gis-static/vendors/maplibre-gl/maplibre-gl.js";
    maplibreScript.async = true;
    maplibreScript.onload = loadSimulator;
    maplibreScript.onerror = function () {
      digitalTwinScriptPromise = null;
      reject(new Error("Could not load maplibre-gl for Digital Twin."));
    };
    document.head.appendChild(maplibreScript);
  });
  return digitalTwinScriptPromise;
}

async function enableDigitalTwinView() {
  const api = await ensureDigitalTwinScript();
  if (api.isEnabled()) {
    await window.refreshDigitalTwinViewerData();
    return api;
  }
  await api.enable();
  return api;
}

function destroyDrawOverlay() {
  snapshotDrawings();
  stopDrawHandler();
  if (drawSyncTimer) {
    clearTimeout(drawSyncTimer);
    drawSyncTimer = null;
  }
  if (drawControl && drawMap) {
    try { drawMap.removeControl(drawControl); } catch (e) {}
  }
  drawControl = null;
  drawItems = null;
  if (drawOwnsMap && drawMap) {
    try { drawMap.remove(); } catch (e) {}
  }
  drawMap = null;
  drawOwnsMap = false;
  const host = document.getElementById("drawHost");
  if (host) host.remove();
  const bar = document.getElementById("drawToolbar");
  if (bar) bar.remove();
}

function boundsForDraw() {
  if (window.Dem3D && Dem3D.getViewWgs84Bounds) {
    const v = Dem3D.getViewWgs84Bounds();
    if (v && Number.isFinite(v.west) && Number.isFinite(v.south) && Number.isFinite(v.east) && Number.isFinite(v.north)) {
      return [[v.south, v.west], [v.north, v.east]];
    }
  }
  return null;
}

function syncDrawMapTo3d() {
  if (!drawOwnsMap || !drawMap) return;
  const host = document.getElementById("drawHost");
  if (host && (host.classList.contains("drawing") || host.classList.contains("editing"))) return;
  const b = boundsForDraw();
  if (!b) return;
  try { drawMap.fitBounds(b, { animate: false, padding: [0, 0] }); } catch (e) {}
}

function bindDrawSync(host) {
  const canvas = host && host.querySelector("canvas");
  if (!canvas) return;
  const kick = function () {
    if (drawSyncTimer) clearTimeout(drawSyncTimer);
    drawSyncTimer = setTimeout(syncDrawMapTo3d, 90);
  };
  canvas.addEventListener("mouseup", kick);
  canvas.addEventListener("touchend", kick);
  canvas.addEventListener("wheel", kick, { passive: true });
}

function stopDrawHandler() {
  if (drawHandler) {
    try {
      if (typeof drawHandler.save === "function") drawHandler.save();
    } catch (e) {}
    try { drawHandler.disable(); } catch (e) {}
    drawHandler = null;
  }
  const bar = document.getElementById("drawToolbar");
  if (bar) {
    bar.classList.remove("busy");
    bar.querySelectorAll("a.active").forEach(function (el) { el.classList.remove("active"); });
  }
  ["drawHost", "orthoHost", "regionDrawHost"].forEach(function (id) {
    const host = document.getElementById(id);
    if (host) {
      host.classList.remove("drawing");
      host.classList.remove("editing");
    }
  });
}

function drawShapeOptions() {
  return {
    polyline: { shapeOptions: { color: "#38bdf8", weight: 3 } },
    polygon: { allowIntersection: false, showArea: true, shapeOptions: { color: "#38bdf8", weight: 3, fillOpacity: 0.2 } },
    rectangle: { shapeOptions: { color: "#38bdf8", weight: 3, fillOpacity: 0.15 } },
    circle: { shapeOptions: { color: "#fbbf24", weight: 3, fillOpacity: 0.15 } },
    marker: {}
  };
}

function startDrawTool(id, map, hostEl) {
  if (!map || typeof L === "undefined" || !L.Draw) return;
  const same = drawHandler && drawHandler._toolId === id;
  stopDrawHandler();
  if (same) return;
  const opts = drawShapeOptions();
  let handler = null;
  if (id === "polyline") handler = new L.Draw.Polyline(map, opts.polyline);
  else if (id === "polygon") handler = new L.Draw.Polygon(map, opts.polygon);
  else if (id === "rectangle") handler = new L.Draw.Rectangle(map, opts.rectangle);
  else if (id === "circle") handler = new L.Draw.Circle(map, opts.circle);
  else if (id === "marker") handler = new L.Draw.Marker(map, opts.marker);
  else if (id === "edit" && L.EditToolbar && L.EditToolbar.Edit) {
    handler = new L.EditToolbar.Edit(map, {
      featureGroup: drawItems,
      selectedPathOptions: { dashArray: "10, 10", fill: true, fillColor: "#fe57a1", fillOpacity: 0.1 }
    });
  } else if (id === "remove" && L.EditToolbar && L.EditToolbar.Delete) {
    handler = new L.EditToolbar.Delete(map, { featureGroup: drawItems });
  }
  if (!handler) return;
  handler._toolId = id;
  drawHandler = handler;
  handler.enable();
  if (hostEl) hostEl.classList.add((id === "edit" || id === "remove") ? "editing" : "drawing");
  const bar = document.getElementById("drawToolbar");
  if (bar) {
    bar.classList.add("busy");
    const btn = bar.querySelector('[data-tool="' + id + '"]');
    if (btn) btn.classList.add("active");
  }
}

function mountDrawToolbar(host, map) {
  let bar = document.getElementById("drawToolbar");
  if (bar) bar.remove();
  bar = document.createElement("div");
  bar.id = "drawToolbar";
  bar.innerHTML =
    '<div class="leaflet-draw leaflet-control">' +
      '<div class="leaflet-draw-section"><div class="leaflet-draw-toolbar leaflet-bar leaflet-draw-toolbar-top">' +
        '<a href="#" data-tool="polyline" class="leaflet-draw-draw-polyline" title="Polyline"></a>' +
        '<a href="#" data-tool="polygon" class="leaflet-draw-draw-polygon" title="Polygon"></a>' +
        '<a href="#" data-tool="rectangle" class="leaflet-draw-draw-rectangle" title="Rectangle"></a>' +
        '<a href="#" data-tool="circle" class="leaflet-draw-draw-circle" title="Circle"></a>' +
        '<a href="#" data-tool="marker" class="leaflet-draw-draw-marker" title="Marker"></a>' +
      '</div></div>' +
      '<div class="leaflet-draw-section"><div class="leaflet-draw-toolbar leaflet-bar">' +
        '<a href="#" data-tool="edit" class="leaflet-draw-edit-edit" title="Sua"></a>' +
        '<a href="#" data-tool="remove" class="leaflet-draw-edit-remove" title="Xoa"></a>' +
      '</div></div>' +
    '</div>' +
    '<button id="drawToolbarDone" type="button">Xong</button>';
  host.appendChild(bar);
  bar.addEventListener("click", function (ev) {
    const btn = ev.target.closest("[data-tool]");
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    startDrawTool(btn.getAttribute("data-tool"), map, document.getElementById("drawHost") || host);
  });
  const done = document.getElementById("drawToolbarDone");
  if (done) done.addEventListener("click", function (ev) {
    ev.preventDefault();
    stopDrawHandler();
    snapshotDrawings();
  });
}

function addDrawControl(map, hostEl) {
  if (typeof L === "undefined" || !L.Draw) return;
  if (L.Icon && L.Icon.Default) {
    L.Icon.Default.imagePath = "/gis-static/vendors/leaflet/images/";
  }
  drawMap = map;
  drawItems = new L.FeatureGroup();
  map.addLayer(drawItems);
  if (drawGeojson && drawGeojson.features && drawGeojson.features.length) {
    L.geoJSON(drawGeojson, {
      style: { color: "#38bdf8", weight: 3, fillColor: "#38bdf8", fillOpacity: 0.18 },
      pointToLayer: function (_f, latlng) {
        return L.circleMarker(latlng, { radius: 6, color: "#38bdf8", fillColor: "#38bdf8", fillOpacity: 0.95, weight: 2 });
      }
    }).eachLayer(function (layer) { drawItems.addLayer(layer); });
  }
  map.on(L.Draw.Event ? L.Draw.Event.CREATED : "draw:created", function (ev) {
    if (ev.layer) drawItems.addLayer(ev.layer);
    snapshotDrawings();
    syncDigitalTwinDrawings();
    stopDrawHandler();
  });
  map.on("draw:edited", function () { snapshotDrawings(); syncDigitalTwinDrawings(); });
  map.on("draw:deleted", function () { snapshotDrawings(); syncDigitalTwinDrawings(); });
  mountDrawToolbar(hostEl && hostEl.id === "drawHost" ? hostEl.parentElement || hostEl : hostEl, map);
}

function mountDrawOverlay(host) {
  destroyDrawOverlay();
  if (!host || typeof L === "undefined" || !L.Draw) return;
  const wrap = document.createElement("div");
  wrap.id = "drawHost";
  const mapDiv = document.createElement("div");
  mapDiv.id = "drawMap";
  wrap.appendChild(mapDiv);
  host.appendChild(wrap);
  const map = L.map(mapDiv, {
    zoomControl: false,
    attributionControl: false,
    maxZoom: MAP_MAX_ZOOM,
    zoomSnap: 0,
    zoomDelta: 0.25,
    boxZoom: false,
    keyboard: false
  });
  drawOwnsMap = true;
  addDrawControl(map, wrap);
  const b = boundsForDraw();
  if (b) map.fitBounds(b, { animate: false });
  setTimeout(function () {
    try { map.invalidateSize(); } catch (e) {}
    syncDrawMapTo3d();
  }, 80);
  bindDrawSync(host);
  bindDigitalTwinMap(map, host.parentElement || host);
}

function attachDrawToLeafletMap(map, hostEl) {
  destroyDrawOverlay();
  if (!map || typeof L === "undefined" || !L.Draw) return;
  drawOwnsMap = false;
  addDrawControl(map, hostEl);
  bindDigitalTwinMap(map, hostEl);
}

function shpLayerStyle() {
  return { color: "#f59e0b", weight: 2, fillColor: "#f59e0b", fillOpacity: 0.22 };
}

function setShpOpenVisible(visible) {
  const btn = document.getElementById("dtwOpenShp");
  if (btn) btn.hidden = !visible;
}

function openShpFilePicker() {
  const input = document.getElementById("dtwShpInput");
  if (!input) return;
  input.value = "";
  input.click();
}

function shpOpenButtons() {
  return Array.prototype.slice.call(document.querySelectorAll("#dtwOpenShp"));
}

function shpFileBaseName(name) {
  const base = String(name || "shapefile").split(/[/\\]/).pop();
  return (base || "shapefile").trim() || "shapefile";
}

function uniqueShpLayerName(name) {
  const base = shpFileBaseName(name);
  let label = base;
  let n = 2;
  while (importedShpLayers.some(function (entry) { return entry.name === label; })) {
    label = base + " (" + n + ")";
    n += 1;
  }
  return label;
}

function fieldNamesFromShpFeatures(features) {
  const names = [];
  (features || []).forEach(function (feature) {
    Object.keys((feature && feature.properties) || {}).forEach(function (key) {
      if (names.indexOf(key) < 0) names.push(key);
    });
  });
  return names;
}

function closeShpFieldsPopover() {
  if (activeShpFieldsPopover && activeShpFieldsPopover.parentNode) {
    activeShpFieldsPopover.parentNode.removeChild(activeShpFieldsPopover);
  }
  if (activeShpFieldsButton) activeShpFieldsButton.classList.remove("is-active");
  activeShpFieldsPopover = null;
  activeShpFieldsButton = null;
}

function applyShpExtrusionFields(layer, fields) {
  layer._visibleLabelFields = Array.isArray(fields) ? fields.slice() : [];
  if (layer._layerFieldsButton) {
    layer._layerFieldsButton.classList.toggle("is-active", layer._visibleLabelFields.length > 0);
  }
  syncDigitalTwinDrawings();
}

function openShpFieldsPopover(layer, anchorBtn) {
  closeShpFieldsPopover();
  const entry = importedShpLayers.find(function (item) { return item.layer === layer; });
  const features = (entry && entry.features) || layer._importSourceFeatures || [];
  const fields = (entry && entry.fields) || fieldNamesFromShpFeatures(features);
  const pop = document.createElement("div");
  pop.className = "dtw-shp-filter-pop";
  const title = document.createElement("b");
  title.textContent = "Dựng khối 3D theo trường";
  pop.appendChild(title);
  if (!fields.length) {
    const empty = document.createElement("div");
    empty.className = "dtw-shp-filter-empty";
    empty.textContent = "Không có trường dữ liệu để dựng khối 3D.";
    pop.appendChild(empty);
  } else {
    const selected = new Set(Array.isArray(layer._visibleLabelFields) ? layer._visibleLabelFields : []);
    const list = document.createElement("div");
    list.className = "dtw-shp-filter-values";
    fields.forEach(function (field) {
      const row = document.createElement("label");
      row.className = "dtw-shp-filter-option";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = selected.has(field);
      const text = document.createElement("span");
      text.textContent = field;
      text.title = field;
      row.appendChild(input);
      row.appendChild(text);
      list.appendChild(row);
      input.dataset.fieldName = field;
      input.addEventListener("change", function () {
        const next = [];
        list.querySelectorAll("input[type=checkbox]").forEach(function (cb) {
          if (cb.checked && cb.dataset.fieldName) next.push(cb.dataset.fieldName);
        });
        applyShpExtrusionFields(layer, next);
      });
    });
    pop.appendChild(list);
  }
  document.body.appendChild(pop);
  positionShpFilterPopover(pop, anchorBtn);
  activeShpFieldsPopover = pop;
  activeShpFieldsButton = anchorBtn;
  anchorBtn.classList.add("is-active");
  setTimeout(function () {
    function onDocClick(ev) {
      if (!activeShpFieldsPopover) return;
      if (activeShpFieldsPopover.contains(ev.target) || ev.target === anchorBtn || anchorBtn.contains(ev.target)) return;
      document.removeEventListener("mousedown", onDocClick, true);
      closeShpFieldsPopover();
    }
    document.addEventListener("mousedown", onDocClick, true);
  }, 0);
}

function positionShpFilterPopover(pop, anchor) {
  const rect = anchor.getBoundingClientRect();
  const width = Math.min(320, window.innerWidth - 16);
  let left = rect.right - width;
  let top = rect.bottom + 8;
  if (left < 8) left = 8;
  if (left + width > window.innerWidth - 8) left = window.innerWidth - width - 8;
  if (top + 280 > window.innerHeight) top = Math.max(8, rect.top - 288);
  pop.style.left = left + "px";
  pop.style.top = top + "px";
}

function layerFromShpControlInput(ctrl, input) {
  if (!ctrl || !input || !ctrl._layers) return null;
  const rawId = input.layerId != null ? input.layerId : input.getAttribute("layerId");
  if (rawId == null) return null;
  const items = Array.isArray(ctrl._layers) ? ctrl._layers : Object.keys(ctrl._layers).map(function (key) { return ctrl._layers[key]; });
  for (let i = 0; i < items.length; i++) {
    const obj = items[i];
    if (!obj || !obj.overlay || !obj.layer) continue;
    const objId = obj.layerId != null ? obj.layerId : L.stamp(obj.layer);
    if (String(objId) === String(rawId) || String(obj.layer._leaflet_id) === String(rawId)) return obj.layer;
  }
  return null;
}

function fitMapToShpLayer(layer) {
  const map = drawMap || orthoMap;
  if (!map || !layer) return;
  let bounds = null;
  try {
    if (typeof layer.getBounds === "function") bounds = layer.getBounds();
  } catch (e) {}
  if (bounds && bounds.isValid && bounds.isValid()) {
    map.fitBounds(bounds, { padding: [28, 28], maxZoom: 19 });
  }
}

function decorateShpLayerLabel(label, layer) {
  if (!label || !layer || label.querySelector(".dtw-layer-remove-btn")) return;
  const locBtn = document.createElement("button");
  locBtn.type = "button";
  locBtn.className = "dtw-layer-btn dtw-layer-locate-btn";
  locBtn.title = "Vị trí";
  locBtn.innerHTML = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="#1d4ed8" stroke-width="1.4"><path d="M8 1.8c-2.5 0-4.5 2-4.5 4.5 0 3.3 4.5 7.9 4.5 7.9s4.5-4.6 4.5-7.9c0-2.5-2-4.5-4.5-4.5Z"/><circle cx="8" cy="6.3" r="1.6"/></svg>';
  L.DomEvent.on(locBtn, "click", function (ev) {
    L.DomEvent.stopPropagation(ev);
    L.DomEvent.preventDefault(ev);
    fitMapToShpLayer(layer);
  });
  const fieldsBtn = document.createElement("button");
  fieldsBtn.type = "button";
  fieldsBtn.className = "dtw-layer-btn dtw-layer-fields-btn";
  fieldsBtn.title = "Dựng khối 3D theo trường";
  fieldsBtn.innerHTML = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="#166534" stroke-width="1.3"><path d="M1.2 8c1.6-2.6 4-4 6.8-4s5.2 1.4 6.8 4c-1.6 2.6-4 4-6.8 4S2.8 10.6 1.2 8Z"/><circle cx="8" cy="8" r="2.1"/></svg>';
  layer._layerFieldsButton = fieldsBtn;
  if (layer._visibleLabelFields && layer._visibleLabelFields.length) fieldsBtn.classList.add("is-active");
  L.DomEvent.on(fieldsBtn, "click", function (ev) {
    L.DomEvent.stopPropagation(ev);
    L.DomEvent.preventDefault(ev);
    if (activeShpFieldsPopover && activeShpFieldsButton === fieldsBtn) {
      closeShpFieldsPopover();
      return;
    }
    openShpFieldsPopover(layer, fieldsBtn);
  });
  const removeBtn = document.createElement("button");
  removeBtn.type = "button";
  removeBtn.className = "dtw-layer-btn dtw-layer-remove-btn";
  removeBtn.title = "Xóa layer khỏi bản đồ";
  removeBtn.innerHTML = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="#b91c1c" stroke-width="1.45"><path d="M4.2 4.2 11.8 11.8M11.8 4.2 4.2 11.8" stroke-linecap="round"/></svg>';
  L.DomEvent.on(removeBtn, "click", function (ev) {
    L.DomEvent.stopPropagation(ev);
    L.DomEvent.preventDefault(ev);
    removeImportedShpLayer(layer);
  });
  label.appendChild(locBtn);
  label.appendChild(fieldsBtn);
  label.appendChild(removeBtn);
}

function decorateShpLayerControl(ctrl) {
  if (!ctrl || typeof ctrl.getContainer !== "function") return;
  const container = ctrl.getContainer();
  if (!container) return;
  container.querySelectorAll(".leaflet-control-layers-selector").forEach(function (input) {
    if (!input || input.type === "radio" || input._dtwDecorated) return;
    const layer = layerFromShpControlInput(ctrl, input);
    const label = input.closest("label");
    if (!layer || !label) return;
    decorateShpLayerLabel(label, layer);
    input._dtwDecorated = true;
  });
}

function ensureShpLayerControl(map) {
  if (!map || typeof L === "undefined") return null;
  if (map._dtwLayerControl) return map._dtwLayerControl;
  const ctrl = L.control.layers(null, null, {
    position: "topright",
    collapsed: true,
    sortLayers: true
  }).addTo(map);
  map._dtwLayerControl = ctrl;
  if (typeof ctrl._addItem === "function") {
    const originalAddItem = ctrl._addItem;
    ctrl._addItem = function (obj) {
      const label = originalAddItem.call(this, obj);
      try {
        if (obj && obj.overlay) decorateShpLayerLabel(label, obj.layer);
      } catch (e) {}
      return label;
    };
  }
  map.on("overlayadd overlayremove", function () {
    syncDigitalTwinDrawings();
  });
  return ctrl;
}

function addImportedShpLayer(map, name, features) {
  const layer = L.geoJSON({ type: "FeatureCollection", features: features }, {
    style: shpLayerStyle,
    pointToLayer: function (_f, latlng) {
      return L.circleMarker(latlng, {
        radius: 6, color: "#f59e0b", fillColor: "#f59e0b", fillOpacity: 0.95, weight: 2
      });
    },
    onEachFeature: function (feature, lyr) {
      lyr.feature = feature;
    }
  });
  layer._importSourceFeatures = features;
  layer._visibleLabelFields = [];
  layer._dtwShpName = name;
  map.addLayer(layer);
  const ctrl = ensureShpLayerControl(map);
  if (ctrl) {
    ctrl.addOverlay(layer, name);
    if (typeof ctrl.expand === "function") ctrl.expand();
    setTimeout(function () { decorateShpLayerControl(ctrl); }, 0);
  }
  importedShpLayers.push({
    name: name,
    layer: layer,
    features: features,
    fields: fieldNamesFromShpFeatures(features)
  });
  return layer;
}

function removeImportedShpLayer(layer) {
  closeShpFieldsPopover();
  const map = drawMap || orthoMap;
  const ctrl = map && map._dtwLayerControl;
  importedShpLayers = importedShpLayers.filter(function (entry) { return entry.layer !== layer; });
  if (ctrl) {
    try { ctrl.removeLayer(layer); } catch (e) {}
  }
  if (map) {
    try { map.removeLayer(layer); } catch (e) {}
  }
  syncDigitalTwinDrawings();
}

async function loadShapefileOntoMap(fileList) {
  const files = (fileList || []).filter(function (f) { return f && f.name; });
  if (!files.length) return;
  const map = drawMap || orthoMap;
  if (!map) return;
  const form = new FormData();
  files.forEach(function (file) { form.append("files", file, file.name); });
  const buttons = shpOpenButtons();
  buttons.forEach(function (btn) { btn.disabled = true; });
  try {
    const res = await fetch("/api/draw/import-shp?view=fast", { method: "POST", body: form });
    const payload = await res.json();
    if (!res.ok || !payload || payload.success === false) {
      throw new Error((payload && (payload.message || payload.error)) || ("HTTP " + res.status));
    }
    const geojson = payload.geojson || {};
    const features = Array.isArray(geojson.features) ? geojson.features : [];
    if (!features.length) throw new Error("Shapefile không có đối tượng.");
    const label = uniqueShpLayerName((files[0] && files[0].name) || "shapefile");
    const layer = addImportedShpLayer(map, label, features);
    const bounds = layer.getBounds && layer.getBounds();
    if (bounds && bounds.isValid && bounds.isValid()) {
      map.fitBounds(bounds, { padding: [28, 28], maxZoom: 19 });
    } else if (Array.isArray(geojson.bbox) && geojson.bbox.length >= 4) {
      map.fitBounds([[geojson.bbox[1], geojson.bbox[0]], [geojson.bbox[3], geojson.bbox[2]]], {
        padding: [28, 28], maxZoom: 19
      });
    }
    syncDigitalTwinDrawings();
  } catch (err) {
    console.warn("Mở shapefile:", err);
  } finally {
    buttons.forEach(function (btn) { btn.disabled = false; });
  }
}

function destroyCesium() {
  if (cesiumViewer) {
    try {
      const canvas = cesiumViewer.scene && cesiumViewer.scene.canvas;
      if (canvas) {
        const gl = canvas.getContext("webgl2") || canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
        const lose = gl && gl.getExtension && gl.getExtension("WEBGL_lose_context");
        if (lose) lose.loseContext();
      }
    } catch (e) {}
    try { if (!cesiumViewer.isDestroyed()) cesiumViewer.destroy(); } catch (e) {}
    cesiumViewer = null;
  }
  lasPointTileset = null;
}

function waitFrame() {
  return new Promise(function (resolve) {
    requestAnimationFrame(function () { requestAnimationFrame(resolve); });
  });
}

function cesiumViewerOptions(webgl1, sharp) {
  const opts = {
    animation: false, timeline: false, baseLayerPicker: false, geocoder: false,
    homeButton: true, sceneModePicker: false, navigationHelpButton: false,
    fullscreenButton: false, infoBox: false, selectionIndicator: false,
    terrainProvider: new Cesium.EllipsoidTerrainProvider(),
    baseLayer: false, imageryProvider: false, requestRenderMode: false,
    orderIndependentTranslucency: false, msaaSamples: sharp ? 4 : 1,
    useBrowserRecommendedResolution: !sharp,
    contextOptions: {
      webgl: {
        alpha: false, depth: true, stencil: false, antialias: !!sharp,
        premultipliedAlpha: true, preserveDrawingBuffer: false,
        failIfMajorPerformanceCaveat: false, powerPreference: sharp ? "high-performance" : "default"
      }
    }
  };
  if (webgl1) {
    opts.contextOptions.requestWebgl1 = true;
    try { opts.contextOptions.requestWebgl2 = false; } catch (e) {}
  }
  return opts;
}

function createCesiumViewer(containerId, sharp) {
  let lastErr = null;
  const modes = [false, true];
  for (let i = 0; i < modes.length; i++) {
    try {
      return new Cesium.Viewer(containerId, cesiumViewerOptions(modes[i], !!sharp));
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr || new Error("Khong khoi tao duoc Cesium / WebGL");
}

async function api(url) {
  const r = await fetch(url);
  if (!r.ok) {
    const t = await r.text();
    try {
      const j = JSON.parse(t);
      throw new Error(j.error || t);
    } catch (e) {
      if (e instanceof SyntaxError) {
        throw new Error((t.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim() || ("HTTP " + r.status)).slice(0, 240));
      }
      throw e;
    }
  }
  return r;
}

function nav() {
  const el = document.getElementById("nav");
  el.innerHTML = PRODUCTS.map(p => {
    const mark = p.exists ? '<span class="ok">●</span>' : (p.rel ? '<span class="miss">○</span>' : '<span class="ok">●</span>');
    return `<a class="item${p.key===current?' active':''}" href="#${p.key}">${mark}<span>${p.label}</span></a>`;
  }).join("");
}

function stage(html) { document.getElementById("stage").innerHTML = html; }

async function show(key) {
  current = key;
  const p = PRODUCTS.find(x => x.key === key);
  document.getElementById("title").innerHTML = "<b>" + p.label + "</b>";
  setShpOpenVisible(key === "dtwin" || key === "obj");
  setRegionToolsVisible(key === "obj" || key === "ortho" || key === "dtwin");
  const shpBtn = document.getElementById("dtwOpenShp");
  if (shpBtn) shpBtn.title = key === "obj" ? "Cat mesh OBJ theo polygon (*.shp)" : "Mo shapefile (*.shp.zip)";
  nav();
  try {
    if (window.Dem3D) {
      try { Dem3D.destroyViewer(); } catch (e) {}
    }
    unbindDigitalTwin();
    destroyRegionDrawMap();
    destroyDrawOverlay();
    destroyOrthoMap();
    destroyCesium();
    hideCameraPreview();
    if (key === "overview") return overview();
    if (key === "dtwin") return digitalTwin();
    if (!p.exists && p.rel) {
      stage(`<div class="empty">Chua co file:<br>${p.rel}<br>Chay digital-twin.py --export-only neu da xu ly.</div>`);
      document.getElementById("meta").textContent = "thieu file";
      return;
    }
    if (key === "dem") return dem3d();
    if (key === "ortho") return ortho3d();
    if (key === "texture") {
      stage(`<img class="view" src="/media/texture.jpg?t=${Date.now()}" alt="${p.label}"/>`);
      document.getElementById("meta").textContent = p.rel;
      return;
    }
    if (key === "obj") return meshObj3d();
    if (key === "meshtiles") return meshTiles3d();
    if (key === "las") return points3d(key);
    if (key === "cameras") return cameras();
    if (key === "catalog" || key === "log") {
      const t = await (await api("/text/" + key)).text();
      stage("<pre></pre>");
      document.querySelector("pre").textContent = t;
      document.getElementById("meta").textContent = p.rel;
      return;
    }
    if (key === "report") {
      stage(`<div class="empty"><a href="/media/report.pdf" target="_blank" style="color:#38bdf8">Mo report.pdf</a></div>`);
      document.getElementById("meta").textContent = p.rel;
    }
  } catch (e) {
    stage(`<div class="empty">${e.message}</div>`);
  }
}

async function digitalTwin() {
  if (typeof L === "undefined") {
    stage('<div class="empty">Leaflet is not loaded.</div>');
    return;
  }
  document.getElementById("meta").textContent = "Digital Twin";
  stage('<div id="orthoHost"><div id="orthoMap"></div></div>');
  let bounds = null;
  let hasOrtho = false;
  let orthoInfo = null;
  try {
    const info = await (await fetch("/api/view3d/open")).then(function (r) { return r.json(); });
    if (info && info.success && info.bounds_wgs84) {
      bounds = info.bounds_wgs84;
      hasOrtho = true;
      orthoInfo = info;
    }
  } catch (e) {}
  if (!bounds) {
    try {
      const ov = await (await api("/api/overview")).json();
      const cams = ov.cameras || [];
      if (cams.length) {
        bounds = {
          west: Math.min.apply(null, cams.map(function (c) { return c.lon; })),
          south: Math.min.apply(null, cams.map(function (c) { return c.lat; })),
          east: Math.max.apply(null, cams.map(function (c) { return c.lon; })),
          north: Math.max.apply(null, cams.map(function (c) { return c.lat; }))
        };
      }
    } catch (e) {}
  }
  if (L.Icon && L.Icon.Default) {
    L.Icon.Default.imagePath = "/gis-static/vendors/leaflet/images/";
  }
  orthoMap = L.map("orthoMap", { maxZoom: MAP_MAX_ZOOM });
  L.tileLayer("/api/basemap/osm/{z}/{x}/{y}.png", {
    maxZoom: MAP_MAX_ZOOM, maxNativeZoom: 19, attribution: "© OpenStreetMap"
  }).addTo(orthoMap);
  if (bounds && [bounds.south, bounds.west, bounds.north, bounds.east].every(Number.isFinite)) {
    const box = [[bounds.south, bounds.west], [bounds.north, bounds.east]];
    if (hasOrtho) {
      addOrthoRasterLayer(orthoMap, orthoInfo, box);
    }
    orthoMap.fitBounds(box, { padding: [24, 24], maxZoom: 19 });
  } else {
    orthoMap.setView([21.177, 103.077], 15);
  }
  setTimeout(function () { try { orthoMap.invalidateSize(); } catch (e) {} }, 80);
  attachDrawToLeafletMap(orthoMap, document.getElementById("orthoHost"));
  ensureShpLayerControl(orthoMap);
  setShpOpenVisible(true);
  try {
    await enableDigitalTwinView();
    document.getElementById("meta").textContent = "Digital Twin";
  } catch (err) {
    document.getElementById("meta").textContent = String(err.message || err);
  }
}

async function dem3d() {
  if (!window.Dem3D || !window.THREE) {
    stage('<div class="empty">Khong tai duoc thu vien dem-3d (Three.js).</div>');
    return;
  }
  document.getElementById("meta").textContent = "DEM 3D (dem-3d)";
  stage(`
    <div id="demHost">
      <div id="view3d"></div>
      <div id="dem3dControls">
        <label>Do phong cao do
          <input id="demExaggeration" type="range" min="0.005" max="0.3" step="0.001" value="0.05"/>
        </label>
        <label style="display:flex;gap:6px;margin-top:8px;align-items:center">
          <input id="demContourToggle" type="checkbox"/> Duong dong muc
        </label>
        <div id="dem3dHint" class="dem3d-hint">Dang dung DEM 3D...</div>
      </div>
      <div id="dem3dLegend" class="dem3d-legend" hidden>
        <div class="dem3d-legend-title">Cao do</div>
        <div class="dem3d-legend-body">
          <div class="dem3d-legend-bar"></div>
          <div class="dem3d-legend-labels">
            <span id="dem3dLegendMax">—</span>
            <span id="dem3dLegendMid">—</span>
            <span id="dem3dLegendMin">—</span>
          </div>
        </div>
      </div>
    </div>`);
  const info = await (await api("/api/dem3d/open")).json();
  if (!info.success) throw new Error(info.message || "Khong mo duoc DEM");
  window.getOpenGeotiffEntries = () => [{ fileId: info.file_id, path: info.path, filename: info.filename }];
  const hint = document.getElementById("dem3dHint");
  const exag = document.getElementById("demExaggeration");
  const contour = document.getElementById("demContourToggle");
  if (exag) exag.addEventListener("input", () => Dem3D.setExaggeration(Number(exag.value)));
  if (contour) contour.addEventListener("change", () => Dem3D.setContoursVisible(contour.checked));
  Dem3D.show3DMode(true);
  await Dem3D.loadDem3D(info.file_id, 256);
  Dem3D.setContoursVisible(!!(contour && contour.checked));
  if (hint) hint.textContent = info.filename + "  " + (info.crs || "");
  document.getElementById("meta").textContent = (info.width || "") + " x " + (info.height || "") + "  " + (info.crs || "");
  mountDrawOverlay(document.getElementById("demHost"));
}

function addOrthoRasterLayer(map, info, box) {
  L.imageOverlay("/media/ortho.png?t=" + Date.now(), box, { opacity: 0.95, interactive: false }).addTo(map);
  let tileLayer = null;
  let skipFitZoom = true;
  let lastZ = null;
  const fileId = info && info.file_id;
  const ver = "?v=" + ((info && info.mtime != null) ? info.mtime : "0") + "a1";
  const showTiles = function () {
    if (!fileId || tileLayer) return;
    tileLayer = L.tileLayer("/api/geotiff/tiles/file/" + fileId + "/{z}/{x}/{y}.png" + ver, {
      maxZoom: MAP_MAX_ZOOM,
      maxNativeZoom: MAP_MAX_ZOOM,
      opacity: 1,
      bounds: box,
      noWrap: true,
      className: "ortho-xyz"
    }).addTo(map);
  };
  const requestZoomCache = function () {
    if (skipFitZoom) {
      skipFitZoom = false;
      return;
    }
    const z = Math.round(map.getZoom());
    if (!Number.isFinite(z) || z === lastZ) return;
    lastZ = z;
    const b = map.getBounds();
    const meta = document.getElementById("meta");
    const base = (meta && meta.dataset.orthoBase) || (meta && meta.textContent) || "";
    if (meta) meta.textContent = base + "  — cache z" + z;
    fetch("/api/ortho/cache", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        z: z,
        west: b.getWest(),
        south: b.getSouth(),
        east: b.getEast(),
        north: b.getNorth()
      })
    }).catch(function () {});
    showTiles();
  };
  map.on("zoomend", requestZoomCache);
}

async function ortho3d() {
  const info = await (await api("/api/view3d/open")).json();
  if (!info.success) throw new Error(info.message || "Khong mo duoc orthomosaic");
  return orthoMap2d(info);
}

async function orthoThree(info) {
  document.getElementById("meta").textContent = "Orthomosaic 3D (Three.js)";
  stage(`
    <div id="demHost">
      <div id="view3d"></div>
      <div id="dem3dControls">
        <label>Do phong cao do
          <input id="demExaggeration" type="range" min="0.005" max="0.3" step="0.001" value="0.05"/>
        </label>
      </div>
    </div>`);
  const dem = await (await api("/api/dem3d/open")).json();
  if (!dem.success) return orthoMap2d(info);
  window.getOpenGeotiffEntries = () => [{ fileId: dem.file_id, path: dem.path, filename: dem.filename }];
  const orthoTex = "/media/ortho.png?t=" + Date.now();
  async function paintOrtho() {
    try { await Dem3D.applyTextureUrl(orthoTex); } catch (e) {
      console.warn("Khong gan texture ortho:", e);
    }
  }
  const exag = document.getElementById("demExaggeration");
  if (exag) {
    exag.addEventListener("input", async () => {
      await Dem3D.setExaggeration(Number(exag.value));
      await paintOrtho();
    });
  }
  Dem3D.show3DMode(true);
  await Dem3D.loadDem3D(dem.file_id, 256);
  await paintOrtho();
  document.getElementById("meta").textContent =
    (info.filename || "orthomosaic") + "  " + (info.crs || "");
  mountDrawOverlay(document.getElementById("demHost"));
}

function seatTilesetOnGlobe(viewer, tileset) {
  try {
    const bs = tileset.boundingSphere;
    if (!bs || !Cesium.defined(bs.center)) return;
    const mag = Cesium.Cartesian3.magnitude(bs.center);
    // Metashape tileset da la ECEF WGS84 — khong keo ve height 0 (se lech globe/OSM).
    if (mag > 1e6) return;
    const carto = Cesium.Cartographic.fromCartesian(bs.center);
    if (!Cesium.defined(carto) || !Number.isFinite(carto.height) || Math.abs(carto.height) < 100) return;
    const surface = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, 0);
    const raised = Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, carto.height);
    const offset = Cesium.Cartesian3.subtract(surface, raised, new Cesium.Cartesian3());
    tileset.modelMatrix = Cesium.Matrix4.fromTranslation(offset);
  } catch (e) {}
}

function wgs84Rectangle(bounds, padDeg) {
  const b = bounds || {};
  const west = Number(b.west), south = Number(b.south), east = Number(b.east), north = Number(b.north);
  if (![west, south, east, north].every(Number.isFinite) || east <= west || north <= south) return undefined;
  const pad = Number(padDeg) || 0;
  return Cesium.Rectangle.fromDegrees(west - pad, south - pad, east + pad, north + pad);
}

function fadeOsmWhenInspectingMesh(scene, tileset) {
  const globe = scene.globe;
  try { globe.show = true; } catch (e) { return; }
  try { globe.cartographicLimitRectangle = undefined; } catch (e) {}
  try { globe.translucency.enabled = true; } catch (e) { return; }
  try { globe.translucency.backFaceAlpha = 0; } catch (e) {}
  const nearM = 80;
  const farM = 900;
  const update = function () {
    let metric = 1e9;
    try {
      const carto = scene.camera.positionCartographic;
      if (carto && Number.isFinite(carto.height)) metric = Math.min(metric, carto.height);
    } catch (e) {}
    try {
      const bs = tileset.boundingSphere;
      if (bs) {
        const clearance = Cesium.Cartesian3.distance(scene.camera.positionWC, bs.center) - bs.radius;
        metric = clearance < 0 ? 0 : Math.min(metric, clearance);
      }
    } catch (e) {}
    let alpha = 1;
    if (metric <= nearM) alpha = 0;
    else if (metric < farM) alpha = (metric - nearM) / (farM - nearM);
    try { globe.translucency.frontFaceAlpha = alpha; } catch (e) {}
  };
  try { scene.preRender.addEventListener(update); } catch (e) {}
  update();
}

function lasPointControlsHtml() {
  const st = lasPointStyle || { size: 4, colorMode: "rgb", color: "#38bdf8" };
  const size = Number(st.size) || 4;
  const mode = st.colorMode || "rgb";
  const hex = st.color || "#38bdf8";
  return (
    '<div id="lasPointControls">' +
      '<div class="lasCtlRow"><span>Kich thuoc</span>' +
        '<input id="lasPointSize" type="range" min="1" max="24" step="0.5" value="' + size + '"/>' +
        '<span id="lasPointSizeVal">' + size + '</span></div>' +
      '<div class="lasCtlRow"><span>Mau</span>' +
        '<select id="lasPointColorMode">' +
          '<option value="rgb"' + (mode === "rgb" ? " selected" : "") + '>RGB goc</option>' +
          '<option value="solid"' + (mode === "solid" ? " selected" : "") + '>Mot mau</option>' +
          '<option value="height"' + (mode === "height" ? " selected" : "") + '>Theo cao do</option>' +
        '</select>' +
        '<input id="lasPointColor" type="color" value="' + hex + '" title="Mau tuy chinh"' +
          (mode === "solid" ? "" : " hidden") + "/></div>" +
    "</div>"
  );
}

function applyLasPointStyle() {
  const size = Math.max(1, Math.min(24, Number(lasPointStyle.size) || 4));
  const mode = lasPointStyle.colorMode || "rgb";
  const hex = lasPointStyle.color || "#38bdf8";
  if (lasPointTileset && window.Cesium) {
    const spec = { pointSize: String(size) };
    if (mode === "solid") spec.color = "color('" + hex + "')";
    else if (mode === "height") {
      spec.color =
        "hsl(240.0 - 240.0 * clamp((${POSITION.z} + 40.0) / 120.0, 0.0, 1.0), 1.0, 0.55)";
    }
    try { lasPointTileset.style = new Cesium.Cesium3DTileStyle(spec); } catch (e) {}
    try {
      if (lasPointTileset.pointCloudShading) {
        lasPointTileset.pointCloudShading.attenuation = false;
        lasPointTileset.pointCloudShading.eyeDomeLighting = false;
      }
    } catch (e) {}
    try { if (cesiumViewer && cesiumViewer.scene) cesiumViewer.scene.requestRender(); } catch (e) {}
  }
  if (window.Dem3D && typeof Dem3D.setPointAppearance === "function") {
    Dem3D.setPointAppearance({ size: size, colorMode: mode, color: hex });
  }
}

function bindLasPointControls() {
  const sizeEl = document.getElementById("lasPointSize");
  const valEl = document.getElementById("lasPointSizeVal");
  const modeEl = document.getElementById("lasPointColorMode");
  const colorEl = document.getElementById("lasPointColor");
  const sync = function () {
    if (sizeEl) lasPointStyle.size = Number(sizeEl.value) || 4;
    if (modeEl) lasPointStyle.colorMode = modeEl.value;
    if (colorEl) lasPointStyle.color = colorEl.value;
    if (valEl) valEl.textContent = String(lasPointStyle.size);
    if (colorEl) colorEl.hidden = lasPointStyle.colorMode !== "solid";
    applyLasPointStyle();
  };
  if (sizeEl) sizeEl.addEventListener("input", sync);
  if (modeEl) modeEl.addEventListener("change", sync);
  if (colorEl) colorEl.addEventListener("input", sync);
}

function mountLasPointControls(host) {
  if (!host) return;
  let el = document.getElementById("lasPointControls");
  if (!el) {
    host.insertAdjacentHTML("beforeend", lasPointControlsHtml());
  }
  bindLasPointControls();
  applyLasPointStyle();
}

function pointTilesetOptions() {
  // geometricError cua pnts = khoang cach diem (m): SSE 2 px → hien ban tho truoc, tai chi tiet quanh camera.
  // Tai lien tuc khi xoay/zoom: khong huy request luc camera chay, tai san tile ke ben.
  return {
    maximumScreenSpaceError: 2,
    skipLevelOfDetail: false,
    immediatelyLoadDesiredLevelOfDetail: false,
    loadSiblings: true,
    dynamicScreenSpaceError: false,
    foveatedScreenSpaceError: false,
    preferLeaves: false,
    preloadWhenHidden: false,
    preloadAncestors: true,
    backFaceCulling: false,
    cullRequestsWhileMoving: false,
    progressiveResolutionHeightFraction: 0.3,
    cacheBytes: 2 * 1024 * 1024 * 1024,
    maximumCacheOverflowBytes: 1024 * 1024 * 1024
  };
}

function tunePointTileset(tileset) {
  const opts = pointTilesetOptions();
  Object.keys(opts).forEach(function (key) {
    try { tileset[key] = opts[key]; } catch (e) {}
  });
  lasPointTileset = tileset;
  applyLasPointStyle();
}

function meshTilesetOptions() {
  // geometricError cua b3dm = o gom dinh LOD (m): SSE 1 → vung xa net hon, ~3-4x so tile cua SSE 2.
  return {
    // Tai lien tuc khi xoay/zoom: khong huy request luc camera chay, giu tile cha den khi con san sang.
    maximumScreenSpaceError: 1,
    skipLevelOfDetail: false,
    immediatelyLoadDesiredLevelOfDetail: false,
    loadSiblings: true,
    cullWithChildrenBounds: false,
    dynamicScreenSpaceError: false,
    foveatedScreenSpaceError: false,
    preferLeaves: false,
    preloadWhenHidden: false,
    preloadAncestors: true,
    backFaceCulling: false,
    cullRequestsWhileMoving: false,
    progressiveResolutionHeightFraction: 0.3,
    cacheBytes: 4 * 1024 * 1024 * 1024,
    maximumCacheOverflowBytes: 1024 * 1024 * 1024
  };
}

function waitMeshTilesFilled(tileset, ms) {
  return new Promise(function (resolve) {
    let done = false;
    const finish = function () {
      if (done) return;
      done = true;
      resolve();
    };
    try {
      if (tileset.tilesLoaded) return finish();
    } catch (e) {}
    try { tileset.allTilesLoaded.addEventListener(finish); } catch (e) {}
    const t0 = Date.now();
    const poll = function () {
      if (done) return;
      try { if (tileset.tilesLoaded) return finish(); } catch (e) {}
      if (Date.now() - t0 > (ms || 90000)) return finish();
      setTimeout(poll, 400);
    };
    poll();
  });
}

function meshTilesStats(tileset) {
  try {
    const s = tileset.statistics;
    if (!s) return "";
    const ready = s.numberOfTilesWithContentReady;
    const total = s.numberOfTilesTotal;
    if (ready != null) return "  " + ready + (total ? ("/" + total) : "") + " tile";
  } catch (e) {}
  return "";
}

function tuneMeshTileset(tileset) {
  const opts = meshTilesetOptions();
  Object.keys(opts).forEach(function (key) {
    try { tileset[key] = opts[key]; } catch (e) {}
  });
  try {
    // Giu parent den khi child + texture san sang — giam vien den REPLACE.
    tileset.tileVisible.addEventListener(function () {
      try { cesiumViewer && cesiumViewer.scene && cesiumViewer.scene.requestRender(); } catch (e2) {}
    });
  } catch (e) {}
  try {
    if (Cesium.CustomShader && Cesium.LightingModel) {
      tileset.customShader = new Cesium.CustomShader({
        lightingModel: Cesium.LightingModel.UNLIT
      });
    }
  } catch (e) {}
  try {
    if (tileset.imageBasedLighting && Cesium.Cartesian2) {
      tileset.imageBasedLighting.imageBasedLightingFactor = new Cesium.Cartesian2(1.0, 0.0);
    }
  } catch (e) {}
}

function frameTilesetOnBasemap(viewer, tileset) {
  const bs = tileset.boundingSphere;
  if (!bs) return;
  const range = Math.max(bs.radius * 2.8, 80);
  viewer.camera.viewBoundingSphere(
    bs,
    new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-40), range)
  );
  viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
  viewer.scene.requestRender();
}

async function orthoTiles3d(info) {
  document.getElementById("meta").textContent = "3D Tiles (Cesium / view-3d)";
  destroyCesium();
  stage('<div id="orthoHost"><div id="cesiumContainer"></div></div>');
  const host = document.getElementById("cesiumContainer");
  if (host && (host.clientWidth < 8 || host.clientHeight < 8)) {
    host.style.minHeight = "560px";
  }
  await waitFrame();
  if (Cesium.Ion) Cesium.Ion.defaultAccessToken = undefined;
  try {
    cesiumViewer = createCesiumViewer("cesiumContainer", !!(info && info.meshTiles));
  } catch (e) {
    if (info && (info.meshTiles || info.pointTiles)) throw e;
    document.getElementById("meta").textContent = "WebGL loi — chuyen sang 2D";
    return orthoMap2d(info);
  }
  try {
    const credit = cesiumViewer.cesiumWidget && cesiumViewer.cesiumWidget.creditContainer;
    if (credit) credit.style.display = "none";
  } catch (e) {}
  const scene = cesiumViewer.scene;
  const meshTiles = !!(info && info.meshTiles);
  const pointTiles = !!(info && info.pointTiles);
  const globeTiles = meshTiles || pointTiles;
  scene.globe.show = true;
  // Nen khong den thuan — khe tile khi LOD khong hien vien den.
  scene.globe.baseColor = Cesium.Color.fromCssColorString(meshTiles ? "#4a5a48" : "#1a2330");
  scene.backgroundColor = Cesium.Color.fromCssColorString("#0b1220");
  scene.globe.depthTestAgainstTerrain = false;
  scene.globe.enableLighting = false;
  try { scene.globe.showGroundAtmosphere = false; } catch (e) {}
  try { scene.fog.enabled = false; } catch (e) {}
  try { scene.skyAtmosphere.show = true; } catch (e) {}
  try { scene.sun.show = true; } catch (e) {}
  try { scene.moon.show = false; } catch (e) {}
  try { scene.skyBox.show = true; } catch (e) {}
  try {
    scene.globe.tileCacheSize = 20;
    scene.globe.maximumScreenSpaceError = 4;
    scene.globe.preloadSiblings = false;
  } catch (e) {}
  try { scene.logarithmicDepthBuffer = true; } catch (e) {}
  if (meshTiles) {
    try { cesiumViewer.resolutionScale = Math.min(window.devicePixelRatio || 1, 2); } catch (e) {}
    try { scene.msaaSamples = 4; } catch (e) {}
    try { scene.postProcessStages.fxaa.enabled = true; } catch (e) {}
    try { scene.highDynamicRange = false; } catch (e) {}
  }
  try {
    if (globeTiles && scene.globe.undergroundColorAlphaByDistance) {
      scene.globe.undergroundColorAlphaByDistance.near = 0;
      scene.globe.undergroundColorAlphaByDistance.nearValue = 0;
      scene.globe.undergroundColorAlphaByDistance.far = 1;
      scene.globe.undergroundColorAlphaByDistance.farValue = 0;
    }
  } catch (e) {}
  const sscc = scene.screenSpaceCameraController;
  sscc.enableCollisionDetection = false;
  sscc.minimumZoomDistance = 0.05;
  sscc.maximumZoomDistance = 500000;
  sscc.enableRotate = true;
  sscc.enableTranslate = true;
  sscc.enableZoom = true;
  sscc.enableTilt = true;
  try { cesiumViewer.imageryLayers.removeAll(); } catch (e) {}
  try {
    cesiumViewer.imageryLayers.addImageryProvider(new Cesium.UrlTemplateImageryProvider({
      url: "/api/basemap/osm/{z}/{x}/{y}.png",
      tilingScheme: new Cesium.WebMercatorTilingScheme(),
      minimumLevel: 0,
      maximumLevel: 19
    }));
  } catch (e) {}
  if (!globeTiles) {
    try {
      const b = info.bounds_wgs84 || {};
      const west = Number(b.west), south = Number(b.south), east = Number(b.east), north = Number(b.north);
      if (info.file_id && [west, south, east, north].every(Number.isFinite)) {
        const ver = "?v=" + ((info.mtime != null) ? info.mtime : "0");
        cesiumViewer.imageryLayers.addImageryProvider(new Cesium.UrlTemplateImageryProvider({
          url: "/api/geotiff/tiles/file/" + info.file_id + "/{z}/{x}/{y}.png" + ver,
          tilingScheme: new Cesium.WebMercatorTilingScheme(),
          rectangle: Cesium.Rectangle.fromDegrees(west, south, east, north),
          minimumLevel: 0,
          maximumLevel: MAP_MAX_ZOOM
        }));
      }
    } catch (e) {}
  }
  const tilesUrl = info.tileset_url || "/tiles/tileset.json";
  try {
    // Mac dinh 18 request/server: server local chiu duoc nhieu hon → tile ve nhanh hon khi xoay.
    Cesium.RequestScheduler.maximumRequestsPerServer = 48;
    Cesium.RequestScheduler.maximumRequests = 96;
  } catch (e) {}
  const tilesetOpts = meshTiles
    ? meshTilesetOptions()
    : (pointTiles ? pointTilesetOptions() : { maximumScreenSpaceError: 1 });
  let tileset;
  try {
    if (typeof Cesium.Cesium3DTileset.fromUrl === "function") {
      tileset = await Cesium.Cesium3DTileset.fromUrl(tilesUrl, tilesetOpts);
    } else {
      tileset = new Cesium.Cesium3DTileset(Object.assign({ url: tilesUrl }, tilesetOpts));
      await tileset.readyPromise;
    }
  } catch (e) {
    throw new Error("Khong tai duoc " + tilesUrl + " — " + ((e && e.message) || e));
  }
  if (!cesiumViewer.scene.primitives.contains(tileset)) {
    cesiumViewer.scene.primitives.add(tileset);
  }
  if (meshTiles) tuneMeshTileset(tileset);
  if (pointTiles) {
    tunePointTileset(tileset);
    mountLasPointControls(document.getElementById("orthoHost"));
  }
  try {
    tileset.imageBasedLighting && Cesium.Cartesian2 &&
      (tileset.imageBasedLighting.imageBasedLightingFactor = new Cesium.Cartesian2(1.0, 1.0));
  } catch (e) {}
  if (globeTiles) {
    fadeOsmWhenInspectingMesh(scene, tileset);
  } else {
    try { scene.globe.translucency.enabled = false; } catch (e) {}
  }
  const ping = function () {
    try { scene.requestRender(); } catch (e) {}
    if (globeTiles) {
      try {
        document.getElementById("meta").textContent =
          "3D Tiles  " + tilesUrl + "  dang tai" + meshTilesStats(tileset);
      } catch (e) {}
    }
  };
  try { tileset.tileLoad.addEventListener(ping); } catch (e) {}
  try { tileset.allTilesLoaded.addEventListener(ping); } catch (e) {}
  try { cesiumViewer.camera.moveEnd.addEventListener(ping); } catch (e) {}
  seatTilesetOnGlobe(cesiumViewer, tileset);
  frameTilesetOnBasemap(cesiumViewer, tileset);
  try {
    await cesiumViewer.zoomTo(tileset, new Cesium.HeadingPitchRange(
      0, Cesium.Math.toRadians(-40), Math.max(tileset.boundingSphere.radius * 2.8, 80)
    ));
  } catch (e) {
    frameTilesetOnBasemap(cesiumViewer, tileset);
  }
  if (meshTiles) {
    try { await waitMeshTilesFilled(tileset, 180000); } catch (e) {}
    frameTilesetOnBasemap(cesiumViewer, tileset);
    try { scene.requestRender(); } catch (e) {}
  }
  ping();
  try {
    cesiumViewer.homeButton.viewModel.command.beforeExecute.addEventListener(function (e) {
      e.cancel = true;
      frameTilesetOnBasemap(cesiumViewer, tileset);
    });
  } catch (e) {}
  document.getElementById("meta").textContent =
    "3D Tiles  " + tilesUrl + "  " + (info.filename || "") + "  " + (info.crs || "") +
    (globeTiles ? meshTilesStats(tileset) : "");
}

async function orthoMap2d(info) {
  if (typeof L === "undefined") {
    stage('<div class="empty">Khong tai duoc Leaflet de xem orthomosaic.</div>');
    return;
  }
  stage('<div id="orthoHost"><div id="orthoMap"></div></div>');
  const b = info.bounds_wgs84 || {};
  const south = Number(b.south), west = Number(b.west), north = Number(b.north), east = Number(b.east);
  if (![south, west, north, east].every(Number.isFinite)) {
    throw new Error("Orthomosaic khong co bounds WGS84");
  }
  if (L.Icon && L.Icon.Default) {
    L.Icon.Default.imagePath = "/gis-static/vendors/leaflet/images/";
  }
  const box = [[south, west], [north, east]];
  orthoMap = L.map("orthoMap", { maxZoom: MAP_MAX_ZOOM });
  L.tileLayer("/api/basemap/osm/{z}/{x}/{y}.png", {
    maxZoom: MAP_MAX_ZOOM, maxNativeZoom: 19, attribution: "© OpenStreetMap"
  }).addTo(orthoMap);
  addOrthoRasterLayer(orthoMap, info, box);
  orthoMap.fitBounds(box, { padding: [24, 24], maxZoom: 19 });
  setTimeout(() => { try { orthoMap.invalidateSize(); } catch (e) {} }, 80);
  attachDrawToLeafletMap(orthoMap, document.getElementById("orthoHost"));
  const size = (info.width && info.height) ? (info.width + " x " + info.height + "  ") : "";
  const baseMeta = "Orthomosaic  " + size + (info.crs || "") + "  — GeoTIFF; zoom de tao cache";
  const metaEl = document.getElementById("meta");
  if (metaEl) {
    metaEl.dataset.orthoBase = baseMeta;
    metaEl.textContent = baseMeta;
  }
}

function hideCameraPreview() {
  const box = document.getElementById("camPreview");
  const img = document.getElementById("camPreviewImg");
  const meta = document.getElementById("camPreviewMeta");
  const frame = document.getElementById("camPreviewFrame");
  const empty = document.getElementById("camPreviewEmpty");
  if (box) box.classList.remove("visible");
  if (frame) frame.classList.remove("has-img");
  if (empty) empty.textContent = "Click diem tren duong bay";
  if (img) { img.removeAttribute("src"); img.alt = "Anh UAV"; }
  if (meta) meta.textContent = "Click mot diem tren duong bay de xem anh UAV";
}

function showCameraPreview(row) {
  const box = document.getElementById("camPreview");
  const img = document.getElementById("camPreviewImg");
  const meta = document.getElementById("camPreviewMeta");
  const frame = document.getElementById("camPreviewFrame");
  const empty = document.getElementById("camPreviewEmpty");
  if (!box || !img || !row) return;
  const label = String(row.label || "").trim();
  if (!label) return;
  box.classList.add("visible");
  if (frame) frame.classList.remove("has-img");
  if (empty) empty.textContent = "Dang tai anh...";
  if (meta) {
    meta.textContent = label +
      (Number.isFinite(row.lon) ? "\n" + row.lat.toFixed(6) + ", " + row.lon.toFixed(6) : "") +
      (Number.isFinite(row.alt) ? "  " + row.alt.toFixed(1) + " m" : "");
  }
  img.onload = function () {
    if (frame) frame.classList.add("has-img");
    if (empty) empty.textContent = "";
  };
  img.onerror = function () {
    if (frame) frame.classList.remove("has-img");
    const dir = ((document.getElementById("imagesPath") || {}).value || "").trim();
    if (empty) empty.textContent = "Khong tim thay anh " + label + (dir ? " trong cac thu muc input" : "");
  };
  img.alt = label;
  const load = function () {
    img.src = "/media/photo/" + encodeURIComponent(label) + "?w=720&t=" + Date.now();
  };
  const images = ((document.getElementById("imagesPath") || {}).value || "").trim();
  if (!images) {
    load();
    return;
  }
  fetch("/api/paths", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ images: images })
  }).then(function (r) { return r.json(); }).then(function (data) {
    fillPaths(data);
    load();
  }).catch(function () { load(); });
}

function rowFromPlotPoint(pt, rows) {
  if (!pt) return null;
  const i = (pt.pointIndex != null) ? pt.pointIndex : pt.pointNumber;
  if (i != null && rows[i]) return rows[i];
  let lab = pt.customdata;
  if (Array.isArray(lab)) lab = lab[0];
  if (!lab && pt.text) lab = String(pt.text).split("<br>")[0];
  if (lab) {
    const found = rows.find(r => r.label === lab);
    if (found) return found;
    return { label: lab, lon: pt.x, lat: pt.y, alt: pt.z };
  }
  return null;
}

function bindFlightPhotoClick(plotId, rows) {
  const plot = document.getElementById(plotId);
  if (!plot || typeof plot.on !== "function") return;
  const pick = function (ev) {
    const pt = ev && ev.points && ev.points[0];
    const row = rowFromPlotPoint(pt, rows);
    if (row) showCameraPreview(row);
  };
  plot.on("plotly_click", pick);
}

async function overview() {
  const data = await (await api("/api/overview")).json();
  const nimg = (data.images || []).length;
  document.getElementById("meta").textContent = data.summary.cameras
    ? `${data.summary.cameras} camera, quality ${data.summary.quality || ""}${nimg > 1 ? ", " + nimg + " thu muc anh" : ""}`
    : (nimg ? nimg + " thu muc anh" : "");
  const rows = data.cameras || [];
  stage('<div id="plot"></div>');
  const preview = document.getElementById("camPreview");
  if (preview) preview.classList.add("visible");
  if (!rows.length) {
    document.getElementById("plot").innerHTML = '<div class="empty">Chua co camera_reference.csv</div>';
    return;
  }
  await Plotly.newPlot("plot", [{
    x: rows.map(r => r.lon), y: rows.map(r => r.lat), mode: "lines+markers",
    marker: { size: 11, color: rows.map(r => r.alt), colorscale: "Plasma", colorbar: { title: "m" } },
    line: { color: "#38bdf8", width: 1 },
    text: rows.map(r => r.label),
    customdata: rows.map(r => r.label),
    hoverinfo: "text+x+y",
    hoveron: "points"
  }], {
    paper_bgcolor: "#0b1220", plot_bgcolor: "#0b1220",
    font: { color: "#e5e7eb" },
    margin: { t: 30, r: 20, b: 40, l: 50 },
    title: "Duong bay " + rows.length + " camera" + (nimg > 1 ? " / " + nimg + " thu muc" : "") + " — click diem de xem anh",
    dragmode: "pan",
    hovermode: "closest",
    clickmode: "event",
    xaxis: { title: "Longitude", gridcolor: "#1f2937" },
    yaxis: { title: "Latitude", scaleanchor: "x", gridcolor: "#1f2937" }
  }, { responsive: true, scrollZoom: true, displaylogo: false });
  bindFlightPhotoClick("plot", rows);
}

async function cameras() {
  const rows = await (await api("/api/cameras")).json();
  document.getElementById("meta").textContent = rows.length + " camera";
  stage('<div id="plot"></div>');
  Plotly.newPlot("plot", [{
    x: rows.map(r => r.lon), y: rows.map(r => r.lat), mode: "markers+lines",
    marker: { size: 11, color: rows.map(r => r.alt), colorscale: "Plasma", colorbar: { title: "Altitude" } },
    line: { color: "#64748b", width: 1 },
    text: rows.map(r => r.label),
    customdata: rows.map(r => r.label),
    hoverinfo: "text+x+y",
    hoveron: "points"
  }], {
    paper_bgcolor: "#0b1220", plot_bgcolor: "#0b1220", font: { color: "#e5e7eb" },
    margin: { t: 30, r: 10, b: 40, l: 50 },
    dragmode: "pan",
    hovermode: "closest",
    clickmode: "event",
    xaxis: { title: "Longitude", gridcolor: "#1f2937" },
    yaxis: { title: "Latitude", scaleanchor: "x", gridcolor: "#1f2937" }
  }, { responsive: true, scrollZoom: true, displaylogo: false }).then(function () {
    const preview = document.getElementById("camPreview");
    if (preview) preview.classList.add("visible");
    bindFlightPhotoClick("plot", rows);
  });
}

async function gunzipBuffer(buf) {
  if (typeof DecompressionStream === "undefined") return buf;
  const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
  return await new Response(stream).arrayBuffer();
}

function openBinCache() {
  return new Promise(function (resolve) {
    if (!window.indexedDB) { resolve(null); return; }
    const req = indexedDB.open("dt-view-bins", 2);
    req.onupgradeneeded = function () {
      const db = req.result;
      if (db.objectStoreNames.contains("bins")) db.deleteObjectStore("bins");
      db.createObjectStore("bins");
    };
    req.onsuccess = function () { resolve(req.result); };
    req.onerror = function () { resolve(null); };
  });
}

function asArrayBuffer(hit) {
  if (!hit) return null;
  if (hit instanceof ArrayBuffer) return hit;
  if (hit.buffer instanceof ArrayBuffer) {
    return hit.byteLength === hit.buffer.byteLength ? hit.buffer : hit.buffer.slice(hit.byteOffset, hit.byteOffset + hit.byteLength);
  }
  return null;
}

function meshMagicOk(buf) {
  if (!buf || buf.byteLength < 4) return false;
  const u = new Uint8Array(buf, 0, 4);
  const mag = String.fromCharCode(u[0], u[1], u[2], u[3]);
  return mag === "DTM1" || mag === "DTM2" || mag === "DTP1" || mag === "DTP2";
}

async function binCacheGet(key) {
  const db = await openBinCache();
  if (!db) return null;
  return new Promise(function (resolve) {
    try {
      const q = db.transaction("bins").objectStore("bins").get(key);
      q.onsuccess = function () { resolve(q.result || null); };
      q.onerror = function () { resolve(null); };
    } catch (e) { resolve(null); }
  });
}

function binCachePut(key, buf) {
  openBinCache().then(function (db) {
    if (!db) return;
    try { db.transaction("bins", "readwrite").objectStore("bins").put(buf, key); } catch (e) {}
  });
}

async function fetchBufferProgress(url, onProgress, expectedTotal) {
  const r = await api(url);
  const packed = (r.headers.get("X-Bin-Encoding") || "").toLowerCase();
  const total = Number(r.headers.get("Content-Length")) || Number(expectedTotal) || 0;
  if (!r.body || !r.body.getReader) {
    let buf = await r.arrayBuffer();
    if (packed === "gzip") buf = await gunzipBuffer(buf);
    if (onProgress) onProgress(100, buf.byteLength, total || buf.byteLength);
    return buf;
  }
  const reader = r.body.getReader();
  const chunks = [];
  let received = 0;
  while (true) {
    const step = await reader.read();
    if (step.done) break;
    chunks.push(step.value);
    received += step.value.byteLength;
    const pct = total > 0 ? Math.min(99, Math.round(received * 100 / total)) : 0;
    if (onProgress) onProgress(pct, received, total);
  }
  const out = new Uint8Array(received);
  let off = 0;
  for (let i = 0; i < chunks.length; i++) {
    out.set(chunks[i], off);
    off += chunks[i].byteLength;
  }
  let buf = out.buffer;
  if (packed === "gzip") {
    if (onProgress) onProgress(99, received, total || received);
    buf = await gunzipBuffer(buf);
  }
  if (onProgress) onProgress(100, received, total || received);
  return buf;
}

async function fetchViewBin(url, onProgress, expectedTotal, cacheKey) {
  const tooBig = Number(expectedTotal) > 80 * 1048576;
  if (cacheKey && !tooBig) {
    const hit = asArrayBuffer(await binCacheGet(cacheKey));
    if (hit && meshMagicOk(hit)) {
      if (onProgress) onProgress(100, hit.byteLength, hit.byteLength);
      return hit.slice(0);
    }
  }
  const buf = await fetchBufferProgress(url, onProgress, expectedTotal);
  if (cacheKey && !tooBig && meshMagicOk(buf) && buf.byteLength < 80 * 1048576) binCachePut(cacheKey, buf.slice(0));
  return buf;
}

function fmtMb(n) {
  return (n / 1048576).toFixed(1) + " MB";
}

function setLasProgress(pct, label, meta) {
  const wrap = document.getElementById("lasProgress");
  const bar = document.getElementById("lasProgressBar");
  const pctEl = document.getElementById("lasProgressPct");
  const lab = document.getElementById("lasProgressLabel");
  const metaEl = document.getElementById("lasProgressMeta");
  if (!wrap) return;
  wrap.hidden = false;
  const v = Math.max(0, Math.min(100, Math.round(pct)));
  if (bar) bar.style.width = v + "%";
  if (pctEl) pctEl.textContent = v + "%";
  if (lab && label) {
    const text = document.getElementById("lasProgressText");
    if (text) text.textContent = label;
    else lab.textContent = label + " " + v + "%";
  }
  if (metaEl && meta != null) metaEl.textContent = meta;
  const header = document.getElementById("meta");
  if (header) header.textContent = (label || "Dang tai") + "  " + v + "%";
}

function hideLasProgress() {
  const wrap = document.getElementById("lasProgress");
  if (wrap) wrap.hidden = true;
}

let meshClipId = "";
let meshWgsBounds = null;
let regionDrawMap = null;

function setRegionToolsVisible(visible) {
  const drawBtn = document.getElementById("btnDrawRegion");
  const optBtn = document.getElementById("btnOptimizeData");
  const saveBtn = document.getElementById("btnSaveMesh");
  if (drawBtn) drawBtn.hidden = !visible;
  if (optBtn) optBtn.hidden = !visible;
  if (saveBtn) saveBtn.hidden = !visible || !meshClipId;
}

function setSaveMeshVisible() {
  const saveBtn = document.getElementById("btnSaveMesh");
  if (saveBtn) saveBtn.hidden = !(current === "obj" && meshClipId);
}

function destroyRegionDrawMap() {
  snapshotDrawings();
  if (regionDrawMap) {
    if (drawMap === regionDrawMap) drawMap = null;
    try { regionDrawMap.remove(); } catch (e) {}
    regionDrawMap = null;
  }
  const host = document.getElementById("regionDrawHost");
  if (host) host.remove();
}

function collectDrawnPolygons() {
  snapshotDrawings();
  const features = [];
  function addFeature(feat) {
    const t = feat && feat.geometry && feat.geometry.type;
    if (t === "Polygon" || t === "MultiPolygon") features.push(feat);
  }
  if (window.Dem3D && typeof Dem3D.getRegionGeoJSON === "function") {
    const drawn = Dem3D.getRegionGeoJSON({ closeIfNeeded: true });
    (drawn.features || []).forEach(addFeature);
  }
  const src = (drawItems && typeof drawItems.toGeoJSON === "function")
    ? drawItems.toGeoJSON()
    : (drawGeojson || { type: "FeatureCollection", features: [] });
  (src.features || []).forEach(addFeature);
  if (drawItems && typeof L !== "undefined" && L.Circle) {
    drawItems.eachLayer(function (layer) {
      if (!(layer instanceof L.Circle)) return;
      const c = layer.getLatLng();
      const r = layer.getRadius();
      const ring = [];
      const cosLat = Math.cos((c.lat * Math.PI) / 180) || 1e-6;
      for (let i = 0; i < 32; i++) {
        const ang = (i / 32) * Math.PI * 2;
        ring.push([
          c.lng + (r * Math.sin(ang)) / (111320 * cosLat),
          c.lat + (r * Math.cos(ang)) / 111320
        ]);
      }
      ring.push(ring[0]);
      addFeature({ type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [ring] } });
    });
  }
  return { type: "FeatureCollection", features: features };
}

async function fetchMeshBounds() {
  try {
    const geo = await (await api("/api/mesh/obj.meta.json")).json();
    const b = geo && geo.bounds_wgs84;
    if (b && [b.west, b.south, b.east, b.north].every(function (v) { return Number.isFinite(Number(v)); })) {
      meshWgsBounds = {
        west: Number(b.west), south: Number(b.south),
        east: Number(b.east), north: Number(b.north)
      };
      return meshWgsBounds;
    }
  } catch (e) {}
  return meshWgsBounds;
}

async function ensureRegionDrawMap() {
  const hostEl = document.getElementById("demHost");
  if (!hostEl || typeof L === "undefined") {
    throw new Error("Chua tai duoc ban do de ve vung");
  }
  let wrap = document.getElementById("regionDrawHost");
  if (!wrap) {
    wrap = document.createElement("div");
    wrap.id = "regionDrawHost";
    const mapDiv = document.createElement("div");
    mapDiv.id = "regionDrawMap";
    wrap.appendChild(mapDiv);
    hostEl.appendChild(wrap);
  }
  wrap.hidden = false;
  if (!regionDrawMap) {
    if (L.Icon && L.Icon.Default) L.Icon.Default.imagePath = "/gis-static/vendors/leaflet/images/";
    regionDrawMap = L.map("regionDrawMap", { maxZoom: MAP_MAX_ZOOM });
    L.tileLayer("/api/basemap/osm/{z}/{x}/{y}.png", {
      maxZoom: MAP_MAX_ZOOM, maxNativeZoom: 19, attribution: "© OpenStreetMap"
    }).addTo(regionDrawMap);
    addDrawControl(regionDrawMap, wrap);
  }
  const b = meshWgsBounds || await fetchMeshBounds();
  if (b) {
    regionDrawMap.fitBounds([[b.south, b.west], [b.north, b.east]], { padding: [24, 24], maxZoom: 19 });
  } else {
    regionDrawMap.setView([21.03, 105.85], 14);
  }
  setTimeout(function () { try { regionDrawMap.invalidateSize(); } catch (e) {} }, 80);
  return regionDrawMap;
}

async function startDrawRegion() {
  try {
    if (current === "obj") {
      destroyRegionDrawMap();
      if (!window.Dem3D || typeof Dem3D.startRegionDraw !== "function") {
        throw new Error("Chua mo Mesh OBJ");
      }
      Dem3D.startRegionDraw();
      document.getElementById("meta").textContent =
        "Ve tren mesh: click them dinh, click diem vang hoac Enter de dong. Esc xoa dinh.";
      return;
    } else {
      const map = drawMap || orthoMap;
      const host = document.getElementById("drawHost") || document.getElementById("orthoHost");
      if (!map) throw new Error("Chua co ban do de ve vung");
      startDrawTool("polygon", map, host);
    }
    document.getElementById("meta").textContent = "Ve vung: click cac dinh, click lai diem dau de dong polygon";
  } catch (err) {
    document.getElementById("meta").textContent = String((err && err.message) || err);
  }
}

function ensureProgressHost() {
  if (document.getElementById("lasProgress")) return;
  const host = document.getElementById("orthoHost") || document.getElementById("demHost") || document.getElementById("stage");
  if (!host) return;
  const wrap = document.createElement("div");
  wrap.id = "lasProgress";
  wrap.innerHTML =
    '<div id="lasProgressLabel"><span id="lasProgressText">Dang xu ly</span> <span id="lasProgressPct">0%</span></div>' +
    '<div id="lasProgressTrack"><div id="lasProgressBar"></div></div>' +
    '<div id="lasProgressMeta"></div>';
  host.appendChild(wrap);
}

async function optimizeDrawnRegion() {
  const geojson = collectDrawnPolygons();
  if (!geojson.features.length) {
    document.getElementById("meta").textContent = "Hay bam Ve vung va ve mot polygon truoc";
    return;
  }
  const btn = document.getElementById("btnOptimizeData");
  if (btn) btn.disabled = true;
  try {
    ensureProgressHost();
    setLasProgress(18, "Dang cat mesh theo vung ve", (geojson.features.length) + " polygon");
    const clipped = await (await fetch("/api/mesh/clip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ geojson: geojson })
    })).json();
    if (!clipped || clipped.success === false) {
      throw new Error((clipped && (clipped.message || clipped.error)) || "Khong cat duoc mesh");
    }
    meshClipId = clipped.clip_id || "";
    setSaveMeshVisible();
    destroyRegionDrawMap();
    if (current !== "obj") {
      location.hash = "obj";
      return;
    }
    await meshObj3d();
  } catch (err) {
    hideLasProgress();
    document.getElementById("meta").textContent = String((err && err.message) || err);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadShapefileOntoMesh(fileList) {
  const files = (fileList || []).filter(function (f) { return f && f.name; });
  if (!files.length) return;
  const form = new FormData();
  files.forEach(function (file) { form.append("files", file, file.name); });
  const buttons = shpOpenButtons();
  buttons.forEach(function (btn) { btn.disabled = true; });
  try {
    setLasProgress(8, "Dang doc shapefile", files[0].name);
    const imported = await (await fetch("/api/draw/import-shp?view=fast", { method: "POST", body: form })).json();
    if (!imported || imported.success === false) {
      throw new Error((imported && (imported.message || imported.error)) || "Khong doc duoc shapefile");
    }
    setLasProgress(35, "Dang cat mesh theo polygon", "");
    const clipped = await (await fetch("/api/mesh/clip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ geojson: imported.geojson || {} })
    })).json();
    if (!clipped || clipped.success === false) {
      throw new Error((clipped && (clipped.message || clipped.error)) || "Khong cat duoc mesh");
    }
    meshClipId = clipped.clip_id || "";
    setSaveMeshVisible();
    await meshObj3d();
  } catch (err) {
    hideLasProgress();
    document.getElementById("meta").textContent = String((err && err.message) || err);
  } finally {
    buttons.forEach(function (btn) { btn.disabled = false; });
  }
}

async function saveClippedMesh() {
  if (!meshClipId) {
    document.getElementById("meta").textContent = "Hay bam Toi uu du lieu truoc khi luu";
    return;
  }
  const btn = document.getElementById("btnSaveMesh");
  if (btn) btn.disabled = true;
  try {
    ensureProgressHost();
    setLasProgress(20, "Dang luu mesh da cat", "model/model.obj");
    const saved = await (await fetch("/api/mesh/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clip_id: meshClipId })
    })).json();
    if (!saved || saved.success === false) {
      throw new Error((saved && (saved.message || saved.error)) || "Khong luu duoc mesh");
    }
    meshClipId = "";
    setSaveMeshVisible();
    await meshObj3d();
    document.getElementById("meta").textContent =
      "Da luu: model.obj" + (saved.renamed ? " (ban cu doi thanh model-full.obj)" : "");
  } catch (err) {
    hideLasProgress();
    document.getElementById("meta").textContent = String((err && err.message) || err);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function meshObj3d() {
  return meshObj3dLegacy();
}

async function meshTiles3d() {
  const info = await (await api("/api/mesh/tiles.meta.json")).json();
  if (!info || info.success === false || !info.ready) {
    stage('<div class="empty">Chua co 3D Tiles:<br>tiled/tileset.json<br>Bam Run Tiles de tao.</div>');
    document.getElementById("meta").textContent = "thieu tiled/tileset.json";
    return;
  }
  let ortho = {};
  try { ortho = await (await api("/api/view3d/open")).json(); } catch (e) { ortho = {}; }
  return orthoTiles3d({
    meshTiles: true,
    tileset_url: info.tileset_url || "/tiles/tileset.json",
    filename: "tiled/tileset.json",
    crs: "WGS84",
    file_id: (info && info.file_id) || (ortho && ortho.file_id),
    mtime: (info && info.mtime) || (ortho && ortho.mtime),
    bounds_wgs84: (info && info.bounds_wgs84) || (ortho && ortho.bounds_wgs84)
  });
}

async function pointTiles3d() {
  const info = await (await api("/api/points/tiles.meta.json")).json();
  if (!info || info.success === false || !info.ready) return false;
  let ortho = {};
  try { ortho = await (await api("/api/view3d/open")).json(); } catch (e) { ortho = {}; }
  await orthoTiles3d({
    pointTiles: true,
    tileset_url: info.tileset_url || "/tiles-las/tileset.json",
    filename: "tiled-las/tileset.json",
    crs: "WGS84",
    file_id: (info && info.file_id) || (ortho && ortho.file_id),
    mtime: (info && info.mtime) || (ortho && ortho.mtime),
    bounds_wgs84: (info && info.bounds_wgs84) || (ortho && ortho.bounds_wgs84)
  });
  return true;
}

async function meshObj3dLegacy() {
  if (!window.Dem3D || !window.THREE) {
    stage('<div class="empty">Khong tai duoc Three.js de xem mesh OBJ.</div>');
    return;
  }
  stage(`
    <div id="demHost">
      <div id="view3d"></div>
      <div id="lasProgress">
        <div id="lasProgressLabel"><span id="lasProgressText">Dang tai mesh OBJ</span> <span id="lasProgressPct">0%</span></div>
        <div id="lasProgressTrack"><div id="lasProgressBar"></div></div>
        <div id="lasProgressMeta">Doc mesh OBJ; mo *.shp de cat theo polygon</div>
      </div>
      <div id="dem3dControls">
        <label>Do phong cao do
          <input id="demExaggeration" type="range" min="0.005" max="0.3" step="0.001" value="0.05"/>
        </label>
      </div>
    </div>`);
  Dem3D.show3DMode(true);
  const exag = document.getElementById("demExaggeration");
  if (exag) {
    exag.addEventListener("input", () => Dem3D.setExaggeration(Number(exag.value)));
  }
  function meshBinUrl(extra) {
    const q = [];
    if (meshClipId) q.push("clip=" + encodeURIComponent(meshClipId));
    if (extra && extra.lod) q.push("lod=" + extra.lod);
    if (extra && extra.v != null) q.push("v=" + extra.v);
    return "/api/mesh/obj.bin" + (q.length ? "?" + q.join("&") : "");
  }
  try {
    setLasProgress(6, "Dang mo mesh", "");
    let geo = {};
    try { geo = await (await api("/api/mesh/obj.meta.json" + (meshClipId ? ("?clip=" + encodeURIComponent(meshClipId)) : ""))).json(); } catch (e) { geo = {}; }
    if (geo && geo.success === false) {
      throw new Error(geo.error || "Khong doc duoc mesh OBJ");
    }
    if (geo && geo.bounds_wgs84) {
      const b = geo.bounds_wgs84;
      if ([b.west, b.south, b.east, b.north].every(function (v) { return Number.isFinite(Number(v)); })) {
        meshWgsBounds = { west: Number(b.west), south: Number(b.south), east: Number(b.east), north: Number(b.north) };
      }
    }
    const ver = (geo && geo.mtime != null) ? geo.mtime : Date.now();
    async function pollMeshMeta(untilFast) {
      let latest = geo || {};
      for (let i = 0; i < 7200; i++) {
        if (latest && latest.success === false) throw new Error(latest.error || "Khong doc duoc mesh OBJ");
        if (latest && latest.error && !(untilFast ? latest.fast_ready : latest.view_ready)) {
          throw new Error(latest.error);
        }
        if (untilFast && (latest.fast_ready || latest.view_ready)) return latest;
        if (!untilFast && latest.view_ready) return latest;
        const label = latest.message || (untilFast ? "Dang bien doi OBJ" : "Dang tao mesh day du");
        setLasProgress(Number(latest.pct) || 5, label, "");
        await new Promise(function (resolve) { setTimeout(resolve, 400); });
        try { latest = await (await api("/api/mesh/obj.meta.json" + (meshClipId ? ("?clip=" + encodeURIComponent(meshClipId)) : ""))).json(); } catch (e) {}
      }
      throw new Error("Het thoi gian cho mesh OBJ");
    }
    geo = await pollMeshMeta(true);
    const faces = Number(geo && geo.faces) || 0;
    const useFast = !meshClipId && (faces > 250000 || geo.building || !geo.view_ready || geo.fast_ready);
    const firstUrl = meshBinUrl({ lod: useFast ? "fast" : "view", v: ver });
    const texFull = (geo && geo.textures && geo.textures.length)
      ? geo.textures
      : "/media/obj-texture.jpg?lod=full&v=" + ver;
    const buf = await fetchBufferProgress(firstUrl, function (pct, got, total) {
      setLasProgress(10 + Math.round(pct * 0.75), "Dang tai mesh xem truoc", total ? fmtMb(got) + " / " + fmtMb(total) : fmtMb(got));
    });
    await Dem3D.loadObjMesh(buf, texFull, geo);
    setSaveMeshVisible();
    if (!useFast) {
      hideLasProgress();
      document.getElementById("meta").textContent = "model/model.obj  " + faces.toLocaleString("vi-VN") + " tam";
      return;
    }
    geo = await pollMeshMeta(false);
    const fullBytes = Number(geo && geo.bytes) || 0;
    const fullFaces = Number(geo && geo.faces) || faces;
    setLasProgress(0, "Dang tai mesh day du", fullBytes ? "0.0 / " + fmtMb(fullBytes) : "");
    const fullBuf = await fetchViewBin(meshBinUrl({ lod: "view", v: geo.mtime || ver }), function (pct, got, total) {
      if (current !== "obj") return;
      setLasProgress(Math.min(88, pct), "Dang tai mesh day du", total ? fmtMb(got) + " / " + fmtMb(total) : fmtMb(got));
    }, fullBytes, "mesh-view:" + (geo.mtime || ver));
    if (current !== "obj") return;
    setLasProgress(90, "Dang ve mesh day du", fmtMb(fullBuf.byteLength));
    await Dem3D.loadObjMesh(fullBuf, texFull, geo, {
      replace: true,
      asyncPlace: true,
      reloadTexture: false,
      onProgress: function (p) {
        if (current !== "obj") return;
        setLasProgress(90 + Math.round(p * 9), "Dang ve mesh day du", fmtMb(fullBuf.byteLength));
      }
    });
    if (current === "obj") {
      hideLasProgress();
      document.getElementById("meta").textContent = "model/model.obj  " + fullFaces.toLocaleString("vi-VN") + " tam";
    }
  } catch (err) {
    hideLasProgress();
    stage('<div class="empty">Khong xem duoc mesh OBJ.<br>' + String((err && err.message) || err) + "</div>");
    document.getElementById("meta").textContent = String((err && err.message) || err);
  }
}

async function points3d(kind) {
  if (kind === "las") {
    try {
      if (await pointTiles3d()) return;
    } catch (e) {}
  }
  if (!window.Dem3D || !window.THREE) {
    stage('<div class="empty">Khong tai duoc Three.js de xem diem WGS84.</div>');
    return;
  }
  stage(`
    <div id="demHost">
      <div id="view3d"></div>
      <div id="lasProgress">
        <div id="lasProgressLabel"><span id="lasProgressText">Dang tai may diem</span> <span id="lasProgressPct">0%</span></div>
        <div id="lasProgressTrack"><div id="lasProgressBar"></div></div>
        <div id="lasProgressMeta">0 MB</div>
      </div>
      <div id="dem3dControls">
        <label>Do phong cao do
          <input id="demExaggeration" type="range" min="0.005" max="0.3" step="0.001" value="0.05"/>
        </label>
      </div>
      ${kind === "las" ? lasPointControlsHtml() : ""}
    </div>`);
  Dem3D.show3DMode(true);
  const exag = document.getElementById("demExaggeration");
  if (exag) {
    exag.addEventListener("input", () => Dem3D.setExaggeration(Number(exag.value)));
  }
  if (kind === "las") bindLasPointControls();
  let data;
  if (kind === "las") {
    setLasProgress(6, "Dang mo may diem", "");
    let meta = {};
    try { meta = await (await api("/api/points/las.meta.json")).json(); } catch (e) { meta = {}; }
    const totalPts = Number(meta.total) || 0;
    const ver = (meta.mtime != null) ? meta.mtime : Date.now();
    const useFast = totalPts > 250000;
    const firstUrl = "/api/points/las.bin?v=" + ver + (useFast ? "&lod=fast" : "");
    const buf = await fetchBufferProgress(firstUrl, function (pct, got, total) {
      setLasProgress(10 + Math.round(pct * 0.75), "Dang tai may diem", total ? fmtMb(got) + " / " + fmtMb(total) : fmtMb(got));
    });
    data = Dem3D.parseLasBinary(buf);
    if (meta.frame) data.frame = meta.frame;
    if (meta.bounds_wgs84) data.bounds_wgs84 = meta.bounds_wgs84;
    await Dem3D.loadPointsWgs84(data);
    applyLasPointStyle();
    mountDrawOverlay(document.getElementById("demHost"));
    if (!useFast) {
      hideLasProgress();
      document.getElementById("meta").textContent = "";
      return;
    }
    const fullBytes = Number(meta && meta.bytes) || 0;
    setLasProgress(0, "Dang tai may diem day du", fullBytes ? "0.0 / " + fmtMb(fullBytes) : "");
    try {
      const fullBuf = await fetchViewBin("/api/points/las.bin?lod=view&v=" + ver, function (pct, got, total) {
        if (current !== "las") return;
        setLasProgress(Math.min(88, pct), "Dang tai may diem day du", total ? fmtMb(got) + " / " + fmtMb(total) : fmtMb(got));
      }, fullBytes, "las-view:" + ver);
      if (current !== "las") return;
      setLasProgress(92, "Dang ve may diem day du", fmtMb(fullBuf.byteLength));
      const full = Dem3D.parseLasBinary(fullBuf);
      if (meta.frame) full.frame = meta.frame;
      if (meta.bounds_wgs84) full.bounds_wgs84 = meta.bounds_wgs84;
      setLasProgress(96, "Dang ve may diem day du", fmtMb(fullBuf.byteLength));
      await Dem3D.loadPointsWgs84(full, { replace: true });
      applyLasPointStyle();
    } catch (e) {}
    if (current === "las") {
      hideLasProgress();
      document.getElementById("meta").textContent = "";
    }
    return;
  }
  setLasProgress(30, "Dang doc file", "");
  data = await (await api("/api/points/" + kind)).json();
  await Dem3D.loadPointsWgs84(data);
  hideLasProgress();
  document.getElementById("meta").textContent = "";
  mountDrawOverlay(document.getElementById("demHost"));
}

let runPollTimer = null;
let runKind = "";

function setRunButtonsBusy(busy) {
  const dtw = document.getElementById("btnRunDtw");
  const tiles = document.getElementById("btnRunTiles");
  if (dtw) dtw.disabled = !!busy;
  if (tiles) tiles.disabled = !!busy;
}

function openRunModal(title) {
  const modal = document.getElementById("runModal");
  const logEl = document.getElementById("runModalLog");
  const bar = document.getElementById("runModalBar");
  document.getElementById("runModalTitle").textContent = title;
  document.getElementById("runModalStatus").textContent = "Dang khoi dong...";
  if (logEl) logEl.textContent = "";
  if (bar) bar.style.width = "0%";
  if (modal) modal.hidden = false;
}

function closeRunModal() {
  const modal = document.getElementById("runModal");
  if (modal) modal.hidden = true;
}

function renderJobStatus(data) {
  const logEl = document.getElementById("runModalLog");
  const bar = document.getElementById("runModalBar");
  const st = document.getElementById("runModalStatus");
  const lines = (data.lines || []).join("\n");
  if (logEl && logEl.textContent !== lines) {
    logEl.textContent = lines;
    logEl.scrollTop = logEl.scrollHeight;
  }
  const pct = Number(data.pct);
  if (bar && Number.isFinite(pct)) bar.style.width = Math.max(0, Math.min(100, pct)) + "%";
  if (data.running) {
    st.textContent = Number.isFinite(pct) ? ("Dang chay  " + pct.toFixed(1) + "%") : "Dang chay...";
  } else if (data.exit_code === 0) {
    if (bar) bar.style.width = "100%";
    st.textContent = "Hoan tat";
  } else if (data.exit_code == null) {
    st.textContent = data.error || "Chua chay";
  } else {
    st.textContent = "Loi  (exit " + data.exit_code + ")";
  }
}

async function pollJob(kind) {
  try {
    const data = await (await api("/api/jobs/" + kind)).json();
    renderJobStatus(data);
    if (data.running) {
      runPollTimer = setTimeout(function () { pollJob(kind); }, 700);
      return;
    }
    setRunButtonsBusy(false);
    runPollTimer = null;
    reloadProducts();
  } catch (e) {
    document.getElementById("runModalStatus").textContent = String(e.message || e);
    setRunButtonsBusy(false);
    runPollTimer = null;
  }
}

function fillPaths(data) {
  const im = document.getElementById("imagesPath");
  const out = document.getElementById("outputPath");
  const hint = document.getElementById("pathHint");
  if (im && data.images != null) {
    im.value = data.images;
    im.classList.toggle("missing", data.images_exists === false);
  }
  if (out && data.output != null) {
    out.value = data.output;
    out.classList.toggle("missing", data.output_exists === false);
  }
  if (hint) {
    const notes = [];
    if (data.error) notes.push(data.error);
    if (data.images_note) notes.push(data.images_note);
    const scanned = data.images_scanned || data.images_list || [];
    if (scanned.length > 1) notes.push("Quet " + scanned.length + " thu muc con");
    hint.textContent = notes.join(" ");
  }
}

let fsTarget = "images";
let fsCurrent = "";
let fsView = "";

function fsMessage(text) {
  const el = document.getElementById("fsMsg");
  if (el) el.textContent = text || "";
}

function closeFsModal() {
  const modal = document.getElementById("fsModal");
  if (modal) modal.hidden = true;
  fsMessage("");
}

async function loadFsList(path) {
  const q = path ? ("?path=" + encodeURIComponent(path)) : "";
  const data = await (await api("/api/fs/list" + q)).json();
  if (data.error) throw new Error(data.error);
  fsView = data.path || "";
  fsCurrent = fsView;
  fsMessage("");
  const pathEl = document.getElementById("fsPath");
  const listEl = document.getElementById("fsList");
  if (pathEl) pathEl.textContent = fsCurrent || "Drives on Windows Server";
  if (!listEl) return;
  const rows = data.items || [];
  if (!rows.length) {
    listEl.innerHTML = '<div class="empty">No subfolders</div>';
    return;
  }
  listEl.innerHTML = rows.map(function (it) {
    return '<button type="button" data-path="' + String(it.path).replace(/"/g, "&quot;") + '">' +
      (it.name || it.path) + "</button>";
  }).join("");
  listEl.querySelectorAll("button").forEach(function (btn) {
    btn.addEventListener("dblclick", function () { loadFsList(btn.getAttribute("data-path")); });
    btn.addEventListener("click", function () {
      fsCurrent = btn.getAttribute("data-path") || "";
      if (pathEl) pathEl.textContent = fsCurrent;
    });
  });
}

async function openFsModal(target) {
  fsTarget = target;
  const modal = document.getElementById("fsModal");
  const title = document.getElementById("fsTitle");
  if (title) title.textContent = target === "output" ? "Select output folder on server" : "Select parent image folder on server";
  if (modal) modal.hidden = false;
  const start = ((document.getElementById(target === "output" ? "outputPath" : "imagesPath") || {}).value || "").trim();
  try {
    await loadFsList(start);
  } catch (e) {
    await loadFsList("");
  }
}

async function createFsFolder() {
  const nameEl = document.getElementById("fsNewName");
  const name = ((nameEl && nameEl.value) || "").trim();
  if (!fsView) {
    fsMessage("Open a drive or parent folder first.");
    return;
  }
  if (!name) {
    fsMessage("Enter a new folder name.");
    return;
  }
  const r = await fetch("/api/fs/mkdir", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: fsView, name: name })
  });
  const data = await r.json();
  if (!r.ok) {
    fsMessage(data.error || ("HTTP " + r.status));
    return;
  }
  if (nameEl) nameEl.value = "";
  await loadFsList(fsView);
  if (data.path) {
    fsCurrent = data.path;
    const pathEl = document.getElementById("fsPath");
    if (pathEl) pathEl.textContent = fsCurrent;
  }
  fsMessage("Created: " + (data.name || name));
}

async function pickFsCurrent() {
  if (!fsCurrent) throw new Error("No server folder selected");
  const id = fsTarget === "output" ? "outputPath" : "imagesPath";
  const el = document.getElementById(id);
  if (el) el.value = fsCurrent;
  closeFsModal();
  await applyPaths(true);
}

async function reloadProducts() {
  const boot = await (await api("/api/products")).json();
  PRODUCTS.splice(0, PRODUCTS.length, ...boot.items);
  await show(current || location.hash.slice(1) || "overview");
}

async function applyPaths(reload) {
  const images = (document.getElementById("imagesPath") || {}).value || "";
  const output = (document.getElementById("outputPath") || {}).value || "";
  const r = await fetch("/api/paths", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ images: images.trim(), output: output.trim() })
  });
  const data = await r.json();
  fillPaths(data);
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  meshClipId = "";
  setSaveMeshVisible();
  if (reload) await reloadProducts();
  return data;
}

async function startJob(kind, title) {
  if (runPollTimer) {
    clearTimeout(runPollTimer);
    runPollTimer = null;
  }
  runKind = kind;
  openRunModal(title);
  setRunButtonsBusy(true);
  try {
    await applyPaths(false);
    const mesh3d = document.getElementById("dtwMesh3d");
    const tex16k = document.getElementById("dtwTex16k");
    const optVal = function (id) {
      const el = document.getElementById(id);
      return el ? el.value : "";
    };
    const payload = kind === "dtw" ? {
      compute_mode: document.getElementById("dtwComputeMode").value,
      mesh_3d: !!(mesh3d && mesh3d.checked),
      texture_size: (tex16k && tex16k.checked) ? 16384 : 8192,
      quality: optVal("dtwQuality"),
      depth_quality: optVal("dtwDepthQuality"),
      cloud_min_conf: optVal("dtwCloudMinConf"),
      face_count: optVal("dtwFaceCount")
    } : (kind === "tiles" && current === "las") ? { points: true } : {};
    const r = await fetch("/api/jobs/" + kind, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await r.json();
    if (!r.ok) {
      renderJobStatus(data);
      document.getElementById("runModalStatus").textContent = data.error || ("HTTP " + r.status);
      if (!data.running) setRunButtonsBusy(false);
      else pollJob(kind);
      return;
    }
    renderJobStatus(data);
    pollJob(kind);
  } catch (e) {
    document.getElementById("runModalStatus").textContent = String(e.message || e);
    setRunButtonsBusy(false);
  }
}

window.addEventListener("hashchange", () => show(location.hash.slice(1) || "overview"));
(async () => {
  const folderEl = document.getElementById("folder");
  if (folderEl) folderEl.remove();
  try {
    fillPaths(await (await api("/api/paths")).json());
  } catch (e) {}
  const boot = await (await api("/api/products")).json();
  PRODUCTS.splice(0, PRODUCTS.length, ...boot.items);
  const btnDtw = document.getElementById("btnRunDtw");
  const btnTiles = document.getElementById("btnRunTiles");
  const btnClose = document.getElementById("runModalClose");
  const btnPaths = document.getElementById("pathApply");
  const btnOpenShp = document.getElementById("dtwOpenShp");
  const shpInput = document.getElementById("dtwShpInput");
  const btnDrawRegion = document.getElementById("btnDrawRegion");
  const btnOptimizeData = document.getElementById("btnOptimizeData");
  if (btnDrawRegion) btnDrawRegion.addEventListener("click", function (ev) {
    ev.preventDefault();
    startDrawRegion();
  });
  if (btnOptimizeData) btnOptimizeData.addEventListener("click", function (ev) {
    ev.preventDefault();
    optimizeDrawnRegion();
  });
  const btnSaveMesh = document.getElementById("btnSaveMesh");
  if (btnSaveMesh) btnSaveMesh.addEventListener("click", function (ev) {
    ev.preventDefault();
    saveClippedMesh();
  });
  if (btnOpenShp) btnOpenShp.addEventListener("click", function (ev) {
    ev.preventDefault();
    openShpFilePicker();
  });
  if (shpInput) shpInput.addEventListener("change", function () {
    const files = shpInput.files ? Array.prototype.slice.call(shpInput.files) : [];
    if (!files.length) return;
    if (current === "obj") loadShapefileOntoMesh(files);
    else loadShapefileOntoMap(files);
  });
  if (btnDtw) btnDtw.addEventListener("click", function () { startJob("dtw", "Run DTW"); });
  if (btnTiles) btnTiles.addEventListener("click", function () { startJob("tiles", "Run Tiles"); });
  if (btnClose) btnClose.addEventListener("click", closeRunModal);
  if (btnPaths) btnPaths.addEventListener("click", function () {
    applyPaths(true).catch(function (err) {
      const hint = document.getElementById("pathHint");
      if (hint) hint.textContent = String(err.message || err);
    });
  });
  const btnBrowseIm = document.getElementById("browseImages");
  const btnBrowseOut = document.getElementById("browseOutput");
  const btnFsClose = document.getElementById("fsClose");
  const btnFsUp = document.getElementById("fsUp");
  const btnFsPick = document.getElementById("fsPick");
  const btnFsMkdir = document.getElementById("fsMkdir");
  const fsNewName = document.getElementById("fsNewName");
  if (btnBrowseIm) btnBrowseIm.addEventListener("click", function () { openFsModal("images"); });
  if (btnBrowseOut) btnBrowseOut.addEventListener("click", function () { openFsModal("output"); });
  if (btnFsClose) btnFsClose.addEventListener("click", closeFsModal);
  if (btnFsUp) btnFsUp.addEventListener("click", function () {
    api("/api/fs/list?path=" + encodeURIComponent(fsCurrent || "")).then(function (r) { return r.json(); }).then(function (data) {
      return loadFsList(data.parent || "");
    }).catch(function () { loadFsList(""); });
  });
  if (btnFsMkdir) btnFsMkdir.addEventListener("click", function () {
    createFsFolder().catch(function (err) { fsMessage(String(err.message || err)); });
  });
  if (fsNewName) fsNewName.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") {
      ev.preventDefault();
      createFsFolder().catch(function (err) { fsMessage(String(err.message || err)); });
    }
  });
  if (btnFsPick) btnFsPick.addEventListener("click", function () {
    pickFsCurrent().catch(function (err) {
      const hint = document.getElementById("pathHint");
      if (hint) hint.textContent = String(err.message || err);
    });
  });
  ["imagesPath", "outputPath"].forEach(function (id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        applyPaths(true).catch(function (err) {
          const hint = document.getElementById("pathHint");
          if (hint) hint.textContent = String(err.message || err);
        });
      }
    });
  });
  show(location.hash.slice(1) || "overview");
})();
</script>
</body>
</html>
"""


def fmt_size(path: Path) -> str:
    if not path.is_file():
        return "thieu file"
    size = path.stat().st_size
    if size >= 1_048_576:
        return f"{size / 1_048_576:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def safe_file(rel: str) -> Path:
    path = (OUTPUT / rel).resolve()
    if OUTPUT.resolve() not in path.parents and path != OUTPUT.resolve():
        abort(403)
    return path


def downsample_xy(arr, max_side: int = MAX_RASTER):
    import numpy as np

    data = np.asarray(arr)
    h, w = data.shape[:2]
    step = max(1, int(max(h, w) / max_side))
    return data[::step, ::step] if data.ndim == 2 else data[::step, ::step, :]


def _preview_shape(height: int, width: int, max_side: int = MAX_RASTER) -> tuple[int, int]:
    longest = max(height, width)
    if longest <= max_side:
        return height, width
    scale = longest / float(max_side)
    return max(1, int(round(height / scale))), max(1, int(round(width / scale)))


def load_raster(path: Path, max_side: int = MAX_RASTER):
    import numpy as np

    try:
        import rasterio
        from rasterio.enums import Resampling

        with rasterio.open(path) as src:
            out_h, out_w = _preview_shape(src.height, src.width, max_side)
            if src.count >= 3:
                bands = src.read(
                    [1, 2, 3],
                    out_shape=(3, out_h, out_w),
                    resampling=Resampling.bilinear,
                )
                return np.transpose(bands, (1, 2, 0))
            return src.read(1, out_shape=(out_h, out_w), resampling=Resampling.bilinear)
    except Exception:
        pass

    try:
        import tifffile

        data = tifffile.imread(str(path))
        return downsample_xy(data, max_side)
    except Exception:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = None
        with Image.open(path) as img:
            img.thumbnail((max_side, max_side))
            return np.array(img)


def _to_uint8_rgb(show):
    import numpy as np

    if show.dtype == np.uint8:
        return show
    finite = show[np.isfinite(show)] if np.issubdtype(show.dtype, np.floating) else show
    mx = float(np.max(finite)) if finite.size else 0.0
    if mx <= 1.5:
        return np.clip(show * 255.0, 0, 255).astype(np.uint8)
    if mx <= 255:
        return np.clip(show, 0, 255).astype(np.uint8)
    return np.clip(show * (255.0 / mx), 0, 255).astype(np.uint8)


def raster_png(path: Path, cmap: str | None, max_side: int = MAX_RASTER) -> bytes:
    import numpy as np
    from PIL import Image

    cache_dir = OUTPUT / ".preview"
    cache = cache_dir / f"{path.stem}-{max_side}-a.png"
    try:
        if cache.is_file() and cache.stat().st_mtime >= path.stat().st_mtime:
            return cache.read_bytes()
    except OSError:
        pass

    data = load_raster(path, max_side)
    if data.ndim == 3:
        rgb = _to_uint8_rgb(data[..., :3])
        alpha = np.where(np.any(rgb > 2, axis=2), 255, 0).astype(np.uint8)
        img = Image.fromarray(np.dstack([rgb, alpha]), mode="RGBA")
    else:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        finite = np.isfinite(data)
        if np.issubdtype(data.dtype, np.floating) or np.issubdtype(data.dtype, np.integer):
            finite &= data > -10000
        usable = data[finite]
        vmin, vmax = (float(usable.min()), float(usable.max())) if usable.size else (0, 1)
        norm = (data - vmin) / (vmax - vmin + 1e-9)
        colored = plt.get_cmap(cmap or "terrain")(np.clip(norm, 0, 1))
        rgba = (colored * 255).astype(np.uint8)
        rgba[~finite, 3] = 0
        img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(png)
    except OSError:
        pass
    return png


def wgs84_product_bounds() -> dict | None:
    import rasterio
    from rasterio.warp import transform_bounds

    for rel in ("dem/dem.tif", "dem/dem-ortho.tif", "orthomosaic/orthomosaic.tif"):
        path = OUTPUT / rel
        if not path.is_file():
            continue
        try:
            with rasterio.open(path) as src:
                west, south, east, north = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=8)
            if east > west and north > south:
                return {
                    "west": float(west),
                    "south": float(south),
                    "east": float(east),
                    "north": float(north),
                }
        except Exception:
            continue
    cams_path = OUTPUT / "camera_reference.csv"
    if cams_path.is_file():
        cams = load_cameras(cams_path)
        if cams:
            lons = [c["lon"] for c in cams]
            lats = [c["lat"] for c in cams]
            return {
                "west": float(min(lons)),
                "south": float(min(lats)),
                "east": float(max(lons)),
                "north": float(max(lats)),
            }
    return None


def product_geo_frame() -> dict | None:
    """Khung chung: local met (mesh/LAS) -> WGS84 (DEM/OSM)."""
    import math

    wgs = wgs84_product_bounds()
    if not wgs:
        return None
    lat0 = 0.5 * (float(wgs["south"]) + float(wgs["north"]))
    lon0 = 0.5 * (float(wgs["west"]) + float(wgs["east"]))
    m_lon = 111320.0 * math.cos(math.radians(lat0))
    m_lat = 110540.0
    x0 = 0.0
    y0 = 0.0
    las = OUTPUT / "point_cloud" / "point_cloud.las"
    if las.is_file():
        try:
            stats = _las_file_stats(las)
            x0 = 0.5 * (float(stats["xmin"]) + float(stats["xmax"]))
            y0 = 0.5 * (float(stats["ymin"]) + float(stats["ymax"]))
        except Exception:
            x0 = y0 = 0.0
    else:
        preview = OUTPUT / ".preview" / "model-mesh-full.bin"
        if preview.is_file():
            try:
                header = preview.read_bytes()[:40]
                _m, _nv, _ni, _f, xmin, xmax, ymin, ymax, zmin, zmax = struct.unpack("<4sIII6f", header)
                x0 = 0.5 * (float(xmin) + float(xmax))
                y0 = 0.5 * (-float(zmax) + -float(zmin))
            except Exception:
                x0 = y0 = 0.0
    return {
        "bounds_wgs84": {
            "west": float(wgs["west"]),
            "south": float(wgs["south"]),
            "east": float(wgs["east"]),
            "north": float(wgs["north"]),
        },
        "lon0": float(lon0),
        "lat0": float(lat0),
        "x0": float(x0),
        "y0": float(y0),
        "meters_per_lon": float(max(m_lon, 1e-6)),
        "meters_per_lat": float(max(m_lat, 1e-6)),
        "crs": "local-en",
    }


def local_xy_to_wgs84(xs, ys, bbox: dict, src_x=None, src_y=None):
    import numpy as np

    x = np.asarray(xs, dtype=np.float64)
    y = np.asarray(ys, dtype=np.float64)
    if src_x is None:
        xmin, xmax = float(np.min(x)), float(np.max(x))
    else:
        xmin, xmax = float(src_x[0]), float(src_x[1])
    if src_y is None:
        ymin, ymax = float(np.min(y)), float(np.max(y))
    else:
        ymin, ymax = float(src_y[0]), float(src_y[1])
    lon_span = max(bbox["east"] - bbox["west"], 1e-12)
    lat_span = max(bbox["north"] - bbox["south"], 1e-12)
    x_span = max(xmax - xmin, 1e-9)
    y_span = max(ymax - ymin, 1e-9)
    lon = bbox["west"] + (x - xmin) / x_span * lon_span
    lat = bbox["south"] + (y - ymin) / y_span * lat_span
    return lon, lat


def local_xy_to_wgs84_frame(xs, ys, frame: dict | None = None):
    import numpy as np

    fr = frame or product_geo_frame()
    if not fr:
        raise ValueError("Khong co khung WGS84")
    x = np.asarray(xs, dtype=np.float64)
    y = np.asarray(ys, dtype=np.float64)
    lon = float(fr["lon0"]) + (x - float(fr["x0"])) / float(fr["meters_per_lon"])
    lat = float(fr["lat0"]) + (y - float(fr["y0"])) / float(fr["meters_per_lat"])
    return lon, lat


def sample_las(path: Path, limit: int = MAX_POINTS):
    import laspy
    import numpy as np

    limit = max(8_000, min(int(limit), 400_000))
    with laspy.open(str(path)) as reader:
        header = reader.header
        n = int(header.point_count or 0)
        if n <= 0:
            raise ValueError("LAS rong")
        dim_names = set(header.point_format.dimension_names)
        has_rgb = {"red", "green", "blue"}.issubset(dim_names)
        xmin, xmax = float(header.x_min), float(header.x_max)
        ymin, ymax = float(header.y_min), float(header.y_max)
        n_windows = min(n, max(96, min(900, limit // 180)))
        per = max(1, (limit + n_windows - 1) // n_windows)
        burst = max(per * 6, 1024)
        chunks_x: list = []
        chunks_y: list = []
        chunks_z: list = []
        chunks_r: list = []
        chunks_g: list = []
        chunks_b: list = []
        taken = 0
        used_seek = True
        try:
            for i in range(n_windows):
                if taken >= limit:
                    break
                start = int(i * n / n_windows)
                remain = n - start
                if remain <= 0:
                    break
                reader.seek(start)
                pts = reader.read_points(min(burst, remain))
                count = int(len(pts))
                if count <= 0:
                    continue
                want = min(per, count, limit - taken)
                sel = np.linspace(0, count - 1, want, dtype=np.int64)
                chunks_x.append(np.asarray(pts.x, dtype=np.float64)[sel])
                chunks_y.append(np.asarray(pts.y, dtype=np.float64)[sel])
                chunks_z.append(np.asarray(pts.z, dtype=np.float64)[sel])
                if has_rgb:
                    chunks_r.append(np.asarray(pts.red, dtype=np.float64)[sel])
                    chunks_g.append(np.asarray(pts.green, dtype=np.float64)[sel])
                    chunks_b.append(np.asarray(pts.blue, dtype=np.float64)[sel])
                taken += want
        except Exception:
            used_seek = False

        if not used_seek or not chunks_x:
            chunks_x.clear()
            chunks_y.clear()
            chunks_z.clear()
            chunks_r.clear()
            chunks_g.clear()
            chunks_b.clear()
            step = max(1, n // limit)
            taken = 0
            offset = 0
            reader.seek(0)
            for pts in reader.chunk_iterator(250_000):
                count = int(len(pts))
                if count <= 0:
                    continue
                keep = ((np.arange(count) + offset) % step) == 0
                if np.any(keep):
                    chunks_x.append(np.asarray(pts.x, dtype=np.float64)[keep])
                    chunks_y.append(np.asarray(pts.y, dtype=np.float64)[keep])
                    chunks_z.append(np.asarray(pts.z, dtype=np.float64)[keep])
                    if has_rgb:
                        chunks_r.append(np.asarray(pts.red, dtype=np.float64)[keep])
                        chunks_g.append(np.asarray(pts.green, dtype=np.float64)[keep])
                        chunks_b.append(np.asarray(pts.blue, dtype=np.float64)[keep])
                    taken += int(np.count_nonzero(keep))
                    if taken >= limit:
                        break
                offset += count

    if not chunks_x:
        raise ValueError("LAS rong")
    xs = np.concatenate(chunks_x)[:limit]
    ys = np.concatenate(chunks_y)[:limit]
    zs = np.concatenate(chunks_z)[:limit]
    rgb_flat = None
    if chunks_r:
        rgb = np.column_stack(
            (
                np.concatenate(chunks_r)[:limit],
                np.concatenate(chunks_g)[:limit],
                np.concatenate(chunks_b)[:limit],
            )
        )
        maxv = float(np.nanmax(rgb)) if rgb.size else 0.0
        scale = 255.0 / 65535.0 if maxv > 255 else 1.0
        rgb_flat = np.clip(np.round(rgb * scale), 0, 255).astype(np.uint8).ravel().tolist()
    bbox = wgs84_product_bounds()
    if bbox is None:
        raise ValueError("Khong xac dinh duoc bbox WGS84 (can DEM hoac orthomosaic)")
    try:
        lon, lat = local_xy_to_wgs84_frame(xs, ys)
    except Exception:
        lon, lat = local_xy_to_wgs84(xs, ys, bbox, src_x=(xmin, xmax), src_y=(ymin, ymax))
    return {
        "lon": np.round(lon, 7).tolist(),
        "lat": np.round(lat, 7).tolist(),
        "z": np.round(zs, 3).tolist(),
        "rgb": rgb_flat,
        "total": n,
        "shown": int(xs.size),
        "crs": "EPSG:4326",
        "bounds_wgs84": bbox,
        "zmin": float(np.min(zs)),
        "zmax": float(np.max(zs)),
    }


LAS_BIN_HEADER = struct.Struct("<4sIII6d4d")
_LAS_FAST_LOCK = threading.Lock()
_LAS_MED_LOCK = threading.Lock()
_LAS_VIEW_LOCK = threading.Lock()
_LAS_FULL_LOCK = threading.Lock()
_GZIP_LOCK = threading.Lock()


def _las_wgs_bbox() -> dict:
    bbox = wgs84_product_bounds()
    if bbox is None:
        raise ValueError("Khong xac dinh duoc bbox WGS84 (can DEM/ortho hoac camera GPS)")
    return bbox


def _pack_las_dtp1_header(n: int, flags: int, stats: dict, bbox: dict) -> bytes:
    return LAS_BIN_HEADER.pack(
        b"DTP1",
        int(n),
        int(flags),
        0,
        float(stats["xmin"]),
        float(stats["xmax"]),
        float(stats["ymin"]),
        float(stats["ymax"]),
        float(stats["zmin"]),
        float(stats["zmax"]),
        float(bbox["west"]),
        float(bbox["south"]),
        float(bbox["east"]),
        float(bbox["north"]),
    )


def _write_las_dtp1(path: Path, xyz, rgb, stats: dict, bbox: dict) -> None:
    import numpy as np

    xyz = np.ascontiguousarray(xyz, dtype=np.float32)
    n = int(xyz.shape[0])
    flags = 1 if rgb is not None else 0
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(_pack_las_dtp1_header(n, flags, stats, bbox))
        handle.write(xyz.tobytes())
        if rgb is not None:
            handle.write(np.ascontiguousarray(rgb, dtype=np.uint8).ravel().tobytes())
    tmp.replace(path)


def _las_preview_paths() -> tuple[Path, Path]:
    preview = _obj_preview_dir()
    return preview / "points-las-full.bin", preview / "points-las-fast.bin"


def _las_medium_path() -> Path:
    return _obj_preview_dir() / "points-las-medium.bin"


def _las_view_path() -> Path:
    return _obj_preview_dir() / "points-las-view.bin"


def ensure_gzip_bin(path: Path) -> Path | None:
    """Nen .gz neu nho hon file goc. File lon (localhost) de nguyen vi SSD nhanh hon gzip+JS inflate."""
    try:
        src_size = path.stat().st_size
    except OSError:
        return None
    if src_size > GZIP_BIN_MAX:
        return None
    dest = Path(str(path) + ".gz")
    with _GZIP_LOCK:
        if dest.is_file() and dest.stat().st_mtime >= path.stat().st_mtime and dest.stat().st_size > 32:
            if dest.stat().st_size < path.stat().st_size:
                return dest
            return None
        import gzip

        tmp = Path(str(dest) + ".tmp")
        with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=4) as out:
            shutil.copyfileobj(src, out, length=8 * 1024 * 1024)
        tmp.replace(dest)
        if dest.stat().st_size < path.stat().st_size:
            return dest
        return None


def send_cached_bin(path: Path, download_name: str):
    gz = ensure_gzip_bin(path)
    use = gz if gz is not None else path
    resp = send_file(
        use,
        mimetype="application/octet-stream",
        download_name=download_name,
        as_attachment=False,
        max_age=86400,
    )
    try:
        st = use.stat()
        src = path.stat()
        resp.headers["ETag"] = f'"{int(src.st_mtime)}-{src.st_size}-{st.st_size}"'
        resp.headers["Cache-Control"] = "public, max-age=86400"
        resp.headers["Content-Length"] = str(st.st_size)
        if gz is not None:
            resp.headers["X-Bin-Encoding"] = "gzip"
            resp.headers["Access-Control-Expose-Headers"] = "X-Bin-Encoding, Content-Length"
    except OSError:
        pass
    return resp


def packed_bin_bytes(path: Path) -> int:
    gz = Path(str(path) + ".gz")
    if gz.is_file() and gz.stat().st_size > 32 and gz.stat().st_size < path.stat().st_size:
        return int(gz.stat().st_size)
    if path.is_file():
        return int(path.stat().st_size)
    return 0


def _las_file_stats(path: Path) -> dict:
    import laspy

    with laspy.open(str(path)) as reader:
        header = reader.header
        n = int(header.point_count or 0)
        if n <= 0:
            raise ValueError("LAS rong")
        return {
            "n": n,
            "has_rgb": {"red", "green", "blue"}.issubset(set(header.point_format.dimension_names)),
            "xmin": float(header.x_min),
            "xmax": float(header.x_max),
            "ymin": float(header.y_min),
            "ymax": float(header.y_max),
            "zmin": float(header.z_min),
            "zmax": float(header.z_max),
        }


def _pack_las_rgb(pts: Any, scale16: bool | None) -> tuple[Any, bool]:
    import numpy as np

    rgb = np.column_stack(
        (
            np.asarray(pts.red),
            np.asarray(pts.green),
            np.asarray(pts.blue),
        )
    )
    if scale16 is None:
        scale16 = bool(rgb.size) and int(np.max(rgb)) > 255
    if scale16:
        packed = np.right_shift(rgb.astype(np.uint16, copy=False), 8).astype(np.uint8)
    else:
        packed = rgb.astype(np.uint8, copy=False)
    return packed, bool(scale16)


def build_las_fast_bin(path: Path) -> Path:
    import laspy
    import numpy as np

    _full, dest = _las_preview_paths()
    if dest.is_file() and dest.stat().st_mtime >= path.stat().st_mtime and dest.stat().st_size > 64:
        return dest
    stats = _las_file_stats(path)
    bbox = _las_wgs_bbox()
    n = stats["n"]
    has_rgb = stats["has_rgb"]
    limit = min(LAS_FAST_POINT_TARGET, n)
    n_windows = min(n, max(96, min(900, limit // 180)))
    per = max(1, (limit + n_windows - 1) // n_windows)
    burst = max(per * 6, 1024)
    chunks_xyz: list = []
    chunks_rgb: list = []
    taken = 0
    scale16 = None
    with laspy.open(str(path)) as reader:
        try:
            for i in range(n_windows):
                if taken >= limit:
                    break
                start = int(i * n / n_windows)
                remain = n - start
                if remain <= 0:
                    break
                reader.seek(start)
                pts = reader.read_points(min(burst, remain))
                count = int(len(pts))
                if count <= 0:
                    continue
                want = min(per, count, limit - taken)
                sel = np.linspace(0, count - 1, want, dtype=np.int64)
                xyz = np.empty((want, 3), dtype=np.float32)
                xyz[:, 0] = np.asarray(pts.x, dtype=np.float32)[sel]
                xyz[:, 1] = np.asarray(pts.y, dtype=np.float32)[sel]
                xyz[:, 2] = np.asarray(pts.z, dtype=np.float32)[sel]
                chunks_xyz.append(xyz)
                if has_rgb:
                    packed, scale16 = _pack_las_rgb(pts, scale16)
                    chunks_rgb.append(packed[sel])
                taken += want
        except Exception:
            chunks_xyz.clear()
            chunks_rgb.clear()
            taken = 0
            step = max(1, n // limit)
            offset = 0
            reader.seek(0)
            for pts in reader.chunk_iterator(250_000):
                count = int(len(pts))
                if count <= 0:
                    continue
                keep = ((np.arange(count) + offset) % step) == 0
                if np.any(keep):
                    xyz = np.empty((int(np.count_nonzero(keep)), 3), dtype=np.float32)
                    xyz[:, 0] = np.asarray(pts.x, dtype=np.float32)[keep]
                    xyz[:, 1] = np.asarray(pts.y, dtype=np.float32)[keep]
                    xyz[:, 2] = np.asarray(pts.z, dtype=np.float32)[keep]
                    chunks_xyz.append(xyz)
                    if has_rgb:
                        packed, scale16 = _pack_las_rgb(pts, scale16)
                        chunks_rgb.append(packed[keep])
                    taken += int(xyz.shape[0])
                    if taken >= limit:
                        break
                offset += count
    if not chunks_xyz:
        raise ValueError("LAS rong")
    xyz = np.concatenate(chunks_xyz, axis=0)[:limit]
    rgb = np.concatenate(chunks_rgb, axis=0)[:limit] if chunks_rgb else None
    _write_las_dtp1(dest, xyz, rgb, stats, bbox)
    return dest


def build_las_medium_bin(path: Path) -> Path:
    import numpy as np

    dest = _las_medium_path()
    full = build_las_full_bin(path)
    if dest.is_file() and dest.stat().st_mtime >= full.stat().st_mtime and dest.stat().st_size > 64:
        return dest
    header = full.read_bytes()[: LAS_BIN_HEADER.size]
    magic, n, flags, _pad, xmin, xmax, ymin, ymax, zmin, zmax, west, south, east, north = LAS_BIN_HEADER.unpack(header)
    if magic != b"DTP1" or int(n) <= 0:
        raise ValueError("LAS binary khong hop le")
    n = int(n)
    flags = int(flags)
    limit = min(LAS_MED_POINT_TARGET, n)
    step = max(1, n // limit)
    xyz = np.memmap(full, dtype=np.float32, mode="r", offset=LAS_BIN_HEADER.size, shape=(n, 3))
    xyz_s = np.ascontiguousarray(xyz[::step][:limit])
    rgb_s = None
    if flags & 1:
        rgb = np.memmap(full, dtype=np.uint8, mode="r", offset=LAS_BIN_HEADER.size + n * 12, shape=(n, 3))
        rgb_s = np.ascontiguousarray(rgb[::step][:limit])
    stats = {
        "xmin": float(xmin),
        "xmax": float(xmax),
        "ymin": float(ymin),
        "ymax": float(ymax),
        "zmin": float(zmin),
        "zmax": float(zmax),
    }
    bbox = {"west": float(west), "south": float(south), "east": float(east), "north": float(north)}
    _write_las_dtp1(dest, xyz_s, rgb_s, stats, bbox)
    return dest


def build_las_view_bin(path: Path) -> Path:
    """Tat ca diem, xyz uint16 theo bbox — cung du diem, nhe hon float32."""
    import numpy as np

    dest = _las_view_path()
    full = build_las_full_bin(path)
    if dest.is_file() and dest.stat().st_mtime >= full.stat().st_mtime and dest.stat().st_size > 64:
        return dest
    header = full.read_bytes()[: LAS_BIN_HEADER.size]
    magic, n, flags, _pad, xmin, xmax, ymin, ymax, zmin, zmax, west, south, east, north = LAS_BIN_HEADER.unpack(header)
    if magic != b"DTP1" or int(n) <= 0:
        raise ValueError("LAS binary khong hop le")
    n = int(n)
    flags = int(flags)
    mins = np.array([xmin, ymin, zmin], dtype=np.float64)
    span = np.maximum(np.array([xmax, ymax, zmax], dtype=np.float64) - mins, 1e-6)
    xyz_mm = np.memmap(full, dtype=np.float32, mode="r", offset=LAS_BIN_HEADER.size, shape=(n, 3))
    q_tmp = dest.with_suffix(".q.tmp")
    chunk = 800_000
    try:
        with q_tmp.open("wb") as fq:
            for i in range(0, n, chunk):
                sl = np.asarray(xyz_mm[i : i + chunk], dtype=np.float64)
                q = np.clip(np.rint((sl - mins) / span * 65535.0), 0, 65535).astype(np.uint16)
                fq.write(q.tobytes())
        tmp = dest.with_suffix(".bin.tmp")
        packed = LAS_BIN_HEADER.pack(
            b"DTP2",
            n,
            flags,
            0,
            float(xmin),
            float(xmax),
            float(ymin),
            float(ymax),
            float(zmin),
            float(zmax),
            float(west),
            float(south),
            float(east),
            float(north),
        )
        with tmp.open("wb") as handle:
            handle.write(packed)
            with q_tmp.open("rb") as fq:
                shutil.copyfileobj(fq, handle, length=8 * 1024 * 1024)
            if flags & 1:
                rgb_off = LAS_BIN_HEADER.size + n * 12
                with full.open("rb") as src:
                    src.seek(rgb_off)
                    shutil.copyfileobj(src, handle, length=8 * 1024 * 1024)
        tmp.replace(dest)
    finally:
        try:
            q_tmp.unlink()
        except OSError:
            pass
    return dest


def build_las_full_bin(path: Path) -> Path:
    import laspy
    import numpy as np

    dest, _fast = _las_preview_paths()
    if dest.is_file() and dest.stat().st_mtime >= path.stat().st_mtime and dest.stat().st_size > 64:
        return dest
    stats = _las_file_stats(path)
    bbox = _las_wgs_bbox()
    n = stats["n"]
    has_rgb = stats["has_rgb"]
    flags = 1 if has_rgb else 0
    xyz_tmp = dest.with_suffix(".xyz.tmp")
    rgb_tmp = dest.with_suffix(".rgb.tmp")
    written = 0
    scale16 = None
    try:
        with xyz_tmp.open("wb") as fx, rgb_tmp.open("wb") as fr, laspy.open(str(path)) as reader:
            for pts in reader.chunk_iterator(400_000):
                k = int(len(pts))
                if k <= 0:
                    continue
                xyz = np.empty((k, 3), dtype=np.float32)
                xyz[:, 0] = np.asarray(pts.x, dtype=np.float32)
                xyz[:, 1] = np.asarray(pts.y, dtype=np.float32)
                xyz[:, 2] = np.asarray(pts.z, dtype=np.float32)
                fx.write(xyz.tobytes())
                if has_rgb:
                    packed, scale16 = _pack_las_rgb(pts, scale16)
                    fr.write(np.ascontiguousarray(packed).tobytes())
                written += k
        tmp = dest.with_suffix(".bin.tmp")
        with tmp.open("wb") as handle:
            handle.write(_pack_las_dtp1_header(written, flags, stats, bbox))
            with xyz_tmp.open("rb") as fx:
                shutil.copyfileobj(fx, handle, length=8 * 1024 * 1024)
            if has_rgb:
                with rgb_tmp.open("rb") as fr:
                    shutil.copyfileobj(fr, handle, length=8 * 1024 * 1024)
        tmp.replace(dest)
    finally:
        for stale in (xyz_tmp, rgb_tmp):
            try:
                stale.unlink()
            except OSError:
                pass
    return dest


def stream_las_binary(path: Path, lod: str = ""):
    if lod == "fast":
        lock = _LAS_FAST_LOCK
        builder = build_las_fast_bin
    elif lod in ("medium", "med"):
        lock = _LAS_MED_LOCK
        builder = build_las_medium_bin
    elif lod in ("view", "pack"):
        lock = _LAS_VIEW_LOCK
        builder = build_las_view_bin
    else:
        lock = _LAS_FULL_LOCK
        builder = build_las_full_bin
    with lock:
        bin_path = builder(path)
    if lod == "fast":
        def _warm_las_view(src=path):
            try:
                with _LAS_VIEW_LOCK:
                    ensure_gzip_bin(build_las_view_bin(src))
            except Exception:
                pass
        threading.Thread(target=_warm_las_view, daemon=True).start()
    return send_cached_bin(bin_path, "points-las.bin")


def sample_obj_vertices(path: Path, limit: int = MAX_POINTS):
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    colors: list[list[float]] = []
    total = 0
    keep_every = max(1, path.stat().st_size // 1_048_576 // 8)
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if not line.startswith("v "):
                continue
            total += 1
            if total % keep_every:
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            xs.append(round(float(parts[1]), 3))
            ys.append(round(float(parts[2]), 3))
            zs.append(round(float(parts[3]), 3))
            if len(parts) >= 7:
                colors.append([float(parts[4]), float(parts[5]), float(parts[6])])
            if len(xs) >= limit:
                break
    if not xs:
        raise ValueError("OBJ khong co dinh v")
    bbox = wgs84_product_bounds()
    if bbox is None:
        raise ValueError("Khong xac dinh duoc bbox WGS84 (can DEM/ortho hoac camera GPS)")
    import numpy as np

    try:
        lon, lat = local_xy_to_wgs84_frame(xs, ys)
    except Exception:
        lon, lat = local_xy_to_wgs84(xs, ys, bbox)
    z_arr = np.asarray(zs, dtype=np.float64)
    return {
        "lon": [round(float(v), 7) for v in lon],
        "lat": [round(float(v), 7) for v in lat],
        "z": zs,
        "x": [round(float(v), 7) for v in lon],
        "y": [round(float(v), 7) for v in lat],
        "colors": colors if len(colors) == len(xs) else None,
        "total": total,
        "shown": len(xs),
        "crs": "EPSG:4326",
        "bounds_wgs84": bbox,
        "zmin": float(np.min(z_arr)),
        "zmax": float(np.max(z_arr)),
    }


def _obj_preview_dir() -> Path:
    path = OUTPUT / ".preview"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atlas_sort_key(path: Path) -> tuple[str, int]:
    stem = path.stem.lower()
    match = re.match(r"^(.*?)(\d+)?$", stem)
    if not match:
        return (stem, 0)
    return (match.group(1), int(match.group(2) or 0))


def _mtl_page_index(name: bytes, page_files: list[Path]) -> int:
    stem = name.decode("ascii", "ignore").strip().lower()
    if not stem or not page_files:
        return 0
    for i, path in enumerate(page_files):
        if path.stem.lower() == stem:
            return i
    return 0


def _obj_texture_page_paths() -> list[Path]:
    """Cac trang texture Metashape: model.tif, model1.tif hoac texture.tif / texture.jpg."""
    pages: list[Path] = []
    seen: set[str] = set()

    def _add(path: Path) -> None:
        if not path.is_file():
            return
        key = path.resolve().as_posix().lower()
        if key in seen:
            return
        seen.add(key)
        pages.append(path)

    model_dir = OUTPUT / "model"
    tex_dir = OUTPUT / "texture"
    if model_dir.is_dir():
        tifs = sorted(model_dir.glob("model*.tif"), key=_atlas_sort_key)
        jpgs = sorted(model_dir.glob("model*.jpg"), key=_atlas_sort_key)
        for path in tifs or jpgs:
            _add(path)
    if not pages and tex_dir.is_dir():
        tifs = sorted(tex_dir.glob("texture*.tif"), key=_atlas_sort_key)
        jpgs = sorted(tex_dir.glob("texture*.jpg"), key=_atlas_sort_key)
        for path in tifs or jpgs:
            _add(path)
    if not pages:
        png = OUTPUT / "mesh3d" / "terrain.png"
        _add(png)
    return pages


def _obj_source_files() -> tuple[Path, Path | None]:
    obj_path = safe_file("model/model.obj")
    pages = _obj_texture_page_paths()
    return obj_path, pages[0] if pages else None


def _write_obj_texture_preview(src: Path, dest: Path, max_side: int = MAX_OBJ_TEXTURE) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    suffix = src.suffix.lower()
    if suffix in {".jpg", ".jpeg"} and dest.resolve() != Path(src).resolve():
        with Image.open(src) as probe:
            if max(probe.size) <= max_side:
                dest.write_bytes(Path(src).read_bytes())
                return
    img = Image.open(src)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")
    longest = max(img.size)
    if longest > max_side:
        scale = max_side / float(longest)
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.Resampling.LANCZOS,
        )
    img.save(dest, format="JPEG", quality=98, optimize=True, subsampling=0)


def _parse_obj_corner_b(token: bytes) -> tuple[int, int]:
    a = token.find(b"/")
    if a < 0:
        return int(token), 0
    vi = int(token[:a] or b"0")
    b = token.find(b"/", a + 1)
    tpart = token[a + 1 : b] if b >= 0 else token[a + 1 :]
    ti = int(tpart) if tpart else 0
    return vi, ti


def _np_grow(arr, n: int):
    if n <= arr.shape[0]:
        return arr
    import numpy as np

    cap = max(n, int(arr.shape[0] * 2))
    new = np.empty((cap,) + arr.shape[1:], dtype=arr.dtype)
    new[: arr.shape[0]] = arr
    return new


def _obj_build_set(pct: float, message: str, **extra) -> None:
    _OBJ_BUILD["pct"] = float(max(0.0, min(99.0, pct)))
    _OBJ_BUILD["message"] = message
    for key, value in extra.items():
        _OBJ_BUILD[key] = value
    print(f"[mesh] {pct:.0f}% {message}", flush=True)


def _obj_bin_magic_ok(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 64:
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(4) in (b"DTM1", OBJ_MESH_MAGIC)
    except OSError:
        return False


def _obj_preview_paths() -> dict[str, Path]:
    preview = _obj_preview_dir()
    full = preview / "model-mesh-full.bin"
    return {
        "full": full,
        "fast": full.with_name(full.stem + "-fast.bin"),
        "medium": full.with_name(full.stem + "-medium.bin"),
        "view": full.with_name(full.stem + "-view.bin"),
        "jpg": preview / "model-mesh-full.jpg",
    }


def _obj_preview_fresh() -> dict:
    obj_path, tex_path = _obj_source_files()
    paths = _obj_preview_paths()
    obj_mtime = obj_path.stat().st_mtime if obj_path.is_file() else 0
    full_ok = _obj_bin_magic_ok(paths["full"]) and paths["full"].stat().st_mtime >= obj_mtime
    fast_ok = _obj_bin_magic_ok(paths["fast"]) and paths["fast"].stat().st_mtime >= obj_mtime
    view_ok = (
        _obj_bin_magic_ok(paths["view"]) and paths["view"].stat().st_mtime >= obj_mtime
    ) or full_ok
    jpg_ok = (not tex_path) or (
        paths["jpg"].is_file()
        and tex_path.is_file()
        and paths["jpg"].stat().st_mtime >= tex_path.stat().st_mtime
    )
    return {
        "obj": obj_path,
        "tex": tex_path,
        "paths": paths,
        "full_ok": full_ok,
        "fast_ok": fast_ok,
        "view_ok": view_ok,
        "jpg_ok": jpg_ok,
        "obj_mtime": obj_mtime,
    }


def _weld_obj_corners(xyz, uv, rgb, corner_v, corner_t, has_uv: bool, keep_color: bool, corner_p=None) -> dict:
    import numpy as np

    n_pages = 1
    pages = None
    use_pages = corner_p is not None and int(np.max(corner_p)) > 0
    if use_pages and corner_p is not None and int(corner_v.max()) < (1 << 28) and int(corner_p.max()) < 8:
        # Goi (v, t, p) vao 1 int64: np.unique tren int64 nhanh hon nhieu so voi structured dtype.
        packed = (
            (corner_v.astype(np.int64, copy=False) << 35)
            | ((corner_t.astype(np.int64, copy=False) & 0xFFFFFFFF) << 3)
            | corner_p.astype(np.int64, copy=False)
        )
        uniq, inverse = np.unique(packed, return_inverse=True)
        del packed
        src_v = (uniq >> 35).astype(np.int32)
        src_t = ((uniq >> 3) & 0xFFFFFFFF).astype(np.int32)
        pages = (uniq & 0x7).astype(np.uint8)
        n_pages = int(pages.max()) + 1
    elif use_pages:
        dt = np.dtype([("v", np.int32), ("t", np.int32), ("p", np.uint8)])
        pairs = np.empty(corner_v.size, dtype=dt)
        pairs["v"] = corner_v
        pairs["t"] = corner_t
        pairs["p"] = corner_p
        uniq, inverse = np.unique(pairs, return_inverse=True)
        src_v = uniq["v"]
        src_t = uniq["t"]
        pages = uniq["p"].astype(np.uint8, copy=False)
        n_pages = int(pages.max()) + 1
    else:
        packed = (corner_v.astype(np.int64, copy=False) << 32) | (corner_t.astype(np.int64, copy=False) & 0xFFFFFFFF)
        uniq, inverse = np.unique(packed, return_inverse=True)
        src_v = (uniq >> 32).astype(np.int32)
        src_t = (uniq & 0xFFFFFFFF).astype(np.int32)
    pos_out = np.empty((uniq.size, 3), dtype=np.float32)
    pos_out[:, 0] = xyz[src_v, 0]
    pos_out[:, 1] = xyz[src_v, 2]
    pos_out[:, 2] = -xyz[src_v, 1]
    flags = 0
    colors = None
    if keep_color and rgb is not None:
        flags |= 1
        colors = rgb[src_v]
    uvs = None
    if has_uv:
        flags |= 2
        uvs = uv[src_t]
    if pages is not None and n_pages > 1:
        flags |= 8
    mins = pos_out.min(axis=0)
    maxs = pos_out.max(axis=0)
    return {
        "flags": flags,
        "pos": pos_out,
        "colors": colors,
        "uvs": uvs,
        "normals": None,
        "pages": pages if (flags & 8) else None,
        "indices": inverse.astype(np.uint32, copy=False),
        "xmin": float(mins[0]),
        "xmax": float(maxs[0]),
        "ymin": float(mins[1]),
        "ymax": float(maxs[1]),
        "zmin": float(mins[2]),
        "zmax": float(maxs[2]),
    }


def _downsample_welded_mesh(mesh: dict, face_target: int) -> dict:
    import numpy as np

    indices = mesh["indices"]
    if indices.size < 3:
        return mesh
    faces = indices.reshape(-1, 3)
    nfaces = int(faces.shape[0])
    step = max(1, (nfaces + face_target - 1) // face_target)
    if step > 1:
        faces = faces[::step]
    kept = faces.ravel()
    used, inverse = np.unique(kept, return_inverse=True)
    flags = int(mesh["flags"]) & 11
    if mesh.get("uvs") is not None and (flags & 2):
        flags &= ~1
    if mesh.get("uvs") is None:
        flags &= ~2
    if mesh.get("colors") is None:
        flags &= ~1
    pages = mesh["pages"][used] if (flags & 8) and mesh.get("pages") is not None else None
    if pages is None:
        flags &= ~8
    return {
        "flags": flags,
        "pos": mesh["pos"][used],
        "colors": mesh["colors"][used] if (flags & 1) and mesh.get("colors") is not None else None,
        "uvs": mesh["uvs"][used] if (flags & 2) and mesh.get("uvs") is not None else None,
        "normals": None,
        "pages": pages,
        "indices": inverse.astype(np.uint32, copy=False),
        "xmin": mesh["xmin"],
        "xmax": mesh["xmax"],
        "ymin": mesh["ymin"],
        "ymax": mesh["ymax"],
        "zmin": mesh["zmin"],
        "zmax": mesh["zmax"],
    }


def _convert_obj_to_preview(st: dict) -> tuple[Path, Path | None]:
    import gc

    import numpy as np

    obj_path = st["obj"]
    tex_path = st["tex"]
    paths = st["paths"]
    bin_path = paths["full"]
    jpg_path = paths["jpg"]
    if not obj_path.is_file():
        raise FileNotFoundError("Khong thay model/model.obj")

    tex_pages = _obj_texture_page_paths()
    if not st["jpg_ok"] and tex_pages:
        _obj_build_set(2, f"Dang tao texture xem truoc ({len(tex_pages)} trang)")
        for i, src in enumerate(tex_pages):
            dest = jpg_path if i == 0 else jpg_path.with_name(f"model-mesh-full-{i + 1}.jpg")
            _write_obj_texture_preview(src, dest)
        _write_obj_texture_preview(tex_pages[0], _obj_preview_dir() / "model-mesh-fast.jpg", MAX_OBJ_TEXTURE_FAST)

    if st["full_ok"]:
        if not st["fast_ok"]:
            ensure_obj_fast_lod(bin_path)
        _OBJ_FAST_EV.set()
        if not st["view_ok"]:
            ensure_obj_view_full(bin_path)
        _OBJ_VIEW_EV.set()
        return bin_path, jpg_path if jpg_path.is_file() else None

    size = obj_path.stat().st_size
    _obj_build_set(3, f"Dang doc OBJ {size / (1024 ** 3):.1f} GB (1 luot)")
    keep_color = tex_path is None
    nv_est = max(1_000_000, min(50_000_000, int(size / 220)))
    nvt_est = max(1_000_000, min(50_000_000, int(size / 220)))
    nf_est = max(1_000_000, min(90_000_000, int(size / 120)))
    xyz = np.empty((nv_est, 3), dtype=np.float32)
    rgb = np.full((nv_est, 3), 200, dtype=np.uint8) if keep_color else None
    uv = np.zeros((nvt_est, 2), dtype=np.float32)
    corner_v = np.empty(nf_est * 3, dtype=np.int32)
    corner_t = np.empty(nf_est * 3, dtype=np.int32)
    corner_p = np.zeros(nf_est * 3, dtype=np.uint8)
    mtl_pages: dict[bytes, int] = {}
    current_page = 0
    n_tex_pages = max(1, len(tex_pages))
    has_color = False
    vi = ti = fi = 0
    last_pct = 0
    t0 = time.time()

    def _bump(pos: int, msg: str) -> None:
        nonlocal last_pct
        pct = 4 + min(70, int(pos * 70 / max(size, 1)))
        if pct >= last_pct + 2:
            last_pct = pct
            _obj_build_set(pct, msg)

    with obj_path.open("rb", buffering=16 * 1024 * 1024) as handle:
        for raw in handle:
            if raw.startswith(b"v "):
                parts = raw.split()
                if vi >= xyz.shape[0]:
                    xyz = _np_grow(xyz, vi + 1)
                    if rgb is not None:
                        rgb = _np_grow(rgb, vi + 1)
                xyz[vi, 0] = float(parts[1])
                xyz[vi, 1] = float(parts[2])
                xyz[vi, 2] = float(parts[3])
                if keep_color and rgb is not None and len(parts) >= 7:
                    has_color = True
                    rgb[vi, 0] = int(max(0, min(255, float(parts[4]) * 255.0)))
                    rgb[vi, 1] = int(max(0, min(255, float(parts[5]) * 255.0)))
                    rgb[vi, 2] = int(max(0, min(255, float(parts[6]) * 255.0)))
                vi += 1
                if (vi & 0x3FFFF) == 0:
                    _bump(handle.tell(), f"Dang doc dinh {vi:,}")
            elif raw.startswith(b"vt "):
                parts = raw.split()
                if ti >= uv.shape[0]:
                    uv = _np_grow(uv, ti + 1)
                uv[ti, 0] = float(parts[1])
                uv[ti, 1] = float(parts[2])
                ti += 1
                if (ti & 0x3FFFF) == 0:
                    _bump(handle.tell(), f"Dang doc UV {ti:,}")
            elif raw.startswith(b"usemtl ") or raw.startswith(b"usemtl\t"):
                name = raw.split(None, 1)[1].strip() if raw.split(None, 1)[1:] else b""
                if name and name.lower() not in (b"solid",):
                    current_page = _mtl_page_index(name, tex_pages)
            elif raw.startswith(b"f "):
                toks = raw.split()[1:]
                if len(toks) < 3:
                    continue
                groups = (
                    [toks[:3]]
                    if len(toks) == 3
                    else [(toks[0], toks[k], toks[k + 1]) for k in range(1, len(toks) - 1)]
                )
                for group in groups:
                    base = fi * 3
                    if base + 3 > corner_v.size:
                        corner_v = _np_grow(corner_v, base + 3)
                        corner_t = _np_grow(corner_t, base + 3)
                        corner_p = _np_grow(corner_p, base + 3)
                    for k, tok in enumerate(group):
                        v_idx, t_idx = _parse_obj_corner_b(tok)
                        corner_v[base + k] = v_idx
                        corner_t[base + k] = t_idx
                        corner_p[base + k] = current_page
                    fi += 1
                if (fi & 0x7FFFF) == 0:
                    _bump(handle.tell(), f"Dang doc tam {fi:,}")
            elif raw.startswith(b"vn "):
                continue

    nv = vi
    nvt = ti
    nf = fi
    if nv < 3 or nf < 1:
        raise ValueError("OBJ khong du dinh/tam giac")
    _OBJ_BUILD["vertices"] = int(nv)
    _OBJ_BUILD["faces"] = int(nf)
    ncorners = nf * 3
    corner_v = corner_v[:ncorners]
    corner_t = corner_t[:ncorners]
    corner_p = corner_p[:ncorners]
    if nv:
        np.clip(corner_v, 1, nv, out=corner_v)
        corner_v -= 1
    if nvt:
        np.clip(corner_t, 1, nvt, out=corner_t)
        corner_t -= 1
    else:
        corner_t.fill(0)
    xyz = xyz[:nv]
    if rgb is not None:
        rgb = rgb[:nv]
    uv = uv[: max(nvt, 1)]
    has_uv = nvt > 0 and tex_path is not None
    elapsed = time.time() - t0
    _obj_build_set(76, f"Da doc {nv:,} dinh, {nf:,} tam ({elapsed:.0f}s). Tao mesh xem truoc")

    step = max(1, (nf + OBJ_FAST_FACE_TARGET - 1) // OBJ_FAST_FACE_TARGET)
    fast_faces = np.arange(0, nf, step, dtype=np.int32)
    fast_v = corner_v.reshape(-1, 3)[fast_faces].ravel()
    fast_t = corner_t.reshape(-1, 3)[fast_faces].ravel()
    fast_p = corner_p.reshape(-1, 3)[fast_faces].ravel()
    use_pages = n_tex_pages > 1 and int(corner_p.max()) > 0
    fast_mesh = _weld_obj_corners(
        xyz, uv, rgb, fast_v, fast_t, has_uv, keep_color and has_color,
        corner_p=fast_p if use_pages else None,
    )
    _write_obj_mesh_binary(paths["fast"], fast_mesh)
    _OBJ_FAST_EV.set()
    _obj_build_set(82, f"Mesh xem truoc san sang ({int(fast_mesh['indices'].size // 3):,} tam). Dang han dinh day du")

    full_mesh = _weld_obj_corners(
        xyz, uv, rgb, corner_v, corner_t, has_uv, keep_color and has_color,
        corner_p=corner_p if use_pages else None,
    )
    del xyz, uv, rgb, corner_v, corner_t, corner_p
    gc.collect()
    _obj_build_set(92, f"Dang ghi mesh day du ({int(full_mesh['indices'].size // 3):,} tam)")
    _write_obj_mesh_binary(bin_path, full_mesh)
    try:
        med = _downsample_welded_mesh(full_mesh, OBJ_MED_FACE_TARGET)
        _write_obj_mesh_binary(paths["medium"], med)
    except Exception:
        pass
    ensure_obj_view_full(bin_path)
    _OBJ_VIEW_EV.set()
    _obj_build_set(100, "Mesh day du san sang")
    _OBJ_BUILD["pct"] = 100.0
    _OBJ_BUILD["vertices"] = int(full_mesh["pos"].shape[0])
    _OBJ_BUILD["faces"] = int(full_mesh["indices"].size // 3)
    return bin_path, jpg_path if jpg_path.is_file() else None


def start_obj_preview_build() -> None:
    st = _obj_preview_fresh()
    out_key = str(OUTPUT)
    if not st["obj"].is_file():
        _OBJ_BUILD["error"] = "Khong thay model/model.obj"
        _OBJ_FAST_EV.set()
        _OBJ_VIEW_EV.set()
        return
    if st["full_ok"] and st["fast_ok"] and _OBJ_BUILD.get("output") in (None, out_key):
        _OBJ_FAST_EV.set()
        _OBJ_VIEW_EV.set()
        _OBJ_BUILD["running"] = False
        _OBJ_BUILD["error"] = None
        _OBJ_BUILD["pct"] = 100.0
        _OBJ_BUILD["message"] = "San sang"
        _OBJ_BUILD["output"] = out_key
        return
    with _OBJ_BUILD_LOCK:
        if _OBJ_BUILD.get("running") and _OBJ_BUILD.get("output") == out_key:
            return
        _OBJ_BUILD["running"] = True
        _OBJ_BUILD["output"] = out_key
        _OBJ_BUILD["error"] = None
        _OBJ_BUILD["pct"] = 1.0
        _OBJ_BUILD["message"] = "Bat dau bien doi OBJ"
        _OBJ_BUILD["gen"] = int(_OBJ_BUILD.get("gen") or 0) + 1
        gen = _OBJ_BUILD["gen"]
        _OBJ_FAST_EV.clear()
        _OBJ_VIEW_EV.clear()

        def _worker():
            try:
                _convert_obj_to_preview(_obj_preview_fresh())
                if _OBJ_BUILD.get("gen") == gen:
                    _OBJ_BUILD["error"] = None
            except Exception as exc:
                if _OBJ_BUILD.get("gen") == gen:
                    _OBJ_BUILD["error"] = f"{type(exc).__name__}: {exc}"
                    print(f"[mesh] Loi: {_OBJ_BUILD['error']}", flush=True)
            finally:
                if _OBJ_BUILD.get("gen") == gen:
                    _OBJ_BUILD["running"] = False
                    _OBJ_FAST_EV.set()
                    _OBJ_VIEW_EV.set()

        threading.Thread(target=_worker, daemon=True, name="obj-preview").start()


def wait_obj_preview_lod(lod: str) -> Path:
    start_obj_preview_build()
    want = (lod or "view").strip().lower()
    if want == "fast":
        _OBJ_FAST_EV.wait()
    else:
        _OBJ_VIEW_EV.wait()
    err = _OBJ_BUILD.get("error")
    st = _obj_preview_fresh()
    if want == "fast":
        if st["fast_ok"]:
            return st["paths"]["fast"]
        if st["full_ok"]:
            return ensure_obj_fast_lod(st["paths"]["full"])
    elif want in ("medium", "med"):
        if st["full_ok"]:
            return ensure_obj_medium_lod(st["paths"]["full"])
    else:
        if st["full_ok"]:
            return ensure_obj_view_full(st["paths"]["full"])
    if err:
        raise RuntimeError(err)
    raise RuntimeError("Chua tao duoc mesh preview")


def obj_preview_public() -> dict:
    st = _obj_preview_fresh()
    start_obj_preview_build()
    return {
        "building": bool(_OBJ_BUILD.get("running")),
        "pct": float(_OBJ_BUILD.get("pct") or 0),
        "message": str(_OBJ_BUILD.get("message") or ""),
        "error": _OBJ_BUILD.get("error"),
        "fast_ready": bool(st["fast_ok"]),
        "view_ready": bool(st["full_ok"] or st["view_ok"]),
        "ready": bool(st["full_ok"]),
        "vertices": int(_OBJ_BUILD.get("vertices") or 0),
        "faces": int(_OBJ_BUILD.get("faces") or 0),
    }


def build_obj_preview(max_faces: int | None = None) -> tuple[Path, Path | None]:
    del max_faces
    start_obj_preview_build()
    _OBJ_VIEW_EV.wait()
    st = _obj_preview_fresh()
    if st["full_ok"]:
        if not st["fast_ok"]:
            ensure_obj_fast_lod(st["paths"]["full"])
        return st["paths"]["full"], st["paths"]["jpg"] if st["paths"]["jpg"].is_file() else None
    err = _OBJ_BUILD.get("error")
    if err:
        raise RuntimeError(err)
    raise RuntimeError("Chua tao duoc mesh preview")


def ensure_obj_lod(src_bin: Path, dest: Path, face_target: int) -> Path:
    """Tao ban mesh xem (bo normal/mau thua) o mat do face_target."""
    import numpy as np

    if dest.is_file() and dest.stat().st_mtime >= src_bin.stat().st_mtime and dest.stat().st_size > 64:
        return dest
    mesh = _read_obj_mesh_binary(src_bin)
    indices = mesh["indices"]
    if indices.size < 3:
        shutil.copy2(src_bin, dest)
        return dest
    faces = indices.reshape(-1, 3)
    nfaces = int(faces.shape[0])
    step = max(1, (nfaces + face_target - 1) // face_target)
    if step > 1:
        faces = faces[::step]
    kept = faces.ravel()
    used, inverse = np.unique(kept, return_inverse=True)
    flags = int(mesh["flags"]) & 11
    if mesh.get("uvs") is not None and (flags & 2):
        flags &= ~1
    if mesh.get("uvs") is None:
        flags &= ~2
    if mesh.get("colors") is None:
        flags &= ~1
    pages = mesh["pages"][used] if (flags & 8) and mesh.get("pages") is not None else None
    if pages is None:
        flags &= ~8
    out = {
        "flags": flags,
        "pos": mesh["pos"][used],
        "colors": mesh["colors"][used] if (flags & 1) and mesh.get("colors") is not None else None,
        "uvs": mesh["uvs"][used] if (flags & 2) and mesh.get("uvs") is not None else None,
        "normals": None,
        "pages": pages,
        "indices": inverse.astype(np.uint32, copy=False),
        "xmin": mesh["xmin"],
        "xmax": mesh["xmax"],
        "ymin": mesh["ymin"],
        "ymax": mesh["ymax"],
        "zmin": mesh["zmin"],
        "zmax": mesh["zmax"],
    }
    _write_obj_mesh_binary(dest, out)
    return dest


def ensure_obj_fast_lod(src_bin: Path) -> Path:
    return ensure_obj_lod(src_bin, src_bin.with_name(src_bin.stem + "-fast.bin"), OBJ_FAST_FACE_TARGET)


def ensure_obj_medium_lod(src_bin: Path) -> Path:
    return ensure_obj_lod(src_bin, src_bin.with_name(src_bin.stem + "-medium.bin"), OBJ_MED_FACE_TARGET)


def ensure_obj_view_full(src_bin: Path) -> Path:
    """Tat ca tam giac/dinh; bo normal va mau neu da co texture."""
    dest = src_bin.with_name(src_bin.stem + "-view.bin")
    if dest.is_file() and dest.stat().st_mtime >= src_bin.stat().st_mtime and dest.stat().st_size > 64:
        return dest
    flags = 0
    try:
        with src_bin.open("rb") as handle:
            header = handle.read(16)
        if len(header) >= 16:
            flags = int(struct.unpack_from("<I", header, 12)[0])
    except OSError:
        flags = 0
    view_ready = (flags & 4) == 0 and ((flags & 1) == 0 or (flags & 2))
    if view_ready:
        try:
            dest.unlink(missing_ok=True)
            os.link(src_bin, dest)
            return dest
        except OSError:
            return src_bin
    mesh = _read_obj_mesh_binary(src_bin)
    flags = int(mesh["flags"]) & 11
    if mesh.get("uvs") is not None and (flags & 2):
        flags &= ~1
    if mesh.get("uvs") is None:
        flags &= ~2
    if mesh.get("colors") is None:
        flags &= ~1
    mesh["flags"] = flags
    mesh["normals"] = None
    if not (flags & 1):
        mesh["colors"] = None
    _write_obj_mesh_binary(dest, mesh)
    return dest


def ensure_obj_texture_pages(max_side: int = MAX_OBJ_TEXTURE) -> list[Path]:
    preview = _obj_preview_dir()
    dests: list[Path] = []
    srcs = _obj_texture_page_paths()
    if not srcs:
        one = _ensure_obj_texture_lod("model-mesh-full.jpg", max_side)
        return [one] if one else []
    for i, src in enumerate(srcs):
        dest = preview / ("model-mesh-full.jpg" if i == 0 else f"model-mesh-full-{i + 1}.jpg")
        if not dest.is_file() or dest.stat().st_mtime < Path(src).stat().st_mtime:
            _write_obj_texture_preview(Path(src), dest, max_side)
        if dest.is_file():
            dests.append(dest)
    return dests


def _ensure_obj_texture_lod(dest_name: str, max_side: int) -> Path | None:
    preview = _obj_preview_dir()
    dest = preview / dest_name
    full = preview / "model-mesh-full.jpg"
    _obj, tex = _obj_source_files()
    src = tex if tex is not None and Path(tex).is_file() else (full if full.is_file() else None)
    if src is None or not Path(src).is_file():
        return dest if dest.is_file() else None
    if dest.is_file() and dest.stat().st_mtime >= Path(src).stat().st_mtime:
        try:
            from PIL import Image

            Image.MAX_IMAGE_PIXELS = None
            with Image.open(dest) as probe:
                if max(probe.size) >= max_side:
                    return dest
        except Exception:
            pass
    _write_obj_texture_preview(Path(src), dest, max_side)
    return dest if dest.is_file() else None


def ensure_obj_fast_texture() -> Path | None:
    return _ensure_obj_texture_lod("model-mesh-fast.jpg", MAX_OBJ_TEXTURE_FAST)


def ensure_obj_medium_texture() -> Path | None:
    return _ensure_obj_texture_lod("model-mesh-medium.jpg", MAX_OBJ_TEXTURE_MED)


def _read_obj_mesh_binary(path: Path) -> dict:
    import numpy as np

    raw = path.read_bytes()
    if len(raw) < 40:
        raise ValueError("Mesh binary qua ngan")
    magic, nverts, nidx, flags, xmin, xmax, ymin, ymax, zmin, zmax = struct.unpack_from("<4sIII6f", raw, 0)
    if magic not in (b"DTM1", b"DTM2"):
        raise ValueError("Mesh binary khong hop le")
    off = 40
    pos = np.frombuffer(raw, dtype=np.float32, count=nverts * 3, offset=off).reshape(nverts, 3).copy()
    off += nverts * 12
    colors = None
    if flags & 1:
        colors = np.frombuffer(raw, dtype=np.uint8, count=nverts * 3, offset=off).reshape(nverts, 3).copy()
        off += nverts * 3
        off = (off + 3) & ~3
    uvs = None
    if flags & 2:
        uvs = np.frombuffer(raw, dtype=np.float32, count=nverts * 2, offset=off).reshape(nverts, 2).copy()
        off += nverts * 8
    normals = None
    if flags & 4:
        normals = np.frombuffer(raw, dtype=np.float32, count=nverts * 3, offset=off).reshape(nverts, 3).copy()
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
        "flags": int(flags),
        "pos": pos,
        "colors": colors,
        "uvs": uvs,
        "normals": normals,
        "pages": pages,
        "indices": indices,
        "xmin": float(xmin),
        "xmax": float(xmax),
        "ymin": float(ymin),
        "ymax": float(ymax),
        "zmin": float(zmin),
        "zmax": float(zmax),
    }


def _write_obj_mesh_binary(path: Path, mesh: dict) -> None:
    import numpy as np

    pos = np.ascontiguousarray(mesh["pos"], dtype=np.float32)
    indices = np.ascontiguousarray(mesh["indices"], dtype=np.uint32)
    flags = int(mesh.get("flags") or 0)
    nverts = int(pos.shape[0])
    header = struct.pack(
        "<4sIII6f",
        OBJ_MESH_MAGIC,
        nverts,
        int(indices.size),
        flags,
        float(mesh["xmin"]),
        float(mesh["xmax"]),
        float(mesh["ymin"]),
        float(mesh["ymax"]),
        float(mesh["zmin"]),
        float(mesh["zmax"]),
    )
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as handle:
        handle.write(header)
        pos.tofile(handle)
        if flags & 1 and mesh.get("colors") is not None:
            colors = np.ascontiguousarray(mesh["colors"], dtype=np.uint8)
            colors.tofile(handle)
            pad = (-int(colors.size)) % 4
            if pad:
                handle.write(b"\x00" * pad)
        if flags & 2 and mesh.get("uvs") is not None:
            np.ascontiguousarray(mesh["uvs"], dtype=np.float32).tofile(handle)
        if flags & 4 and mesh.get("normals") is not None:
            np.ascontiguousarray(mesh["normals"], dtype=np.float32).tofile(handle)
        if flags & 8 and mesh.get("pages") is not None:
            pages = np.ascontiguousarray(mesh["pages"], dtype=np.uint8)
            pages.tofile(handle)
            pad = (-int(pages.size)) % 4
            if pad:
                handle.write(b"\x00" * pad)
        indices.tofile(handle)
    tmp.replace(path)


def _as_xy_ring(ring: Any) -> list[tuple[float, float]]:
    if not isinstance(ring, (list, tuple)):
        return []
    out: list[tuple[float, float]] = []
    for p in ring:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        try:
            out.append((float(p[0]), float(p[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _geojson_polygons(geojson: dict) -> list[tuple]:
    if not isinstance(geojson, dict):
        return []
    kind = geojson.get("type")
    features = []
    if kind == "FeatureCollection":
        features = list(geojson.get("features") or [])
    elif kind == "Feature":
        features = [geojson]
    elif kind in {"Polygon", "MultiPolygon"}:
        features = [{"type": "Feature", "geometry": geojson}]
    polys = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry") or {}
        if not isinstance(geom, dict):
            continue
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        parts: list[Any] = []
        if gtype == "Polygon":
            parts = [coords]
        elif gtype == "MultiPolygon" and isinstance(coords, list):
            parts = coords
        for part in parts:
            if not isinstance(part, (list, tuple)) or not part:
                continue
            exterior = _as_xy_ring(part[0])
            if len(exterior) < 3:
                continue
            holes = []
            for hole in part[1:]:
                ring = _as_xy_ring(hole)
                if len(ring) >= 3:
                    holes.append(ring)
            polys.append((exterior, holes))
    return polys


def _points_in_polygons(lon: Any, lat: Any, polygons: list[tuple]) -> Any:
    import numpy as np
    from matplotlib.path import Path

    n = int(lon.shape[0])
    inside = np.zeros(n, dtype=bool)
    xy = np.column_stack((lon, lat))
    for exterior, holes in polygons:
        mask = Path(exterior, closed=True).contains_points(xy)
        for hole in holes:
            mask &= ~Path(hole, closed=True).contains_points(xy)
        inside |= mask
    return inside


def clip_obj_mesh_by_geojson(geojson: dict) -> tuple[Path, dict]:
    """Cat mesh OBJ theo polygon GeoJSON (EPSG:4326). Giu bbox local goc de khong keo gian mesh."""
    import numpy as np

    polygons = _geojson_polygons(geojson)
    if not polygons:
        raise ValueError("Shapefile khong co polygon")
    bbox = wgs84_product_bounds()
    if not bbox:
        raise ValueError("Khong co bounds WGS84 (can DEM hoac orthomosaic)")
    clip_id = hashlib.sha1(
        json.dumps(geojson, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    dest = _obj_preview_dir() / f"model-mesh-clip-{clip_id}.bin"
    src, _jpg = build_obj_preview()
    if dest.is_file() and dest.stat().st_mtime >= src.stat().st_mtime and dest.stat().st_size > 64:
        mesh = _read_obj_mesh_binary(dest)
        return dest, {
            "clip_id": clip_id,
            "vertices": int(mesh["pos"].shape[0]),
            "faces": int(mesh["indices"].size // 3),
        }

    mesh = _read_obj_mesh_binary(src)
    pos = mesh["pos"]
    indices = mesh["indices"]
    if indices.size < 3:
        raise ValueError("Mesh khong co tam giac")
    obj_x = pos[:, 0]
    obj_y = -pos[:, 2]
    frame = product_geo_frame()
    if frame:
        lon, lat = local_xy_to_wgs84_frame(obj_x, obj_y, frame)
    else:
        xspan = max(mesh["xmax"] - mesh["xmin"], 1e-9)
        yspan = max(mesh["zmax"] - mesh["zmin"], 1e-9)
        obj_ymin = -mesh["zmax"]
        lon = float(bbox["west"]) + (obj_x - mesh["xmin"]) / xspan * (float(bbox["east"]) - float(bbox["west"]))
        lat = float(bbox["south"]) + (obj_y - obj_ymin) / yspan * (float(bbox["north"]) - float(bbox["south"]))
    faces = indices.reshape(-1, 3)
    cx = (lon[faces[:, 0]] + lon[faces[:, 1]] + lon[faces[:, 2]]) / 3.0
    cy = (lat[faces[:, 0]] + lat[faces[:, 1]] + lat[faces[:, 2]]) / 3.0
    keep = _points_in_polygons(cx, cy, polygons)
    if not bool(np.any(keep)):
        raise ValueError("Polygon khong cat trung mesh. Kiem tra vi tri shapefile va Output (output-3d).")
    kept = faces[keep].ravel()
    used, inverse = np.unique(kept, return_inverse=True)
    clipped = {
        "flags": mesh["flags"],
        "pos": pos[used],
        "colors": mesh["colors"][used] if mesh["colors"] is not None else None,
        "uvs": mesh["uvs"][used] if mesh["uvs"] is not None else None,
        "normals": mesh["normals"][used] if mesh["normals"] is not None else None,
        "indices": inverse.astype(np.uint32, copy=False),
        "xmin": mesh["xmin"],
        "xmax": mesh["xmax"],
        "ymin": mesh["ymin"],
        "ymax": mesh["ymax"],
        "zmin": mesh["zmin"],
        "zmax": mesh["zmax"],
    }
    if clipped["colors"] is None:
        clipped["flags"] &= ~1
    if clipped["uvs"] is None:
        clipped["flags"] &= ~2
    if clipped["normals"] is None:
        clipped["flags"] &= ~4
    _write_obj_mesh_binary(dest, clipped)
    ensure_obj_fast_lod(dest)
    return dest, {
        "clip_id": clip_id,
        "vertices": int(used.size),
        "faces": int(keep.sum()),
        "faces_before": int(faces.shape[0]),
    }


def clipped_obj_mesh_path(clip_id: str) -> Path | None:
    if not clip_id or not re.fullmatch(r"[0-9a-f]{8,32}", clip_id):
        return None
    path = _obj_preview_dir() / f"model-mesh-clip-{clip_id}.bin"
    return path if path.is_file() else None


def _export_mesh_binary_to_obj(mesh: dict, obj_path: Path, mtl_name: str, tex_rel: str | None) -> None:
    import numpy as np

    pos = np.asarray(mesh["pos"], dtype=np.float64)
    xyz = np.empty_like(pos)
    xyz[:, 0] = pos[:, 0]
    xyz[:, 1] = -pos[:, 2]
    xyz[:, 2] = pos[:, 1]
    uvs = np.asarray(mesh["uvs"], dtype=np.float64) if mesh.get("uvs") is not None else None
    nrm = mesh.get("normals")
    nrm_obj = None
    if nrm is not None:
        nrm = np.asarray(nrm, dtype=np.float64)
        nrm_obj = np.empty_like(nrm)
        nrm_obj[:, 0] = nrm[:, 0]
        nrm_obj[:, 1] = -nrm[:, 2]
        nrm_obj[:, 2] = nrm[:, 1]
    colors = np.asarray(mesh["colors"], dtype=np.float64) / 255.0 if mesh.get("colors") is not None else None
    faces = np.asarray(mesh["indices"], dtype=np.int64).reshape(-1, 3) + 1
    obj_path.parent.mkdir(parents=True, exist_ok=True)
    with obj_path.open("wb") as handle:
        handle.write(b"# digital-twin clipped mesh\n")
        handle.write(f"mtllib {mtl_name}\n".encode("ascii"))
        handle.write(b"usemtl material\n")
        if colors is not None and colors.shape[0] == xyz.shape[0]:
            np.savetxt(handle, np.column_stack((xyz, colors)), fmt="v %.7f %.7f %.7f %.5f %.5f %.5f")
        else:
            np.savetxt(handle, xyz, fmt="v %.7f %.7f %.7f")
        if uvs is not None:
            np.savetxt(handle, uvs, fmt="vt %.7f %.7f")
        if nrm_obj is not None:
            np.savetxt(handle, nrm_obj, fmt="vn %.6f %.6f %.6f")
        a, b, c = faces[:, 0], faces[:, 1], faces[:, 2]
        if uvs is not None and nrm_obj is not None:
            out = np.column_stack((a, a, a, b, b, b, c, c, c))
            np.savetxt(handle, out, fmt="f %d/%d/%d %d/%d/%d %d/%d/%d")
        elif uvs is not None:
            out = np.column_stack((a, a, b, b, c, c))
            np.savetxt(handle, out, fmt="f %d/%d %d/%d %d/%d")
        elif nrm_obj is not None:
            out = np.column_stack((a, a, b, b, c, c))
            np.savetxt(handle, out, fmt="f %d//%d %d//%d %d//%d")
        else:
            np.savetxt(handle, faces, fmt="f %d %d %d")
    mtl_path = obj_path.with_name(mtl_name)
    tex_line = f"map_Kd {tex_rel}\n" if tex_rel else ""
    mtl_path.write_text(
        "# digital-twin clipped mesh\nnewmtl material\nKa 1 1 1\nKd 1 1 1\nKs 0 0 0\n"
        f"d 1\nillum 1\n{tex_line}",
        encoding="ascii",
    )


def save_clipped_obj_mesh(clip_id: str) -> dict:
    bin_path = clipped_obj_mesh_path(clip_id)
    if bin_path is None:
        raise ValueError("Chua co mesh da cat. Hay bam Toi uu du lieu truoc.")
    mesh = _read_obj_mesh_binary(bin_path)
    if mesh["indices"].size < 3:
        raise ValueError("Mesh da cat khong co tam giac")
    model_dir = Path(OUTPUT) / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    src_obj = model_dir / "model.obj"
    full_obj = model_dir / "model-full.obj"
    _obj, tex = _obj_source_files()
    tex_rel = None
    if tex and tex.is_file():
        try:
            tex_rel = Path(os.path.relpath(tex, model_dir)).as_posix()
        except ValueError:
            dest_tex = model_dir / tex.name
            if dest_tex.resolve() != tex.resolve():
                shutil.copy2(tex, dest_tex)
            tex_rel = dest_tex.name
    renamed = False
    if src_obj.is_file() and not full_obj.is_file():
        src_obj.replace(full_obj)
        renamed = True
    _export_mesh_binary_to_obj(mesh, src_obj, "model.mtl", tex_rel)
    for leftover in (model_dir / "model-clipped.obj", model_dir / "model-clipped.mtl"):
        try:
            leftover.unlink()
        except OSError:
            pass
    preview = _obj_preview_dir()
    for stale in preview.glob("model-mesh-*.bin"):
        try:
            stale.unlink()
        except OSError:
            pass
    nfaces = int(mesh["indices"].size // 3)
    return {
        "path": str(src_obj),
        "full_path": str(full_obj) if full_obj.is_file() else None,
        "renamed": renamed,
        "vertices": int(mesh["pos"].shape[0]),
        "faces": nfaces,
    }


def load_cameras(path: Path):
    rows = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle):
            try:
                rows.append(
                    {
                        "label": row.get("Label", ""),
                        "lon": float(row["Longitude"]),
                        "lat": float(row["Latitude"]),
                        "alt": float(row["Altitude"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
    return rows


_JOB_LOCK = threading.Lock()
_OBJ_PREVIEW_LOCK = threading.Lock()
_OBJ_BUILD_LOCK = threading.Lock()
_OBJ_FAST_EV = threading.Event()
_OBJ_VIEW_EV = threading.Event()
_OBJ_BUILD: dict = {
    "running": False,
    "pct": 0.0,
    "message": "",
    "error": None,
    "vertices": 0,
    "faces": 0,
    "gen": 0,
    "output": None,
}
_PATH_LOCK = threading.Lock()
_ORTHO_CACHE_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
ORTHO_CACHE_MAX_TILES_Z = 1600
_ORTHO_CACHE_JOB: dict = {
    "running": False,
    "pct": 0.0,
    "message": "",
    "z": None,
    "zooms": [],
    "saved": 0,
    "from_cache": 0,
    "error": None,
}
_ORTHO_CACHE_PENDING: tuple | None = None


def read_paths_file() -> dict:
    if not PATHS_FILE.is_file():
        return {}
    try:
        data = json.loads(PATHS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def normalize_user_path(path_str: str) -> Path:
    text = str(path_str or "").strip().strip('"').strip("'")
    if not text:
        raise ValueError("Empty path")
    if os.name == "nt":
        text = text.replace("/", "\\")
        while text.startswith("\\\\\\"):
            text = text[1:]
    path = Path(text).expanduser()
    try:
        if path.exists():
            return path.resolve()
    except OSError:
        pass
    return path


def path_access_note(path: Path) -> str:
    if path.is_dir():
        return ""
    text = str(path)
    if os.name == "nt" and len(text) >= 2 and text[1] == ":" and not text.startswith("\\\\"):
        return (
            f"{text} was not found on the Windows Server. "
            "A local PC drive letter cannot be used. "
            "Use Server or a UNC path \\\\computer\\share."
        )
    return f"Folder not found by the Windows Server service: {text}"


def _dir_has_jpgs(folder: Path) -> bool:
    try:
        for entry in folder.iterdir():
            if entry.is_file() and entry.suffix.lower() in {".jpg", ".jpeg"}:
                return True
    except OSError:
        return False
    return False


def discover_mission_dirs(folder: Path, depth: int = 0, max_depth: int = 2) -> list[Path]:
    """Thư mục cha: tự quét thư mục con có ảnh raw (cùng logic digital-twin.py)."""
    if not folder.is_dir():
        return []
    if _dir_has_jpgs(folder):
        return [folder]
    if depth >= max_depth:
        return []
    found: list[Path] = []
    try:
        children = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    for child in children:
        if child.is_dir() and not child.name.startswith("."):
            found.extend(discover_mission_dirs(child, depth + 1, max_depth))
    return found


def parse_parent_image_dir(value) -> Path:
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value if str(item).strip()]
    else:
        text = str(value or "").replace(";", "\n").replace("|", "\n")
        parts = [line.strip().strip('"').strip("'") for line in text.splitlines() if line.strip()]
    if not parts:
        raise ValueError("Input image directory is empty")
    dirs = [normalize_user_path(part) for part in parts]
    if len(dirs) == 1:
        return dirs[0]
    parents = {str(item.parent.resolve()).lower(): item.parent for item in dirs}
    if len(parents) == 1:
        return next(iter(parents.values()))
    return dirs[0]


def image_dirs() -> list[Path]:
    root = Path(IMAGES)
    found = discover_mission_dirs(root)
    return found or ([root] if root.is_dir() else [])


def paths_public() -> dict:
    root = Path(IMAGES)
    scanned = image_dirs()
    output = normalize_user_path(str(OUTPUT)) if str(OUTPUT).strip() else Path(OUTPUT)
    return {
        "images": str(root),
        "images_list": [str(p) for p in scanned],
        "images_scanned": [str(p) for p in scanned],
        "output": str(output),
        "images_exists": root.is_dir(),
        "output_exists": output.is_dir(),
        "images_note": path_access_note(root),
        "output_note": path_access_note(output),
    }


def save_paths() -> None:
    PATHS_FILE.write_text(
        json.dumps(paths_public(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


_ORTHO_CACHE_FLAT_FOR: str | None = None


def ortho_tile_cache_root() -> Path:
    global _ORTHO_CACHE_FLAT_FOR
    root = Path(OUTPUT) / ".ortho-tiles"
    root.mkdir(parents=True, exist_ok=True)
    key = str(root.resolve())
    if TILES_MOD is not None and _ORTHO_CACHE_FLAT_FOR != key:
        TILES_MOD.flatten_ortho_cache_root(root)
        _ORTHO_CACHE_FLAT_FOR = key
    return root


def raster_tile_cache_root(raster_path: Path | str) -> Path:
    path = Path(raster_path)
    try:
        if path.resolve() == ortho_geotiff().resolve():
            return ortho_tile_cache_root()
    except OSError:
        pass
    return TILE_CACHE_ROOT


def ortho_cache_public() -> dict:
    with _ORTHO_CACHE_LOCK:
        job = dict(_ORTHO_CACHE_JOB)
    path = ortho_geotiff()
    zooms: list[int] = []
    if TILES_MOD is not None and path.is_file():
        try:
            zooms = TILES_MOD.list_cached_zooms(path, ortho_tile_cache_root())
        except Exception:
            zooms = list(job.get("zooms") or [])
    job["zooms"] = zooms
    job["ready"] = bool(zooms) and not job.get("running")
    job["path"] = str(ortho_tile_cache_root()) if path.is_file() else None
    return job


def _open_ortho_src(path: Path):
    src = None
    own_src = False
    if DEM3D is not None:
        try:
            src = DEM3D._open_cached_raster(str(path))
        except Exception:
            src = None
    if src is None:
        import rasterio

        src = rasterio.open(path)
        own_src = True
    return src, own_src


def _cache_one_ortho_zoom(z: int, bounds: dict | None) -> None:
    path = ortho_geotiff()
    if TILES_MOD is None or not path.is_file():
        raise RuntimeError("Chua co orthomosaic.tif")
    cache_root = ortho_tile_cache_root()
    src, own_src = _open_ortho_src(path)
    saved = 0
    from_cache = 0
    try:
        with _ORTHO_CACHE_LOCK:
            _ORTHO_CACHE_JOB["z"] = z
            _ORTHO_CACHE_JOB["message"] = f"Cache viewport z{z}"
        if bounds and all(k in bounds for k in ("west", "south", "east", "north")):
            stats = TILES_MOD.ensure_viewport_tiles(
                src,
                path,
                z,
                float(bounds["west"]),
                float(bounds["south"]),
                float(bounds["east"]),
                float(bounds["north"]),
                cache_root=cache_root,
                write_cache=True,
                max_tiles=256,
            )
            from_cache += int(stats.get("from_cache") or 0)
            saved += int(stats.get("rendered") or 0)
        x_min, x_max, y_min, y_max = TILES_MOD.raster_tile_range(src, z)
        tiles = [
            (x, y)
            for x in range(x_min, x_max + 1)
            for y in range(y_min, y_max + 1)
        ]
        if len(tiles) > ORTHO_CACHE_MAX_TILES_Z:
            with _ORTHO_CACHE_LOCK:
                _ORTHO_CACHE_JOB["pct"] = 100.0
                _ORTHO_CACHE_JOB["message"] = f"z{z}: cache khung nhin ({len(tiles)} tile toan canh, bo qua)"
                _ORTHO_CACHE_JOB["saved"] = saved
                _ORTHO_CACHE_JOB["from_cache"] = from_cache
            return
        with _ORTHO_CACHE_LOCK:
            _ORTHO_CACHE_JOB["message"] = f"Cache toan z{z} ({len(tiles)} tile)"
        for index, (x, y) in enumerate(tiles, start=1):
            hit = False
            try:
                _payload, hit = TILES_MOD.get_or_render_tile(
                    src, path, z, x, y, cache_root=cache_root, write_cache=True
                )
            except Exception:
                pass
            if hit:
                from_cache += 1
            else:
                saved += 1
            if index % 25 == 0 or index == len(tiles):
                with _ORTHO_CACHE_LOCK:
                    _ORTHO_CACHE_JOB["pct"] = round(100.0 * index / float(len(tiles)), 1)
                    _ORTHO_CACHE_JOB["saved"] = saved
                    _ORTHO_CACHE_JOB["from_cache"] = from_cache
        with _ORTHO_CACHE_LOCK:
            zooms = list(_ORTHO_CACHE_JOB.get("zooms") or [])
            if z not in zooms:
                zooms.append(z)
                zooms.sort()
            _ORTHO_CACHE_JOB["zooms"] = zooms
            _ORTHO_CACHE_JOB["pct"] = 100.0
            _ORTHO_CACHE_JOB["message"] = f"Xong cache z{z}"
            _ORTHO_CACHE_JOB["error"] = None
    finally:
        if own_src:
            try:
                src.close()
            except Exception:
                pass


def _ortho_zoom_worker(z: int, bounds: dict | None) -> None:
    global _ORTHO_CACHE_PENDING
    try:
        _cache_one_ortho_zoom(z, bounds)
    except Exception as exc:
        with _ORTHO_CACHE_LOCK:
            _ORTHO_CACHE_JOB["error"] = str(exc)
            _ORTHO_CACHE_JOB["message"] = str(exc)
    nxt = None
    with _ORTHO_CACHE_LOCK:
        nxt = _ORTHO_CACHE_PENDING
        _ORTHO_CACHE_PENDING = None
        if nxt is None:
            _ORTHO_CACHE_JOB["running"] = False
    if nxt is not None:
        nz, nb = nxt
        with _ORTHO_CACHE_LOCK:
            _ORTHO_CACHE_JOB["running"] = True
            _ORTHO_CACHE_JOB["z"] = nz
            _ORTHO_CACHE_JOB["pct"] = 0.0
            _ORTHO_CACHE_JOB["message"] = f"Cache z{nz}"
        _ortho_zoom_worker(nz, nb)


def start_ortho_zoom_cache(z: int, bounds: dict | None = None) -> dict:
    global _ORTHO_CACHE_PENDING
    path = ortho_geotiff()
    if not path.is_file():
        data = ortho_cache_public()
        data["error"] = f"Khong co {path}"
        return data
    z = max(0, min(MAP_MAX_ZOOM, int(z)))
    if TILES_MOD is not None and TILES_MOD.zoom_has_tiles(path, z, ortho_tile_cache_root()):
        with _ORTHO_CACHE_LOCK:
            zooms = list(_ORTHO_CACHE_JOB.get("zooms") or [])
            if z not in zooms:
                zooms.append(z)
                zooms.sort()
            if not _ORTHO_CACHE_JOB.get("running"):
                _ORTHO_CACHE_JOB.update(
                    {"z": z, "zooms": zooms, "message": f"Da co cache z{z}", "pct": 100.0, "error": None}
                )
        return ortho_cache_public()
    with _ORTHO_CACHE_LOCK:
        if _ORTHO_CACHE_JOB.get("running"):
            if _ORTHO_CACHE_JOB.get("z") != z:
                _ORTHO_CACHE_PENDING = (z, bounds)
            return dict(_ORTHO_CACHE_JOB)
        _ORTHO_CACHE_JOB.update(
            {
                "running": True,
                "pct": 0.0,
                "z": z,
                "message": f"Cache z{z}",
                "error": None,
            }
        )
    threading.Thread(
        target=_ortho_zoom_worker,
        args=(z, bounds),
        name=f"ortho-cache-z{z}",
        daemon=True,
    ).start()
    return ortho_cache_public()


def any_job_running() -> bool:
    with _JOB_LOCK:
        return any(bool(job.get("running")) for job in _JOBS.values())


def bind_output_runtime() -> None:
    global OUTPUT
    OUTPUT = Path(OUTPUT).expanduser().resolve()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    dem_dir = OUTPUT / "dem"
    dem_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DEM3D_DATA_DIR"] = str(dem_dir)
    os.environ["TILE_CACHE_ROOT"] = str(TILE_CACHE_ROOT)
    TILE_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    if DEM3D is None:
        return
    setattr(DEM3D, "DATA_DIR", dem_dir)
    setattr(DEM3D, "TILE_CACHE_ROOT", TILE_CACHE_ROOT)
    registry = getattr(DEM3D, "_REGISTRY", None)
    if isinstance(registry, dict):
        registry.clear()
    try:
        DEM3D._bootstrap_registry()
    except Exception:
        pass
    dem_tif = ensure_dem_clipped_to_ortho()
    if dem_tif.is_file():
        DEM3D._register_raster(str(dem_tif))
        print(f"DEM 3D (cat theo bbox ortho): {dem_tif}")
    else:
        print(f"Chua co DEM: {dem_geotiff()}")
    ortho_tif = ortho_geotiff()
    if ortho_tif.is_file():
        DEM3D._register_raster(str(ortho_tif))
        print(f"Ortho: {ortho_tif}")
        print(f"Ortho tile cache (theo zoom): {ortho_tile_cache_root()}")
    else:
        print(f"Chua co orthomosaic: {ortho_tif}")
    try:
        obj_path, _tex = _obj_source_files()
        if obj_path.is_file():
            tiles = tileset_json()
            if tiles.is_file():
                print(f"Mesh OBJ: {obj_path}  (xem bang 3D Tiles: {tiles})")
            else:
                print(f"Mesh OBJ: {obj_path}  (chua co 3D Tiles — bam Mesh OBJ hoac Run Tiles)")
    except Exception as exc:
        print(f"Khong doc duoc mesh OBJ: {exc}")


def set_images_dir(path_str, persist: bool = True) -> dict:
    global IMAGES
    IMAGES = parse_parent_image_dir(path_str)
    if persist:
        save_paths()
    return paths_public()


def set_output_dir(path_str: str, persist: bool = True) -> dict:
    global OUTPUT
    text = str(path_str or "").strip()
    if not text:
        raise ValueError("Output directory is empty")
    OUTPUT = normalize_user_path(text)
    bind_output_runtime()
    if persist:
        save_paths()
    return paths_public()


def apply_saved_paths() -> bool:
    data = read_paths_file()
    if not data:
        return False
    if data.get("images"):
        set_images_dir(data.get("images"), persist=False)
    if data.get("output"):
        set_output_dir(str(data["output"]), persist=False)
        return True
    return False


DTW_QUALITIES = ("low", "medium", "high")
DTW_DEPTH_QUALITIES = ("ultra", "high", "medium", "low")


def parse_dtw_options(payload: dict) -> dict:
    """Doc tuy chon Run DTW tu payload; gia tri rong/sai bi bo qua (dung mac dinh digital-twin.py)."""
    opts: dict = {}
    quality = str(payload.get("quality") or "").strip().lower()
    opts["quality"] = quality if quality in DTW_QUALITIES else "medium"
    depth = str(payload.get("depth_quality") or "").strip().lower()
    if depth in DTW_DEPTH_QUALITIES:
        opts["depth_quality"] = depth
    conf = str(payload.get("cloud_min_conf") if payload.get("cloud_min_conf") is not None else "").strip()
    if conf.isdigit():
        opts["cloud_min_conf"] = int(conf)
    faces = str(payload.get("face_count") or "").strip().lower()
    if faces in ("high", "max") or (faces.isdigit() and int(faces) > 0):
        opts["face_count"] = faces
    return opts


def _job_cmd(
    kind: str,
    compute_mode: str = "gpu",
    mesh_3d: bool = True,
    texture_size: int | None = None,
    points: bool = False,
    dtw_opts: dict | None = None,
) -> list[str]:
    py = sys.executable
    if kind == "dtw":
        opts = dtw_opts or {}
        cmd = [
            py,
            "-u",
            str(HERE / "digital-twin.py"),
            "--images",
            str(IMAGES),
            "--output",
            str(OUTPUT),
            "--quality",
            opts.get("quality", "medium"),
        ]
        if opts.get("depth_quality"):
            cmd.extend(["--depth-quality", opts["depth_quality"]])
        if opts.get("cloud_min_conf") is not None:
            cmd.extend(["--cloud-min-conf", str(opts["cloud_min_conf"])])
        if opts.get("face_count"):
            cmd.extend(["--face-count", opts["face_count"]])
        if mesh_3d:
            cmd.append("--mesh-3d")
            cmd.extend(["--texture-pages", "4"])
        if texture_size in (4096, 8192, 16384):
            cmd.extend(["--texture-size", str(texture_size)])
        cmd.append("--cpu-only" if compute_mode == "cpu" else "--gpu-mask")
        if compute_mode != "cpu":
            cmd.append("10")
        return cmd
    if kind == "tiles":
        cmd = [
            py,
            "-u",
            str(HERE / "build-3dtiles.py"),
            str(OUTPUT),
            "--grid",
            "256",
            "--texture",
            "8192",
            "--max-zoom",
            "32",
        ]
        if points:
            cmd.append("--points")
        if compute_mode == "cpu":
            cmd.append("--cpu-only")
        else:
            cmd.extend(["--gpu-mask", "10"])
        return cmd
    raise ValueError(kind)


def _read_job_output(job: dict) -> None:
    proc = job["proc"]
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            job["lines"].append(line)
            found = _PCT_RE.search(line)
            if found:
                try:
                    job["pct"] = float(found.group(1))
                except ValueError:
                    pass
        proc.wait()
    except Exception as exc:
        job["lines"].append(f"[viewer] {exc}")
        try:
            proc.wait()
        except Exception:
            pass
    finally:
        job["exit_code"] = proc.returncode
        job["running"] = False
        if proc.returncode == 0 and job.get("pct") is None:
            job["pct"] = 100.0


def start_pipeline_job(
    kind: str,
    compute_mode: str = "gpu",
    mesh_3d: bool = True,
    texture_size: int | None = None,
    points: bool = False,
    dtw_opts: dict | None = None,
) -> dict:
    if kind not in {"dtw", "tiles"}:
        abort(404)
    if kind == "dtw" and compute_mode not in {"cpu", "gpu"}:
        return {"error": "compute_mode phai la cpu hoac gpu", "running": False}
    with _JOB_LOCK:
        current = _JOBS.get(kind)
        if current and current.get("running"):
            return {"error": f"{kind} dang chay", "running": True, **job_public(current)}
        other = "tiles" if kind == "dtw" else "dtw"
        alt = _JOBS.get(other)
        if alt and alt.get("running"):
            return {"error": f"Dang chay {other}. Cho xong roi chay {kind}.", "running": False}
        script = HERE / ("digital-twin.py" if kind == "dtw" else "build-3dtiles.py")
        if not script.is_file():
            return {"error": f"Khong thay {script}", "running": False}
        if kind == "dtw" and not Path(IMAGES).is_dir():
            return {"error": f"Khong thay thu muc anh: {IMAGES}", "running": False}
        if kind == "tiles":
            Path(OUTPUT).mkdir(parents=True, exist_ok=True)
        cmd = _job_cmd(
            kind,
            compute_mode,
            mesh_3d=mesh_3d if kind == "dtw" else False,
            texture_size=texture_size if kind == "dtw" else None,
            points=points if kind == "tiles" else False,
            dtw_opts=dtw_opts if kind == "dtw" else None,
        )
        kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "cwd": str(HERE),
            "text": True,
            "bufsize": 1,
            "encoding": "utf-8",
            "errors": "replace",
            "env": {**os.environ, "PYTHONUNBUFFERED": "1"},
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(cmd, **kwargs)
        job = {
            "kind": kind,
            "cmd": cmd,
            "proc": proc,
            "lines": deque(maxlen=2500),
            "running": True,
            "exit_code": None,
            "pct": 0.0,
            "started": time.time(),
        }
        job["lines"].append("$ " + " ".join(cmd))
        _JOBS[kind] = job
        threading.Thread(target=_read_job_output, args=(job,), daemon=True).start()
        return job_public(job)


def job_public(job: dict) -> dict:
    return {
        "kind": job.get("kind"),
        "running": bool(job.get("running")),
        "exit_code": job.get("exit_code"),
        "pct": job.get("pct"),
        "lines": list(job.get("lines") or []),
        "cmd": job.get("cmd"),
    }


@app.post("/api/jobs/<kind>")
def api_job_start(kind: str):
    try:
      payload = request.get_json(silent=True) or {}
      compute_mode = str(payload.get("compute_mode", "gpu")).lower()
      mesh_3d = bool(payload.get("mesh_3d", True))
      try:
          texture_size = int(payload.get("texture_size") or 0) or None
      except (TypeError, ValueError):
          texture_size = None
      if texture_size not in (4096, 8192, 16384, None):
          texture_size = None
      points = bool(payload.get("points"))
      data = start_pipeline_job(
          kind,
          compute_mode,
          mesh_3d=mesh_3d,
          texture_size=texture_size,
          points=points,
          dtw_opts=parse_dtw_options(payload) if kind == "dtw" else None,
      )
    except Exception as exc:
        return jsonify({"error": str(exc), "running": False}), 500
    status = 409 if data.get("error") and not data.get("running") else 200
    if data.get("error") and data.get("running"):
        status = 409
    return jsonify(data), status


@app.get("/api/jobs/<kind>")
def api_job_status(kind: str):
    if kind not in {"dtw", "tiles"}:
        abort(404)
    job = _JOBS.get(kind)
    if job is None:
        return jsonify({"kind": kind, "running": False, "exit_code": None, "pct": None, "lines": []})
    return jsonify(job_public(job))


@app.get("/")
def index():
    resp = Response(PAGE.replace("__PRODUCTS__", "[]"), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/api/paths")
def api_paths_get():
    return jsonify(paths_public())


@app.post("/api/paths")
def api_paths_set():
    if any_job_running():
        data = paths_public()
        data["error"] = "DTW/Tiles is running. Wait before changing folders."
        return jsonify(data), 409
    payload = request.get_json(silent=True) or {}
    try:
        with _PATH_LOCK:
            if "images" in payload:
                set_images_dir(payload.get("images"), persist=False)
            if "output" in payload:
                set_output_dir(str(payload.get("output") or ""), persist=False)
            save_paths()
        return jsonify(paths_public())
    except Exception as exc:
        data = paths_public()
        data["error"] = str(exc)
        return jsonify(data), 400


def list_server_drives() -> list[dict]:
    items = []
    if os.name == "nt":
        import string

        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.isdir(root):
                items.append({"name": root, "path": root})
    else:
        items.append({"name": "/", "path": "/"})
    return items


def fs_parent(path: Path) -> str:
    text = str(path)
    if os.name == "nt":
        if len(text) <= 3 and len(text) >= 2 and text[1] == ":":
            return ""
        if text.startswith("\\\\"):
            parts = [p for p in text.split("\\") if p]
            if len(parts) <= 2:
                return ""
    parent = path.parent
    if parent == path:
        return ""
    return str(parent)


@app.get("/api/fs/list")
def api_fs_list():
    raw = (request.args.get("path") or "").strip()
    if not raw:
        return jsonify({"path": "", "parent": "", "items": list_server_drives()})
    try:
        path = normalize_user_path(raw)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not path.is_dir():
        return jsonify(
            {
                "error": path_access_note(path) or f"Not a folder on the server: {path}",
                "path": str(path),
                "parent": "",
                "items": [],
            }
        ), 404
    items = []
    try:
        for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            try:
                if child.is_dir():
                    items.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except OSError as exc:
        return jsonify({"error": str(exc), "path": str(path), "parent": fs_parent(path), "items": []}), 403
    return jsonify({"path": str(path), "parent": fs_parent(path), "items": items})


_FS_BAD_NAME = re.compile(r'[<>:"/\\|?*]')


@app.post("/api/fs/mkdir")
def api_fs_mkdir():
    payload = request.get_json(silent=True) or {}
    parent_raw = str(payload.get("path") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not parent_raw:
        return jsonify({"error": "Open a drive or parent folder on the server first"}), 400
    if not name or name in {".", ".."} or _FS_BAD_NAME.search(name):
        return jsonify({"error": "Invalid folder name"}), 400
    try:
        parent = normalize_user_path(parent_raw)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not parent.is_dir():
        return jsonify({"error": path_access_note(parent) or f"Parent folder not found: {parent}"}), 404
    dest = parent / name
    try:
        dest.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        return jsonify({"error": f"Folder already exists: {name}"}), 409
    except OSError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"path": str(dest), "parent": str(parent), "name": name})


@app.post("/api/draw/import-shp")
def api_import_shp():
    """Mo *.shp.zip hoac bo .shp/.shx/.dbf/.prj -> GeoJSON EPSG:4326."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    try:
        from shp_import import shapefile_parts_to_geojson, shapefile_zip_to_geojson
    except Exception as exc:
        return jsonify({"success": False, "message": f"Thieu bo doc shapefile: {exc}"}), 500
    files = request.files.getlist("files") or []
    if not files and "file" in request.files:
        files = [request.files["file"]]
    files = [item for item in files if item and item.filename]
    if not files:
        return jsonify({"success": False, "message": "Chưa chọn file shapefile."}), 400
    view_arg = str(request.args.get("view") or request.form.get("view") or "").lower()
    view_fast = view_arg not in ("0", "false", "full", "edit")
    try:
        zip_file = next((item for item in files if (item.filename or "").lower().endswith(".zip")), None)
        if zip_file is not None:
            geojson = shapefile_zip_to_geojson(zip_file.read(), view_fast=view_fast)
        else:
            file_map = {Path(item.filename or "upload").name: item.read() for item in files}
            if not any(name.lower().endswith(".shp") for name in file_map):
                return jsonify({
                    "success": False,
                    "message": "Cần file .zip hoặc bộ file có .shp (kèm .shx/.dbf).",
                }), 400
            geojson = shapefile_parts_to_geojson(file_map, view_fast=view_fast)
        return jsonify({"success": True, "geojson": geojson})
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"success": False, "message": f"Lỗi đọc Shapefile: {exc}"}), 500


@app.get("/api/products")
def api_products():
    items = []
    for key, label, rel in PRODUCTS:
        path = OUTPUT / rel if rel else None
        exists = True if rel is None else bool(path and path.is_file())
        size_path = path
        out_rel = rel
        if key == "las":
            las_tiles = las_tileset_json()
            if las_tiles.is_file():
                exists = True
                out_rel = "tiled-las/tileset.json"
                size_path = las_tiles
            elif path and path.is_file():
                exists = True
                size_path = path
            else:
                exists = False
                size_path = path
        items.append(
            {
                "key": key,
                "label": label,
                "rel": out_rel,
                "exists": exists,
                "size": fmt_size(size_path) if size_path else "",
            }
        )
    return jsonify({"folder": str(OUTPUT), "items": items})


@app.get("/api/overview")
def api_overview():
    summary = {}
    summary_path = OUTPUT / "twin_summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    cameras_path = OUTPUT / "camera_reference.csv"
    cameras = load_cameras(cameras_path) if cameras_path.is_file() else []
    return jsonify({
        "folder": str(OUTPUT),
        "images": [str(p) for p in image_dirs()],
        "summary": summary,
        "cameras": cameras,
    })


@app.get("/api/cameras")
def api_cameras():
    path = safe_file("camera_reference.csv")
    if not path.is_file():
        abort(404)
    return jsonify(load_cameras(path))


@app.get("/api/points/las.meta.json")
def api_points_las_meta():
    path = safe_file("point_cloud/point_cloud.las")
    if not path.is_file():
        abort(404)
    try:
        stats = _las_file_stats(path)
        frame = product_geo_frame()
        bbox = (frame or {}).get("bounds_wgs84") or wgs84_product_bounds()
        full_bin, _fast_bin = _las_preview_paths()
        view_path = _las_view_path()
        view_bytes = packed_bin_bytes(view_path) if view_path.is_file() else (
            int(full_bin.stat().st_size) if full_bin.is_file() else 0
        )
        return jsonify(
            {
                "success": True,
                "total": int(stats["n"]),
                "mtime": int(path.stat().st_mtime),
                "has_rgb": bool(stats["has_rgb"]),
                "bounds_wgs84": bbox,
                "zmin": float(stats["zmin"]),
                "zmax": float(stats["zmax"]),
                "bytes": view_bytes,
                "frame": frame,
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.get("/api/points/las.bin")
def api_points_las_bin():
    path = safe_file("point_cloud/point_cloud.las")
    if not path.is_file():
        abort(404)
    try:
        lod = str(request.args.get("lod") or "").strip().lower()
        return stream_las_binary(path, lod=lod)
    except HTTPException:
        raise
    except Exception as exc:
        app.logger.exception("api_points/las.bin")
        return jsonify({"error": str(exc), "type": type(exc).__name__}), 500


@app.get("/api/points/<kind>")
def api_points(kind: str):
    try:
        if kind == "las.bin":
            path = safe_file("point_cloud/point_cloud.las")
            if not path.is_file():
                abort(404)
            return stream_las_binary(path, lod=str(request.args.get("lod") or "").strip().lower())
        if kind == "las":
            path = safe_file("point_cloud/point_cloud.las")
            if not path.is_file():
                abort(404)
            return jsonify(sample_las(path))
        if kind == "obj":
            path = safe_file("model/model.obj")
            if not path.is_file():
                abort(404)
            return jsonify(sample_obj_vertices(path))
        abort(404)
    except HTTPException:
        raise
    except Exception as exc:
        app.logger.exception("api_points/%s", kind)
        return jsonify({"error": str(exc), "type": type(exc).__name__}), 500
    return jsonify({})


@app.get("/text/<kind>")
def text_file(kind: str):
    rel = {"catalog": "mission_catalog.json", "log": "run.log"}.get(kind)
    if not rel:
        abort(404)
    path = safe_file(rel)
    if not path.is_file():
        abort(404)
    text = path.read_text(encoding="utf-8", errors="replace")
    if rel.endswith(".json"):
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    return Response(text, mimetype="text/plain; charset=utf-8")


@app.get("/media/ortho.png")
def media_ortho():
    path = safe_file("orthomosaic/orthomosaic.tif")
    if not path.is_file():
        abort(404)
    return Response(raster_png(path, None, MAX_ORTHO_RASTER), mimetype="image/png")


def photo_input_dirs() -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    for raw in image_dirs():
        try:
            root = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if not root.is_dir():
            continue
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots


def _photo_under_root(root: Path, stem: str) -> Path | None:
    for ext in (".JPG", ".jpg", ".JPEG", ".jpeg", ".PNG", ".png"):
        path = root / f"{stem}{ext}"
        if path.is_file():
            try:
                resolved = path.resolve()
                if resolved == root or root in resolved.parents:
                    return path
            except OSError:
                continue
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.stem != stem or path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
                continue
            try:
                resolved = path.resolve()
                if resolved == root or root in resolved.parents:
                    return path
            except OSError:
                continue
    except OSError:
        pass
    return None


def find_raw_photo(label: str) -> Path | None:
    name = Path(str(label or "")).name
    if not name or name in {".", ".."}:
        return None
    stem = name
    lower = name.lower()
    for ext in (".jpg", ".jpeg", ".png", ".dng"):
        if lower.endswith(ext):
            stem = name[: -len(ext)]
            break
    for root in photo_input_dirs():
        found = _photo_under_root(root, stem)
        if found is not None:
            return found
    return None


@app.get("/media/photo/<path:label>")
def media_photo(label: str):
    path = find_raw_photo(label)
    if path is None or not path.is_file():
        abort(404)
    try:
        max_w = int(request.args.get("w", "720"))
    except (TypeError, ValueError):
        max_w = 720
    max_w = max(160, min(max_w, 1600))
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = 40_000_000
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((max_w, max_w), Image.Resampling.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=78, optimize=True)
        return Response(buf.getvalue(), mimetype="image/jpeg")
    except Exception:
        return send_file(path)


@app.get("/media/dem.png")
def media_dem():
    path = ensure_dem_clipped_to_ortho()
    if not path.is_file():
        abort(404)
    return Response(raster_png(path, "terrain"), mimetype="image/png")


@app.get("/media/texture.jpg")
def media_texture():
    path = safe_file("texture/texture.jpg")
    if not path.is_file():
        abort(404)
    return send_file(path)


@app.post("/api/mesh/clip")
def api_mesh_clip():
    payload = request.get_json(silent=True) or {}
    geojson = payload.get("geojson")
    if not isinstance(geojson, dict):
        return jsonify({"success": False, "message": "Thieu geojson polygon"}), 400
    try:
        with _OBJ_PREVIEW_LOCK:
            _path, info = clip_obj_mesh_by_geojson(geojson)
        return jsonify({"success": True, **info})
    except HTTPException:
        raise
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("api/mesh/clip")
        return jsonify({"success": False, "message": str(exc), "type": type(exc).__name__}), 500


@app.post("/api/mesh/save")
def api_mesh_save():
    payload = request.get_json(silent=True) or {}
    clip_id = str(payload.get("clip_id") or "").strip()
    try:
        with _OBJ_PREVIEW_LOCK:
            info = save_clipped_obj_mesh(clip_id)
        return jsonify({"success": True, **info})
    except HTTPException:
        raise
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("api/mesh/save")
        return jsonify({"success": False, "message": str(exc), "type": type(exc).__name__}), 500


@app.get("/api/mesh/tiles.meta.json")
def api_mesh_tiles_meta():
    path = tileset_json()
    obj_path, _tex = _obj_source_files()
    ready = path.is_file() and path.stat().st_size > 32
    frame = None
    try:
        frame = product_geo_frame()
    except Exception:
        frame = None
    bounds = (frame or {}).get("bounds_wgs84") or wgs84_product_bounds()
    return jsonify(
        {
            "success": True,
            "ready": ready,
            "tileset_url": _versioned_tileset_url("/tiles", path),
            "obj_path": str(obj_path) if obj_path.is_file() else None,
            "obj_bytes": int(obj_path.stat().st_size) if obj_path.is_file() else 0,
            "mtime": int(path.stat().st_mtime) if ready else 0,
            "bytes": int(path.stat().st_size) if ready else 0,
            "bounds_wgs84": bounds,
            "frame": frame,
        }
    )


@app.get("/api/mesh/obj.bin")
def api_mesh_obj_bin():
    try:
        clip_id = str(request.args.get("clip") or "").strip()
        lod = str(request.args.get("lod") or "").strip().lower()
        if clip_id:
            with _OBJ_PREVIEW_LOCK:
                bin_path = clipped_obj_mesh_path(clip_id)
                if bin_path is None:
                    return jsonify({"error": "Chua co mesh da cat. Mo lai *.shp."}), 404
                if lod == "fast":
                    bin_path = ensure_obj_fast_lod(bin_path)
                elif lod in ("medium", "med"):
                    bin_path = ensure_obj_medium_lod(bin_path)
                elif lod in ("view", "pack", ""):
                    bin_path = ensure_obj_view_full(bin_path)
        else:
            bin_path = wait_obj_preview_lod(lod or "view")
        return send_cached_bin(bin_path, "model-mesh.bin")
    except HTTPException:
        raise
    except Exception as exc:
        app.logger.exception("api/mesh/obj.bin")
        return jsonify({"error": str(exc), "type": type(exc).__name__}), 500


@app.get("/api/mesh/obj.meta.json")
def api_mesh_obj_meta():
    try:
        clip_id = str(request.args.get("clip") or "").strip()
        frame = product_geo_frame()
        bbox = (frame or {}).get("bounds_wgs84") or wgs84_product_bounds()
        if clip_id:
            with _OBJ_PREVIEW_LOCK:
                bin_path = clipped_obj_mesh_path(clip_id)
                if bin_path is None:
                    return jsonify({"success": False, "error": "Chua co mesh da cat"}), 404
            header_path = bin_path
            pub = {
                "building": False,
                "pct": 100.0,
                "message": "San sang",
                "error": None,
                "fast_ready": True,
                "view_ready": True,
                "ready": True,
            }
        else:
            pub = obj_preview_public()
            st = _obj_preview_fresh()
            header_path = None
            if st["full_ok"]:
                header_path = st["paths"]["full"]
            elif st["fast_ok"]:
                header_path = st["paths"]["fast"]
            if pub.get("error") and not header_path:
                return jsonify({"success": False, "error": pub["error"]}), 500
        nverts = int(pub.get("vertices") or 0)
        faces = int(pub.get("faces") or 0)
        xmin = xmax = ymin = ymax = zmin = zmax = 0.0
        mtime = 0
        nbytes = 0
        if header_path and header_path.is_file():
            with header_path.open("rb") as handle:
                header = handle.read(40)
            if len(header) >= 40:
                _magic, hv, nidx, _flags, xmin, xmax, ymin, ymax, zmin, zmax = struct.unpack("<4sIII6f", header)
                if not nverts:
                    nverts = int(hv)
                if not faces:
                    faces = int(nidx // 3 if nidx else hv // 3)
            mtime = int(header_path.stat().st_mtime)
            view_path = header_path.with_name(header_path.stem.replace("-fast", "") + "-view.bin")
            full_path = header_path.with_name("model-mesh-full.bin") if header_path.name.endswith("-fast.bin") else header_path
            nbytes = packed_bin_bytes(full_path.with_name(full_path.stem + "-view.bin")) or packed_bin_bytes(full_path)
        obj_path, _tex = _obj_source_files()
        if obj_path.is_file() and not mtime:
            mtime = int(obj_path.stat().st_mtime)
        return jsonify(
            {
                "success": True,
                "bounds_wgs84": bbox,
                "frame": frame,
                "zmin": float(ymin),
                "zmax": float(ymax),
                "vertices": nverts,
                "faces": faces,
                "clipped": bool(clip_id),
                "clip_id": clip_id or None,
                "mtime": mtime,
                "bytes": int(nbytes),
                "building": bool(pub.get("building")),
                "pct": float(pub.get("pct") or 0),
                "message": str(pub.get("message") or ""),
                "error": pub.get("error"),
                "fast_ready": bool(pub.get("fast_ready")),
                "view_ready": bool(pub.get("view_ready")),
                "ready": bool(pub.get("ready")),
                "textures": [
                    f"/media/obj-texture.jpg?lod=full&page={i}&v={mtime}"
                    for i in range(max(1, len(ensure_obj_texture_pages())))
                ],
                "local": {
                    "xmin": float(xmin),
                    "xmax": float(xmax),
                    "ymin": float(ymin),
                    "ymax": float(ymax),
                    "zmin": float(zmin),
                    "zmax": float(zmax),
                },
            }
        )
    except HTTPException:
        raise
    except Exception as exc:
        app.logger.exception("api/mesh/obj.meta.json")
        return jsonify({"success": False, "error": str(exc), "type": type(exc).__name__}), 500


@app.get("/media/obj-texture.jpg")
def media_obj_texture():
    try:
        want_lod = str(request.args.get("lod") or "").strip().lower()
        page = 0
        try:
            page = max(0, int(request.args.get("page") or 0))
        except (TypeError, ValueError):
            page = 0
        if want_lod == "fast":
            jpg_path = ensure_obj_fast_texture()
        else:
            pages = ensure_obj_texture_pages(MAX_OBJ_TEXTURE)
            jpg_path = pages[min(page, len(pages) - 1)] if pages else None
        if not jpg_path or not Path(jpg_path).is_file():
            abort(404)
        return send_file(jpg_path, mimetype="image/jpeg", max_age=86400)
    except HTTPException:
        raise
    except Exception as exc:
        app.logger.exception("media/obj-texture")
        return jsonify({"error": str(exc), "type": type(exc).__name__}), 500


@app.get("/media/report.pdf")
def media_report():
    path = safe_file("report.pdf")
    if not path.is_file():
        abort(404)
    return send_file(path)


def ortho_geotiff() -> Path:
    return OUTPUT / "orthomosaic" / "orthomosaic.tif"


def dem_geotiff() -> Path:
    return OUTPUT / "dem" / "dem.tif"


def dem_clipped_geotiff() -> Path:
    return OUTPUT / "dem" / "dem-ortho.tif"


def load_build_3dtiles():
    spec = importlib.util.spec_from_file_location("dt_build_3dtiles", HERE / "build-3dtiles.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Khong load duoc build-3dtiles.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ensure_dem_clipped_to_ortho() -> Path:
    dem_path = dem_geotiff()
    ortho_path = ortho_geotiff()
    out_path = dem_clipped_geotiff()
    if not dem_path.is_file():
        return dem_path
    if not ortho_path.is_file():
        return dem_path
    try:
        if (
            out_path.is_file()
            and out_path.stat().st_mtime >= dem_path.stat().st_mtime
            and out_path.stat().st_mtime >= ortho_path.stat().st_mtime
        ):
            return out_path
        mod = load_build_3dtiles()
        return mod.write_dem_clipped_to_ortho(dem_path, ortho_path, out_path)
    except Exception as exc:
        print(f"Khong cat duoc DEM theo ortho: {exc}")
        return dem_path


def tileset_dir() -> Path:
    return OUTPUT / "tiled"


def tileset_json() -> Path:
    return tileset_dir() / "tileset.json"


def las_tileset_dir() -> Path:
    return OUTPUT / "tiled-las"


def las_tileset_json() -> Path:
    return las_tileset_dir() / "tileset.json"


def _versioned_tileset_url(prefix: str, path: Path) -> str:
    # Cesium gan query cua tileset.json vao moi request tile con → doi ?v= khi build lai.
    try:
        ver = path.stat().st_mtime_ns
    except OSError:
        return f"{prefix}/tileset.json"
    return f"{prefix}/tileset.json?v={ver}"


def _tile_response(path: Path):
    resp = send_file(path)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    if request.args.get("v"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def load_tiles_module():
    lib = str(LIB)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import tiles as gis_tiles

    return gis_tiles


@app.get("/gis-static/<path:filename>")
def gis_static(filename: str):
    return send_from_directory(GIS_STATIC, filename)



@app.get("/tiles/<path:relpath>")
def serve_3d_tiles(relpath: str):
    root = tileset_dir().resolve()
    path = (root / relpath).resolve()
    if path != root and root not in path.parents:
        abort(403)
    if not path.is_file():
        abort(404)
    return _tile_response(path)


@app.get("/tiles-las/<path:relpath>")
def serve_las_tiles(relpath: str):
    root = las_tileset_dir().resolve()
    path = (root / relpath).resolve()
    if path != root and root not in path.parents:
        abort(403)
    if not path.is_file():
        abort(404)
    return _tile_response(path)


@app.get("/api/points/tiles.meta.json")
def api_points_tiles_meta():
    path = las_tileset_json()
    las_path = OUTPUT / "point_cloud" / "point_cloud.las"
    ready = path.is_file() and path.stat().st_size > 32
    frame = None
    try:
        frame = product_geo_frame()
    except Exception:
        frame = None
    bounds = (frame or {}).get("bounds_wgs84") or wgs84_product_bounds()
    return jsonify(
        {
            "success": True,
            "ready": ready,
            "tileset_url": _versioned_tileset_url("/tiles-las", path),
            "las_path": str(las_path) if las_path.is_file() else None,
            "las_bytes": int(las_path.stat().st_size) if las_path.is_file() else 0,
            "mtime": int(path.stat().st_mtime) if ready else 0,
            "bytes": int(path.stat().st_size) if ready else 0,
            "bounds_wgs84": bounds,
            "frame": frame,
        }
    )


@app.get("/api/view3d/open")
def api_view3d_open():
    if DEM3D is None:
        return jsonify({"success": False, "message": "Chua gan thu vien dem-3d"}), 500
    path = ortho_geotiff()
    if not path.is_file():
        return jsonify({"success": False, "message": f"Khong co {path}"}), 404
    try:
        file_id = DEM3D._register_raster(str(path))
        meta = DEM3D._get_raster_metadata(str(path))
        mtime = int(path.stat().st_mtime)
        cache = ortho_cache_public()
        return jsonify(
            {
                "success": True,
                "file_id": file_id,
                "path": str(path),
                "filename": path.name,
                **meta,
                "mtime": mtime,
                "cache": cache,
                "tileset_ready": tileset_json().is_file(),
                "tileset_url": _versioned_tileset_url("/tiles", tileset_json()),
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.get("/api/geotiff/tiles/file/<file_id>/<int:z>/<int:x>/<int:y>.png")
def geotiff_xyz_tile(file_id: str, z: int, x: int, y: int):
    if DEM3D is None or TILES_MOD is None:
        abort(500)
    if z < 0 or z > MAP_MAX_ZOOM:
        abort(404)
    raster_path = DEM3D._resolve_uploaded_raster_path(file_id)
    if not raster_path:
        abort(404)
    try:
        src = DEM3D._open_cached_raster(raster_path)
        tile, from_cache = TILES_MOD.get_or_render_tile(
            src,
            raster_path,
            z,
            x,
            y,
            cache_root=raster_tile_cache_root(raster_path),
            write_cache=True,
        )
    except Exception:
        buf = TILES_MOD.empty_tile_buffer()
        resp = Response(buf.getvalue(), mimetype="image/png")
        resp.headers["Cache-Control"] = "no-store"
        return resp
    headers = {
        "Cache-Control": "public, max-age=604800, immutable" if from_cache else "public, max-age=86400",
        "Access-Control-Allow-Origin": "*",
    }
    if hasattr(tile, "getvalue"):
        resp = Response(tile.getvalue(), mimetype="image/png")
    elif hasattr(tile, "read"):
        tile.seek(0)
        resp = Response(tile.read(), mimetype="image/png")
    else:
        resp = send_file(str(tile), mimetype="image/png")
    for key, value in headers.items():
        resp.headers[key] = value
    return resp


@app.get("/api/ortho/cache")
def api_ortho_cache_get():
    return jsonify(ortho_cache_public())


@app.post("/api/ortho/cache")
def api_ortho_cache_start():
    payload = request.get_json(silent=True) or {}
    raw_z = payload.get("z")
    if raw_z is None:
        data = ortho_cache_public()
        data["error"] = "Can z (muc zoom) de sinh cache"
        return jsonify(data), 400
    try:
        z = int(raw_z)
    except (TypeError, ValueError):
        data = ortho_cache_public()
        data["error"] = "z khong hop le"
        return jsonify(data), 400
    bounds = None
    if all(payload.get(key) is not None for key in ("west", "south", "east", "north")):
        bounds = {
            "west": payload.get("west"),
            "south": payload.get("south"),
            "east": payload.get("east"),
            "north": payload.get("north"),
        }
    return jsonify(start_ortho_zoom_cache(z, bounds))


def load_dem3d_module():
    if not (DEM3D_ROOT / "app.py").is_file():
        raise RuntimeError(f"Khong thay thu vien dem-3d: {DEM3D_ROOT}")
    sys.path.insert(0, str(DEM3D_ROOT))
    spec = importlib.util.spec_from_file_location("dem3d_lib", DEM3D_ROOT / "app.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Khong load duoc dem-3d/app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def attach_dem3d_routes(mod) -> None:
    skip = {"index", "static"}
    for rule in mod.app.url_map.iter_rules():
        if rule.endpoint in skip:
            continue
        view = mod.app.view_functions.get(rule.endpoint)
        if view is None:
            continue
        methods = [m for m in (rule.methods or set()) if m not in {"HEAD", "OPTIONS"}]
        app.add_url_rule(
            rule.rule,
            endpoint="dem3d_" + rule.endpoint,
            view_func=view,
            methods=methods or ["GET"],
        )


@app.get("/api/dem3d/open")
def api_dem3d_open():
    if DEM3D is None:
        return jsonify({"success": False, "message": "Chua gan thu vien dem-3d"}), 500
    path = ensure_dem_clipped_to_ortho()
    if not path.is_file():
        return jsonify({"success": False, "message": f"Khong co {path}"}), 404
    try:
        file_id = DEM3D._register_raster(str(path))
        meta = DEM3D._get_raster_metadata(str(path))
        return jsonify(
            {
                "success": True,
                "file_id": file_id,
                "path": str(path),
                "filename": path.name,
                **meta,
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


def get_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        sock.close()


def main() -> None:
    global OUTPUT, IMAGES, DEM3D, TILES_MOD
    missing = []
    if not (DEM3D_ROOT / "app.py").is_file():
        missing.append(str(DEM3D_ROOT / "app.py"))
    if not (LIB / "tiles.py").is_file():
        missing.append(str(LIB / "tiles.py"))
    if not (GIS_STATIC / "vendors" / "cesium" / "Cesium" / "Cesium.js").is_file():
        missing.append(str(GIS_STATIC / "vendors" / "cesium" / "Cesium" / "Cesium.js"))
    if not (GIS_STATIC / "vendors" / "leaflet" / "leaflet.js").is_file():
        missing.append(str(GIS_STATIC / "vendors" / "leaflet" / "leaflet.js"))
    if not (GIS_STATIC / "vendors" / "three" / "three.min.js").is_file():
        missing.append(str(GIS_STATIC / "vendors" / "three" / "three.min.js"))
    if not (GIS_STATIC / "vendors" / "three" / "OrbitControls.js").is_file():
        missing.append(str(GIS_STATIC / "vendors" / "three" / "OrbitControls.js"))
    if not (GIS_STATIC / "vendors" / "digital-twin" / "Render-3D.js").is_file():
        missing.append(str(GIS_STATIC / "vendors" / "digital-twin" / "Render-3D.js"))
    if not (GIS_STATIC / "vendors" / "digital-twin" / "view3D_simulator.js").is_file():
        missing.append(str(GIS_STATIC / "vendors" / "digital-twin" / "view3D_simulator.js"))
    if not (GIS_STATIC / "vendors" / "maplibre-gl" / "maplibre-gl.js").is_file():
        missing.append(str(GIS_STATIC / "vendors" / "maplibre-gl" / "maplibre-gl.js"))
    if not (HERE / "shp_import.py").is_file():
        missing.append(str(HERE / "shp_import.py"))
    if not (HERE / "digital_twin_view.py").is_file():
        missing.append(str(HERE / "digital_twin_view.py"))
    if not (GIS_STATIC / "vendors" / "leaflet" / "images" / "layers.png").is_file():
        missing.append(str(GIS_STATIC / "vendors" / "leaflet" / "images" / "layers.png"))
    if missing:
        raise SystemExit("Thieu thu vien trong digital-twin/lib:\n  " + "\n  ".join(missing))
    parser = argparse.ArgumentParser(description="Web viewer cho output-new")
    parser.add_argument("output", nargs="?", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--images", default=str(DEFAULT_IMAGES), help="Thu muc cha anh raw (quet thu muc con)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    lan_ip = get_local_ip()
    bind_host = args.host
    public_host = lan_ip if bind_host in {"0.0.0.0", "::"} else bind_host
    OUTPUT = Path(args.output).expanduser().resolve()
    IMAGES = Path(args.images).expanduser().resolve()
    TILES_MOD = load_tiles_module()
    DEM3D = load_dem3d_module()
    attach_dem3d_routes(DEM3D)
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import digital_twin_view as dtv
    if "digital_twin" not in app.blueprints:
        app.register_blueprint(dtv.create_digital_twin_blueprint())
    if not apply_saved_paths():
        bind_output_runtime()
    scanned = image_dirs()
    print(f"Anh raw: {IMAGES}")
    if len(scanned) > 1:
        print("  Quet " + str(len(scanned)) + " thu muc con")
    print(f"Thu muc dau ra: {OUTPUT}")
    if tileset_json().is_file():
        print(f"3D Tiles: {tileset_json()}")
    else:
        print(f"Chua co 3D Tiles. Chay: {HERE / 'build-3dtiles.py'}")
    url = f"http://{public_host}:{args.port}/"
    print(f"Digital twin web UI: {url}")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host=bind_host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
