from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

CORE_POLYGON = [
    (105.8316071906868, 28.072806164731343),
    (105.91675123365555, 28.1121799667603),
    (105.91537794263992, 28.27557786331242),
    (105.88791212232742, 28.366246393155855),
    (105.67924402644924, 28.33657472342928),
    (105.7043065874844, 28.212909671632772),
    (105.7537450640469, 28.156017959625107),
    (105.75443170955471, 28.101518824273246),
]


@dataclass
class RegionContext:
    core_min_lon: float
    core_max_lon: float
    core_min_lat: float
    core_max_lat: float
    center_lon: float
    center_lat: float
    coordinate_region_map: dict[tuple[float, float], str]


def is_town_south_point(row: dict) -> bool:
    row_id = row.get("row_id")
    col_id = row.get("col_id")
    try:
        row_id_int = int(float(row_id))
        col_id_int = int(float(col_id))
    except (TypeError, ValueError):
        return False
    return row_id_int == 1 and 3 <= col_id_int <= 6


def round_coord(value: float) -> float:
    return round(float(value), 6)


def point_in_polygon(longitude: float, latitude: float, polygon: list[tuple[float, float]]) -> bool:
    inside = False
    previous_index = len(polygon) - 1
    for index, (lon_i, lat_i) in enumerate(polygon):
        lon_j, lat_j = polygon[previous_index]
        intersects = ((lat_i > latitude) != (lat_j > latitude)) and (
            longitude < (lon_j - lon_i) * (latitude - lat_i) / ((lat_j - lat_i) or 1e-12) + lon_i
        )
        if intersects:
            inside = not inside
        previous_index = index
    return inside


def _read_coordinate_rows(input_path: Path) -> list[dict]:
    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        with input_path.open("r", encoding="utf-8-sig", newline="") as csvfile:
            reader = csv.DictReader(csvfile)
            rows = list(reader)
    else:
        dataframe = pd.read_excel(input_path)
        rows = dataframe.to_dict(orient="records")
    return [{str(key).strip().lower(): value for key, value in row.items()} for row in rows]


def _build_default_context() -> RegionContext:
    longitudes = [lon for lon, _ in CORE_POLYGON]
    latitudes = [lat for _, lat in CORE_POLYGON]
    return RegionContext(
        core_min_lon=min(longitudes),
        core_max_lon=max(longitudes),
        core_min_lat=min(latitudes),
        core_max_lat=max(latitudes),
        center_lon=(min(longitudes) + max(longitudes)) / 2,
        center_lat=(min(latitudes) + max(latitudes)) / 2,
        coordinate_region_map={},
    )


def _build_core_bounds_from_rows(rows: list[dict]) -> RegionContext:
    core_points: list[tuple[float, float]] = []
    for row in rows:
        latitude = float(row["latitude"])
        longitude = float(row["longitude"])
        if point_in_polygon(longitude, latitude, CORE_POLYGON):
            core_points.append((longitude, latitude))

    if not core_points:
        return _build_default_context()

    longitudes = [lon for lon, _ in core_points]
    latitudes = [lat for _, lat in core_points]
    return RegionContext(
        core_min_lon=min(longitudes),
        core_max_lon=max(longitudes),
        core_min_lat=min(latitudes),
        core_max_lat=max(latitudes),
        center_lon=(min(longitudes) + max(longitudes)) / 2,
        center_lat=(min(latitudes) + max(latitudes)) / 2,
        coordinate_region_map={},
    )


def _pick_region_column(row: dict) -> str | None:
    for key in ["region_name", "region", "area_name", "area"]:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _classify_core_region(longitude: float, latitude: float, context: RegionContext) -> str:
    lon_span = max(context.core_max_lon - context.core_min_lon, 1e-9)
    lat_span = max(context.core_max_lat - context.core_min_lat, 1e-9)

    left_boundary = context.core_min_lon + lon_span / 3
    right_boundary = context.core_min_lon + 2 * lon_span / 3
    lower_boundary = context.core_min_lat + lat_span / 3
    upper_boundary = context.core_min_lat + 2 * lat_span / 3

    if longitude < left_boundary:
        horizontal = "west"
    elif longitude > right_boundary:
        horizontal = "east"
    else:
        horizontal = "center"

    if latitude < lower_boundary:
        vertical = "south"
    elif latitude > upper_boundary:
        vertical = "north"
    else:
        vertical = "center"

    mapping = {
        ("north", "west"): "茶林区域-西北",
        ("north", "center"): "茶林区域-北部",
        ("north", "east"): "茶林区域-东北",
        ("center", "west"): "茶林区域-西部",
        ("center", "center"): "茶林区域（中）",
        ("center", "east"): "茶林区域-东部",
        ("south", "west"): "茶林区域-西南",
        ("south", "center"): "茶林区域-南部",
        ("south", "east"): "茶林区域-东南",
    }
    return mapping[(vertical, horizontal)]


def _classify_outer_region(longitude: float, latitude: float, context: RegionContext) -> str:
    if latitude > context.core_max_lat:
        return "外围-北部"
    if longitude < context.core_min_lon:
        return "外围-西部"
    if longitude > context.core_max_lon:
        return "外围-东部"
    if longitude <= context.center_lon:
        return "外围-西部"
    return "外围-东部"


def classify_region(longitude: float, latitude: float, context: RegionContext) -> str:
    if point_in_polygon(longitude, latitude, CORE_POLYGON):
        return _classify_core_region(longitude, latitude, context)
    return _classify_outer_region(longitude, latitude, context)


def build_region_context(coord_file: str | Path | None = None) -> RegionContext:
    if coord_file is None:
        return _build_default_context()

    input_path = Path(coord_file).expanduser()
    rows = _read_coordinate_rows(input_path)
    context = _build_core_bounds_from_rows(rows)
    coordinate_region_map: dict[tuple[float, float], str] = {}
    for row in rows:
        latitude = round_coord(float(row["latitude"]))
        longitude = round_coord(float(row["longitude"]))
        coordinate_region_map[(latitude, longitude)] = (
            _pick_region_column(row)
            or ("城镇区域（南）" if is_town_south_point(row) else classify_region(longitude, latitude, context))
        )
    context.coordinate_region_map = coordinate_region_map
    return context


def get_region_name(longitude: float, latitude: float, context: RegionContext) -> str:
    key = (round_coord(latitude), round_coord(longitude))
    if key in context.coordinate_region_map:
        return context.coordinate_region_map[key]
    return classify_region(longitude, latitude, context)
