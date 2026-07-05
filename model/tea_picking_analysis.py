import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_INPUT_DIR = Path(__file__).resolve().parent / (
    "drive-download-20260607T015525Z-3-001"
)
FILE_PATTERN = "Gulin_ERA5Land_GridPoints_Rectangle_*.csv"

RAIN_THRESHOLD_MM = 0.1
RAIN_LOOKBACK_HOURS = 12
RH_THRESHOLD = 85
CURRENT_SOLAR_THRESHOLD_WM2 = 80
DRYING_SOLAR_THRESHOLD_WM2 = 120
MIN_DRYING_SUN_HOURS = 4
MIN_DRYING_SOLAR_ENERGY_MJ = 1.5
AREA_SUITABLE_FRACTION_THRESHOLD = 0.5

REQUIRED_COLS = [
    "pixel_id",
    "lon",
    "lat",
    "inside_polygon",
    "datetime_local",
    "date_local",
    "hour_local",
    "in_season",
    "rain_mm",
    "temp_c",
    "relative_humidity",
    "solar_wm2",
    "solar_elevation_deg",
    "is_daylight",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calculate tea-picking suitability from Gulin ERA5-Land CSV files."
    )
    parser.add_argument(
        "input_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Directory containing yearly CSV files (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="Output directory (default: <input_dir>/tea_picking_outputs)",
    )
    return parser.parse_args()


def load_input(input_dir: Path) -> tuple[pd.DataFrame, list[Path]]:
    input_dir = input_dir.expanduser().resolve()
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input directory does not exist: {input_dir}")

    files = sorted(input_dir.glob(FILE_PATTERN))
    if not files:
        raise FileNotFoundError(
            f"No files matching {FILE_PATTERN!r} were found in {input_dir}"
        )

    print(f"Found {len(files)} input files:")
    for file in files:
        print(f" - {file.name}")

    frames = []
    for file in files:
        frame = pd.read_csv(file)
        missing = [column for column in REQUIRED_COLS if column not in frame.columns]
        if missing:
            raise ValueError(f"{file.name} is missing columns: {missing}")
        frame["source_file"] = file.name
        frames.append(frame)

    df = pd.concat(frames, ignore_index=True)
    print(f"Raw rows: {len(df):,}")
    return df, files


