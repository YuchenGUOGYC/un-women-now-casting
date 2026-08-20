from __future__ import annotations

import csv
import json
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

DEFAULT_REGION_GRID_PATH = Path(__file__).resolve().parent.parent / "study area" / "study_area_region_grid.geojson"

REGION_GRID_LON_EDGES = [
    105.63526264470082,
    105.660263,
    105.710263,
    105.760263,
    105.810263,
    105.860263,
    105.910263,
    105.960263,
    105.99369159977894,
]
REGION_GRID_LAT_EDGES = [
    28.0314987256145,
    28.056499,
    28.106499,
    28.156499,
    28.206499,
    28.256499,
    28.306499,
    28.356499,
    28.406499,
]
REGION_GRID_NAMES = [
    ["外围-西部", "外围-西部", "城镇区域（南）", "城镇区域（南）", "城镇区域（南）", "城镇区域（南）", "外围-东部", "外围-东部"],
    ["外围-西部", "外围-西部", "外围-西部", "外围-南部", "茶林区域-东南", "外围-东部", "外围-东部", "外围-东部"],
    ["外围-西部", "外围-西部", "外围-西部", "茶林区域-南部", "茶林区域-东南", "茶林区域-东南", "外围-东部", "外围-东部"],
    ["外围-西部", "外围-西部", "茶林区域-西部", "茶林区域（中）", "茶林区域-东部", "茶林区域-东部", "外围-东部", "外围-东部"],
    ["外围-西部", "外围-西部", "茶林区域-西部", "茶林区域（中）", "茶林区域-东部", "茶林区域-东部", "外围-东部", "外围-东部"],
    ["外围-西部", "外围-西部", "茶林区域-西北", "茶林区域-北部", "茶林区域-东北", "茶林区域-东北", "外围-东部", "外围-东部"],
    ["外围-北部", "茶林区域-西北", "茶林区域-西北", "茶林区域-北部", "茶林区域-东北", "茶林区域-东北", "外围-北部", "外围-北部"],
    ["外围-北部", "外围-北部", "外围-北部", "外围-北部", "外围-北部", "外围-北部", "外围-北部", "外围-北部"],
]


@dataclass
class RegionCell:
    region_name: str
    polygon: list[tuple[float, float]]
    min_lon: float
    max_lon: float
    min_lat: float
    max_lat: float


@dataclass
class RegionContext:
    core_min_lon: float
    core_max_lon: float
    core_min_lat: float
    core_max_lat: float
    center_lon: float
    center_lat: float
    coordinate_region_map: dict[tuple[float, float], str]
    region_cells: list[RegionCell]


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


def point_on_segment(
    longitude: float,
    latitude: float,
    start: tuple[float, float],
    end: tuple[float, float],
    tolerance: float = 1e-9,
) -> bool:
    lon_1, lat_1 = start
    lon_2, lat_2 = end
    cross = (longitude - lon_1) * (lat_2 - lat_1) - (latitude - lat_1) * (lon_2 - lon_1)
    if abs(cross) > tolerance:
        return False
    return (
        min(lon_1, lon_2) - tolerance <= longitude <= max(lon_1, lon_2) + tolerance
        and min(lat_1, lat_2) - tolerance <= latitude <= max(lat_1, lat_2) + tolerance
    )


def point_in_polygon(longitude: float, latitude: float, polygon: list[tuple[float, float]]) -> bool:
    inside = False
    previous_index = len(polygon) - 1
    for index, (lon_i, lat_i) in enumerate(polygon):
        lon_j, lat_j = polygon[previous_index]
        if point_on_segment(longitude, latitude, (lon_i, lat_i), (lon_j, lat_j)):
            return True
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


def _extract_polygon_rings(geometry: dict) -> list[list[tuple[float, float]]]:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates", [])
    if geometry_type == "Polygon":
        return [[(float(lon), float(lat)) for lon, lat in coordinates[0]]]
    if geometry_type == "MultiPolygon":
        return [[(float(lon), float(lat)) for lon, lat in polygon[0]] for polygon in coordinates]
    return []


def _make_region_cell(region_name: str, west: float, south: float, east: float, north: float) -> RegionCell:
    polygon = [(west, north), (east, north), (east, south), (west, south), (west, north)]
    return RegionCell(
        region_name=region_name,
        polygon=polygon,
        min_lon=west,
        max_lon=east,
        min_lat=south,
        max_lat=north,
    )


