"""Doc shapefile (.shp.zip hoac bo .shp/.shx/.dbf/.prj) -> GeoJSON EPSG:4326."""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable

try:
    import shapefile
except ImportError as exc:  # pragma: no cover
    shapefile = None
    _SHP_IMPORT_ERROR = exc
else:
    _SHP_IMPORT_ERROR = None

try:
    from pyproj import CRS, Transformer
except ImportError:
    CRS = None
    Transformer = None

_VN_LON = (102.0, 110.0)
_VN_LAT = (8.0, 24.0)
_GUESS_EPSG = (32648, 32647, 3405, 3857, 3406)
_POINT = {1, 11, 21}
_MULTI = {8, 18, 28}
_LINE = {3, 13, 23}
_POLY = {5, 15, 25}


def shapefile_zip_to_geojson(zip_bytes: bytes, *, view_fast: bool = False) -> dict[str, Any]:
    del view_fast
    if not zip_bytes:
        raise ValueError("File ZIP rỗng.")
    with tempfile.TemporaryDirectory(prefix="dtw_shp_") as tmp:
        dest = Path(tmp)
        zip_path = dest / "upload.zip"
        zip_path.write_bytes(zip_bytes)
        _extract_zip(zip_path, dest)
        shp_files = sorted(p for p in dest.rglob("*.shp") if p.is_file())
        if not shp_files:
            raise ValueError("ZIP không chứa file .shp.")
        return _collection_from_shp_files(shp_files)


def shapefile_parts_to_geojson(file_map: dict[str, bytes], *, view_fast: bool = False) -> dict[str, Any]:
    del view_fast
    if not file_map:
        raise ValueError("Chưa chọn file shapefile.")
    with tempfile.TemporaryDirectory(prefix="dtw_shp_") as tmp:
        dest = Path(tmp)
        for name, data in file_map.items():
            safe = Path(name).name
            if not safe or safe in {".", ".."}:
                continue
            (dest / safe).write_bytes(data)
        shp_files = sorted(p for p in dest.glob("*.shp") if p.is_file())
        if not shp_files:
            raise ValueError("Cần file .zip hoặc bộ file có .shp (kèm .shx/.dbf).")
        return _collection_from_shp_files(shp_files)


def _extract_zip(zip_path: Path, dest: Path) -> None:
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest)):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as out:
                out.write(src.read())


def _collection_from_shp_files(shp_files: list[Path]) -> dict[str, Any]:
    features: list[dict[str, Any]] = []
    fields: list[str] = []
    for shp in shp_files:
        layer_fields, layer_features = _read_shp(shp)
        for name in layer_fields:
            if name not in fields:
                fields.append(name)
        features.extend(layer_features)
    if not features:
        raise ValueError("Shapefile không có đối tượng hợp lệ.")
    bbox = _bbox_of_features(features)
    out: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": features,
        "fields": fields,
    }
    if bbox:
        out["bbox"] = bbox
    return out