def normalize_grid_ids(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build stable grid IDs from each source file's north-south row and
    west-east column. This prevents small cross-year coordinate shifts from
    being interpreted as additional physical grid points.
    """
    normalized = []
    expected_shape = None

    for source_file, frame in df.groupby("source_file", sort=False):
        frame = frame.copy()
        point_meta = frame[["pixel_id", "lon", "lat", "inside_polygon"]].drop_duplicates()
        lon_values = sorted(point_meta["lon"].unique())
        lat_values = sorted(point_meta["lat"].unique(), reverse=True)
        shape = (len(lat_values), len(lon_values))

        if expected_shape is None:
            expected_shape = shape
        elif shape != expected_shape:
            raise ValueError(
                f"Inconsistent grid shape in {source_file}: {shape}, "
                f"expected {expected_shape}"
            )
        if len(point_meta) != shape[0] * shape[1]:
            raise ValueError(
                f"{source_file} is not a complete rectangular grid: "
                f"{len(point_meta)} points for shape {shape}"
            )

        lon_to_col = {value: index + 1 for index, value in enumerate(lon_values)}
        lat_to_row = {value: index + 1 for index, value in enumerate(lat_values)}
        frame["grid_row"] = frame["lat"].map(lat_to_row).astype(int)
        frame["grid_col"] = frame["lon"].map(lon_to_col).astype(int)
        frame["grid_id"] = (
            "R"
            + frame["grid_row"].astype(str).str.zfill(2)
            + "C"
            + frame["grid_col"].astype(str).str.zfill(2)
        )
        frame = frame.rename(
            columns={
                "pixel_id": "source_pixel_id",
                "lon": "source_lon",
                "lat": "source_lat",
            }
        )
        normalized.append(frame)

    result = pd.concat(normalized, ignore_index=True)
    grid_meta = (
        result[
            [
                "grid_id",
                "grid_row",
                "grid_col",
                "source_file",
                "source_lon",
                "source_lat",
                "inside_polygon",
            ]
        ]
        .drop_duplicates()
        .groupby(["grid_id", "grid_row", "grid_col"])
        .agg(
            lon=("source_lon", "median"),
            lat=("source_lat", "median"),
            inside_values=("inside_polygon", "nunique"),
            inside_polygon_canonical=("inside_polygon", "first"),
        )
        .reset_index()
    )
    inconsistent = grid_meta.loc[grid_meta["inside_values"] != 1, "grid_id"].tolist()
    if inconsistent:
        raise ValueError(
            "inside_polygon changes across years for stable grid IDs: "
            + ", ".join(inconsistent)
        )

    result = result.drop(columns=["inside_polygon"]).merge(
        grid_meta[
            [
                "grid_id",
                "grid_row",
                "grid_col",
                "lon",
                "lat",
                "inside_polygon_canonical",
            ]
        ],
        on=["grid_id", "grid_row", "grid_col"],
        how="left",
        validate="many_to_one",
    )
    result = result.rename(
        columns={
            "grid_id": "pixel_id",
            "inside_polygon_canonical": "inside_polygon",
        }
    )
    print(
        "Stable grid normalization:",
        f"{expected_shape[0]} rows x {expected_shape[1]} columns =",
        f"{result['pixel_id'].nunique()} points",
    )
    return result


def clean_data(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["datetime_local"] = pd.to_datetime(df["datetime_local"], errors="raise")
    df["date_local"] = pd.to_datetime(df["date_local"], errors="raise").dt.date

    numeric_cols = [
        "lon",
        "lat",
        "inside_polygon",
        "hour_local",
        "in_season",
        "rain_mm",
        "temp_c",
        "relative_humidity",
        "solar_wm2",
        "solar_elevation_deg",
        "is_daylight",
    ]
    for column in numeric_cols:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df["rain_mm"] = df["rain_mm"].clip(lower=0)
    df["solar_wm2"] = df["solar_wm2"].clip(lower=0)
    for column in ["inside_polygon", "in_season", "is_daylight"]:
        df[column] = df[column].fillna(0).astype(int)

    return df.sort_values(["pixel_id", "datetime_local"]).reset_index(drop=True)


def validate_grid_points(df: pd.DataFrame) -> None:
    point_meta = (
        df[["pixel_id", "lon", "lat", "inside_polygon"]]
        .drop_duplicates()
        .sort_values("pixel_id")
    )
    n_points = point_meta["pixel_id"].nunique()
    n_polygon_points = point_meta.loc[
        point_meta["inside_polygon"] == 1, "pixel_id"
    ].nunique()

    print(f"Total ERA5 grid points in rectangle: {n_points}")
    print(f"Grid points inside polygon: {n_polygon_points}")
    if n_polygon_points == 0:
        raise ValueError(
            "No ERA5 grid-point centers fall inside the ROI polygon. "
            "Use the rectangle result, nearest grid points, or a polygon buffer."
        )


def calculate_point_suitability(group: pd.DataFrame) -> pd.DataFrame:
    g = group.sort_values("datetime_local").copy()
    g["is_raining"] = (g["rain_mm"] >= RAIN_THRESHOLD_MM).astype(int)
    g["too_humid"] = (g["relative_humidity"] > RH_THRESHOLD).astype(int)
    g["current_enough_sun"] = (
        g["solar_wm2"] >= CURRENT_SOLAR_THRESHOLD_WM2
    ).astype(int)
    g["effective_drying_sun"] = (
        (g["is_daylight"] == 1)
        & (g["solar_wm2"] >= DRYING_SOLAR_THRESHOLD_WM2)
    ).astype(int)
    g["solar_energy_mj"] = g["solar_wm2"] * 3600 / 1_000_000

    datetimes = g["datetime_local"].to_numpy()
    is_rain = g["is_raining"].to_numpy()
    effective = g["effective_drying_sun"].to_numpy()
    energy = g["solar_energy_mj"].to_numpy()
    cum_effective = np.concatenate([[0], np.cumsum(effective)])
    cum_energy = np.concatenate([[0.0], np.cumsum(energy)])

    has_recent_rain = np.zeros(len(g), dtype=int)
    drying_hours = np.full(len(g), 999, dtype=int)
    drying_energy = np.full(len(g), 999.0, dtype=float)
    post_dry_enough = np.ones(len(g), dtype=int)
    last_rain_idx = None

    for i in range(len(g)):
        if is_rain[i] == 1:
            last_rain_idx = i
        if last_rain_idx is None:
            continue

        delta_hours = (
            pd.Timestamp(datetimes[i]) - pd.Timestamp(datetimes[last_rain_idx])
        ).total_seconds() / 3600
        if delta_hours <= RAIN_LOOKBACK_HOURS:
            has_recent_rain[i] = 1
            start = last_rain_idx + 1
            end = i + 1
            hours = int(cum_effective[end] - cum_effective[start])
            energy_mj = float(cum_energy[end] - cum_energy[start])
            drying_hours[i] = hours
            drying_energy[i] = energy_mj
            post_dry_enough[i] = int(
                hours >= MIN_DRYING_SUN_HOURS
                and energy_mj >= MIN_DRYING_SOLAR_ENERGY_MJ
            )

    g["has_recent_rain_12h"] = has_recent_rain
    g["drying_sun_hours_after_last_rain"] = drying_hours
    g["drying_solar_energy_mj_after_last_rain"] = drying_energy
    g["post_rain_dry_enough"] = post_dry_enough
    g["suitable_for_picking"] = (
        (g["is_daylight"] == 1)
        & (g["is_raining"] == 0)
        & (g["too_humid"] == 0)
        & (g["current_enough_sun"] == 1)
        & (g["post_rain_dry_enough"] == 1)
    ).astype(int)
    return g


def build_point_hourly(df: pd.DataFrame) -> pd.DataFrame:
    # Explicit concatenation is stable across pandas 2.x and 3.x groupby changes.
    groups = (
        calculate_point_suitability(group)
        for _, group in df.groupby("pixel_id", sort=False)
    )
    point_hourly = pd.concat(groups, ignore_index=True)
    result = point_hourly.loc[point_hourly["in_season"] == 1].copy()
    print(f"Point-hourly rows in season: {len(result):,}")
    return result


def build_area_hourly(point_hourly: pd.DataFrame) -> pd.DataFrame:
    rectangle = point_hourly.copy()
    rectangle["area_name"] = "ROI2_Bounding_Rectangle"
    polygon = point_hourly.loc[point_hourly["inside_polygon"] == 1].copy()
    polygon["area_name"] = "ROI1_Ancient_Tea_Tree_Polygon"
    area_points = pd.concat([polygon, rectangle], ignore_index=True)

    area_hourly = (
        area_points.groupby(
            ["area_name", "datetime_local", "date_local", "hour_local"]
        )
        .agg(
            n_grid_points=("pixel_id", "nunique"),
            mean_rain_mm=("rain_mm", "mean"),
            total_rain_mm=("rain_mm", "mean"),
            mean_temp_c=("temp_c", "mean"),
            mean_relative_humidity=("relative_humidity", "mean"),
            mean_solar_wm2=("solar_wm2", "mean"),
            mean_solar_elevation_deg=("solar_elevation_deg", "mean"),
            daylight_fraction=("is_daylight", "mean"),
            raining_fraction=("is_raining", "mean"),
            humid_fraction=("too_humid", "mean"),
            current_enough_sun_fraction=("current_enough_sun", "mean"),
            post_rain_dry_enough_fraction=("post_rain_dry_enough", "mean"),
            suitable_fraction=("suitable_for_picking", "mean"),
            mean_drying_sun_hours_after_last_rain=(
                "drying_sun_hours_after_last_rain",
                "mean",
            ),
            mean_drying_solar_energy_mj_after_last_rain=(
                "drying_solar_energy_mj_after_last_rain",
                "mean",
            ),
        )
        .reset_index()
    )
    area_hourly["area_suitable_for_picking"] = (
        area_hourly["suitable_fraction"] >= AREA_SUITABLE_FRACTION_THRESHOLD
    ).astype(int)
    return area_hourly


def build_area_daily(area_hourly: pd.DataFrame) -> pd.DataFrame:
    area_daily = (
        area_hourly.groupby(["area_name", "date_local"])
        .agg(
            n_hours=("hour_local", "count"),
            mean_n_grid_points=("n_grid_points", "mean"),
            daylight_hours=("daylight_fraction", "sum"),
            suitable_hours=("area_suitable_for_picking", "sum"),
            mean_suitable_fraction=("suitable_fraction", "mean"),
            mean_raining_fraction=("raining_fraction", "mean"),
            mean_humid_fraction=("humid_fraction", "mean"),
            mean_post_rain_dry_enough_fraction=(
                "post_rain_dry_enough_fraction",
                "mean",
            ),
            total_rain_mm=("mean_rain_mm", "sum"),
            mean_rain_mm=("mean_rain_mm", "mean"),
            mean_temp_c=("mean_temp_c", "mean"),
            mean_relative_humidity=("mean_relative_humidity", "mean"),
            mean_solar_wm2=("mean_solar_wm2", "mean"),
            mean_solar_elevation_deg=("mean_solar_elevation_deg", "mean"),
        )
        .reset_index()
    )
    area_daily["picking_class"] = np.select(
        [area_daily["suitable_hours"] >= 6, area_daily["suitable_hours"] >= 3],
        ["Good picking day", "Partial picking day"],
        default="Not suitable",
    )
    area_daily["date_local"] = pd.to_datetime(area_daily["date_local"])
    area_daily["year"] = area_daily["date_local"].dt.year
    area_daily["month"] = area_daily["date_local"].dt.month
    area_daily["month_day"] = area_daily["date_local"].dt.strftime("%m-%d")
    return area_daily


def build_historical_cycle(area_daily: pd.DataFrame) -> pd.DataFrame:
    return (
        area_daily.groupby(["area_name", "month_day"])
        .agg(
            mean_daylight_hours=("daylight_hours", "mean"),
            mean_suitable_hours=("suitable_hours", "mean"),
            mean_suitable_fraction=("mean_suitable_fraction", "mean"),
            mean_raining_fraction=("mean_raining_fraction", "mean"),
            mean_humid_fraction=("mean_humid_fraction", "mean"),
            mean_post_rain_dry_enough_fraction=(
                "mean_post_rain_dry_enough_fraction",
                "mean",
            ),
            mean_total_rain_mm=("total_rain_mm", "mean"),
            mean_relative_humidity=("mean_relative_humidity", "mean"),
            mean_solar_wm2=("mean_solar_wm2", "mean"),
            mean_solar_elevation_deg=("mean_solar_elevation_deg", "mean"),
        )
        .reset_index()
    )


def build_grid_summary(point_hourly: pd.DataFrame) -> pd.DataFrame:
    summary = (
        point_hourly.groupby(["pixel_id", "lon", "lat", "inside_polygon"])
        .agg(
            n_hours=("datetime_local", "count"),
            suitable_hours=("suitable_for_picking", "sum"),
            rain_hours=("is_raining", "sum"),
            humid_hours=("too_humid", "sum"),
            mean_rain_mm=("rain_mm", "mean"),
            total_rain_mm=("rain_mm", "sum"),
            mean_relative_humidity=("relative_humidity", "mean"),
            mean_solar_wm2=("solar_wm2", "mean"),
        )
        .reset_index()
    )
    summary["suitable_hour_fraction"] = (
        summary["suitable_hours"] / summary["n_hours"]
    )
    return summary


def infer_year_range(files: list[Path]) -> str:
    years = []
    for file in files:
        match = re.search(r"_(\d{4})(?:_[^.]*)?\.csv$", file.name)
        if match:
            years.append(int(match.group(1)))
    return f"{min(years)}_{max(years)}" if years else "all_years"


def main() -> None:
    args = parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else input_dir / "tea_picking_outputs"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    df, files = load_input(input_dir)
    df = normalize_grid_ids(df)
    df = clean_data(df)
    validate_grid_points(df)
    point_hourly = build_point_hourly(df)
    area_hourly = build_area_hourly(point_hourly)
    area_daily = build_area_daily(area_hourly)
    historical_cycle = build_historical_cycle(area_daily)
    grid_summary = build_grid_summary(point_hourly)

    period = infer_year_range(files)
    outputs = {
        f"Gulin_Tea_Picking_Point_Hourly_{period}.csv": point_hourly,
        f"Gulin_Tea_Picking_Area_Hourly_{period}.csv": area_hourly,
        f"Gulin_Tea_Picking_Area_Daily_{period}.csv": area_daily,
        f"Gulin_Tea_Picking_Historical_Cycle_{period}.csv": historical_cycle,
        f"Gulin_Tea_Picking_GridPoint_Summary_{period}.csv": grid_summary,
    }
    for filename, data in outputs.items():
        path = output_dir / filename
        data.to_csv(path, index=False)
        print(f"Wrote {path} ({len(data):,} rows)")

    print("Done.")


if __name__ == "__main__":
    main()