def build_default_region_grid_cells() -> list[RegionCell]:
    cells: list[RegionCell] = []
    for row_index, region_row in enumerate(REGION_GRID_NAMES):
        south = REGION_GRID_LAT_EDGES[row_index]
        north = REGION_GRID_LAT_EDGES[row_index + 1]
        for col_index, region_name in enumerate(region_row):
            west = REGION_GRID_LON_EDGES[col_index]
            east = REGION_GRID_LON_EDGES[col_index + 1]
            cells.append(_make_region_cell(region_name, west, south, east, north))
    return cells


def load_region_cells(region_grid_file: str | Path | None = None) -> list[RegionCell]:
    grid_path = Path(region_grid_file).expanduser() if region_grid_file else DEFAULT_REGION_GRID_PATH
    if not grid_path.exists():
        return build_default_region_grid_cells()

    data = json.loads(grid_path.read_text(encoding="utf-8"))
    cells: list[RegionCell] = []
    for feature in data.get("features", []):
        properties = feature.get("properties") or {}
        region_name = properties.get("region_name") or properties.get("region") or properties.get("area_name")
        if not isinstance(region_name, str) or not region_name.strip():
            continue
        for polygon in _extract_polygon_rings(feature.get("geometry") or {}):
            if len(polygon) < 3:
                continue
            longitudes = [lon for lon, _ in polygon]
            latitudes = [lat for _, lat in polygon]
            cells.append(
                RegionCell(
                    region_name=region_name.strip(),
                    polygon=polygon,
                    min_lon=min(longitudes),
                    max_lon=max(longitudes),
                    min_lat=min(latitudes),
                    max_lat=max(latitudes),
                )
            )
    return cells or build_default_region_grid_cells()


def _build_default_context(region_cells: list[RegionCell] | None = None) -> RegionContext:
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
        region_cells=region_cells if region_cells is not None else load_region_cells(),
    )


def _build_core_bounds_from_rows(rows: list[dict], region_cells: list[RegionCell]) -> RegionContext:
    core_points: list[tuple[float, float]] = []
    for row in rows:
        latitude = float(row["latitude"])
        longitude = float(row["longitude"])
        if point_in_polygon(longitude, latitude, CORE_POLYGON):
            core_points.append((longitude, latitude))

    if not core_points:
        return _build_default_context(region_cells)

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
        region_cells=region_cells,
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


def _classify_region_grid(longitude: float, latitude: float, context: RegionContext) -> str | None:
    for cell in context.region_cells:
        if not (cell.min_lon <= longitude <= cell.max_lon and cell.min_lat <= latitude <= cell.max_lat):
            continue
        if point_in_polygon(longitude, latitude, cell.polygon):
            return cell.region_name
    return None


def classify_region(longitude: float, latitude: float, context: RegionContext) -> str:
    grid_region = _classify_region_grid(longitude, latitude, context)
    if grid_region:
        return grid_region
    if point_in_polygon(longitude, latitude, CORE_POLYGON):
        return _classify_core_region(longitude, latitude, context)
    return _classify_outer_region(longitude, latitude, context)


def build_region_context(
    coord_file: str | Path | None = None,
    region_grid_file: str | Path | None = None,
) -> RegionContext:
    region_cells = load_region_cells(region_grid_file)
    if coord_file is None:
        return _build_default_context(region_cells)

    input_path = Path(coord_file).expanduser()
    rows = _read_coordinate_rows(input_path)
    context = _build_core_bounds_from_rows(rows, region_cells)
    coordinate_region_map: dict[tuple[float, float], str] = {}
    for row in rows:
        latitude = round_coord(float(row["latitude"]))
        longitude = round_coord(float(row["longitude"]))
        grid_region = _classify_region_grid(longitude, latitude, context)
        coordinate_region_map[(latitude, longitude)] = (
            grid_region
            or _pick_region_column(row)
            or ("城镇区域（南）" if is_town_south_point(row) else classify_region(longitude, latitude, context))
        )
    context.coordinate_region_map = coordinate_region_map
    return context


def get_region_name(longitude: float, latitude: float, context: RegionContext) -> str:
    key = (round_coord(latitude), round_coord(longitude))
    if key in context.coordinate_region_map:
        return context.coordinate_region_map[key]
    return classify_region(longitude, latitude, context)