def _read_shp(shp_path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    if shapefile is None:
        raise ValueError("Thiếu thư viện pyshp. Cài: pip install pyshp pyproj") from _SHP_IMPORT_ERROR
    reader = _open_reader(shp_path)
    try:
        raw_fields = list(reader.fields[1:]) if reader.fields else []
        field_names = [str(item[0]) for item in raw_fields]
        transform = _transformer_for_shp(shp_path, reader)
        features: list[dict[str, Any]] = []
        for item in reader.iterShapeRecords():
            geom = _shape_to_geometry(item.shape)
            if not geom:
                continue
            if transform:
                geom = _transform_geometry(geom, transform)
            props: dict[str, Any] = {}
            record = list(item.record) if item.record is not None else []
            for idx, name in enumerate(field_names):
                value = record[idx] if idx < len(record) else None
                if isinstance(value, bytes):
                    value = value.decode("utf-8", "replace")
                elif hasattr(value, "isoformat"):
                    value = value.isoformat()
                props[name] = value
            features.append({"type": "Feature", "geometry": geom, "properties": props})
        return field_names, features
    finally:
        try:
            reader.close()
        except Exception:
            pass


def _open_reader(shp_path: Path):
    last_err: Exception | None = None
    for encoding in ("utf-8", "cp1258", "cp1252", "latin-1"):
        try:
            return shapefile.Reader(str(shp_path), encoding=encoding)
        except Exception as exc:
            last_err = exc
    if last_err:
        raise ValueError(f"Không đọc được shapefile: {last_err}") from last_err
    raise ValueError("Không đọc được shapefile.")


def _transformer_for_shp(shp_path: Path, reader) -> Callable[[float, float], tuple[float, float]] | None:
    prj = shp_path.with_suffix(".prj")
    bbox = _reader_bbox(reader)
    if prj.is_file():
        text = prj.read_text(encoding="utf-8", errors="ignore").strip()
        if text:
            transformer = _transformer_from_wkt(text)
            if transformer:
                return transformer
    if bbox and _looks_wgs84(bbox):
        return None
    if bbox:
        guessed = _guess_projected_transformer(bbox)
        if guessed:
            return guessed
    if bbox and not _looks_wgs84(bbox):
        raise ValueError(
            "Shapefile không có .prj và tọa độ không phải WGS84. "
            "Hãy đóng gói kèm file .prj trong *.shp.zip."
        )
    return None


def _transformer_from_wkt(wkt: str) -> Callable[[float, float], tuple[float, float]] | None:
    if CRS is None or Transformer is None:
        return None
    try:
        src = CRS.from_wkt(wkt)
        if src.to_epsg() == 4326 or src.is_geographic:
            return None
        tf = Transformer.from_crs(src, "EPSG:4326", always_xy=True)
        return lambda x, y: tf.transform(x, y)
    except Exception:
        return None


def _guess_projected_transformer(bbox: tuple[float, float, float, float]):
    if CRS is None or Transformer is None:
        return None
    cx = (bbox[0] + bbox[2]) / 2.0
    cy = (bbox[1] + bbox[3]) / 2.0
    for epsg in _GUESS_EPSG:
        try:
            tf = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
            lon, lat = tf.transform(cx, cy)
            if _in_vietnam(lon, lat):
                return lambda x, y, _tf=tf: _tf.transform(x, y)
        except Exception:
            continue
    return None


def _reader_bbox(reader) -> tuple[float, float, float, float] | None:
    try:
        box = list(reader.bbox)
    except Exception:
        return None
    if len(box) < 4:
        return None
    try:
        return float(box[0]), float(box[1]), float(box[2]), float(box[3])
    except (TypeError, ValueError):
        return None


def _looks_wgs84(bbox: tuple[float, float, float, float]) -> bool:
    minx, miny, maxx, maxy = bbox
    return (
        -180.0 <= minx <= 180.0
        and -180.0 <= maxx <= 180.0
        and -90.0 <= miny <= 90.0
        and -90.0 <= maxy <= 90.0
    )


def _in_vietnam(lon: float, lat: float) -> bool:
    return _VN_LON[0] <= lon <= _VN_LON[1] and _VN_LAT[0] <= lat <= _VN_LAT[1]


def _shape_to_geometry(shape) -> dict[str, Any] | None:
    if shape is None:
        return None
    stype = int(getattr(shape, "shapeType", 0) or 0)
    points = [_xy(pt) for pt in (shape.points or [])]
    if not points:
        return None
    if stype in _POINT:
        return {"type": "Point", "coordinates": list(points[0])}
    if stype in _MULTI:
        return {"type": "MultiPoint", "coordinates": [list(pt) for pt in points]}
    rings = _rings(shape, points)
    if not rings:
        return None
    if stype in _LINE:
        if len(rings) == 1:
            return {"type": "LineString", "coordinates": rings[0]}
        return {"type": "MultiLineString", "coordinates": rings}
    if stype in _POLY:
        polygons = _polygons_from_rings(rings)
        if not polygons:
            return None
        if len(polygons) == 1:
            return {"type": "Polygon", "coordinates": polygons[0]}
        return {"type": "MultiPolygon", "coordinates": polygons}
    if len(points) == 1:
        return {"type": "Point", "coordinates": list(points[0])}
    return {"type": "LineString", "coordinates": [list(pt) for pt in points]}


def _rings(shape, points: list[tuple[float, float]]) -> list[list[list[float]]]:
    starts = list(getattr(shape, "parts", None) or [0])
    rings: list[list[list[float]]] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(points)
        ring = [list(pt) for pt in points[start:end]]
        if len(ring) < 2:
            continue
        if ring[0] != ring[-1]:
            ring.append(list(ring[0]))
        rings.append(ring)
    return rings


def _polygons_from_rings(rings: list[list[list[float]]]) -> list[list[list[list[float]]]]:
    if len(rings) == 1:
        return [rings]
    exteriors: list[list[list[float]]] = []
    holes: list[list[list[float]]] = []
    for ring in rings:
        if _signed_area(ring) < 0:
            exteriors.append(ring)
        else:
            holes.append(ring)
    if not exteriors:
        return [[ring] for ring in rings]
    grouped: list[list[list[list[float]]]] = [[ext] for ext in exteriors]
    for hole in holes:
        sample = hole[0]
        attached = False
        for poly in grouped:
            if _point_in_ring(sample, poly[0]):
                poly.append(hole)
                attached = True
                break
        if not attached:
            grouped.append([hole])
    return grouped


def _signed_area(ring: list[list[float]]) -> float:
    area = 0.0
    for i in range(len(ring) - 1):
        area += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return area / 2.0


def _point_in_ring(pt: list[float], ring: list[list[float]]) -> bool:
    x, y = pt[0], pt[1]
    inside = False
    j = len(ring) - 1
    for i, node in enumerate(ring):
        xi, yi = node[0], node[1]
        xj, yj = ring[j][0], ring[j][1]
        intersects = ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi)
        if intersects:
            inside = not inside
        j = i
    return inside


def _xy(pt) -> tuple[float, float]:
    return float(pt[0]), float(pt[1])


def _transform_geometry(geom: dict[str, Any], transform) -> dict[str, Any]:
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    return {"type": gtype, "coordinates": _map_coords(coords, transform)}


def _map_coords(coords, transform):
    if not coords:
        return coords
    if isinstance(coords[0], (int, float)):
        x, y = transform(float(coords[0]), float(coords[1]))
        return [x, y]
    return [_map_coords(item, transform) for item in coords]


def _bbox_of_features(features: list[dict[str, Any]]) -> list[float] | None:
    xs: list[float] = []
    ys: list[float] = []

    def walk(coords):
        if not coords:
            return
        if isinstance(coords[0], (int, float)):
            xs.append(float(coords[0]))
            ys.append(float(coords[1]))
            return
        for item in coords:
            walk(item)

    for feat in features:
        geom = feat.get("geometry") or {}
        walk(geom.get("coordinates"))
    if not xs or not ys:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]
