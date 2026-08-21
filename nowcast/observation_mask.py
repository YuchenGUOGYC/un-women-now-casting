from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


FILENAME_COORD_PATTERN = re.compile(r"lat(?P<lat>[mp\d]+)_lon(?P<lon>[mp\d]+)")
DEFAULT_MASK_COLUMNS = (
    "precipitation",
    "rain",
    "showers",
    "precipitation_probability",
)


@dataclass(frozen=True)
class MergeStats:
    file_count: int
    row_count: int
    observed_row_count: int
    masked_row_count: int
    unmatched_point_files: tuple[str, ...]


def binary_precipitation_mask(
    observed_precip_mm: pd.Series,
    *,
    threshold_mm: float = 0.0,
) -> pd.Series:
    """Return 1 where observed rain exceeds the threshold, otherwise 0.

    This is intentionally the only policy function used to create the mask. A
    later change (for example, a soft mask or a coverage rule) can be made here
    without changing the time/coordinate alignment code.
    """
    numeric = pd.to_numeric(observed_precip_mm, errors="coerce")
    return (numeric > threshold_mm).astype("int8")


def _decode_coord(text: str) -> float:
    return float(text.replace("m", "-").replace("p", "."))


def forecast_file_coordinates(path: Path, dataframe: pd.DataFrame) -> tuple[float, float]:
    match = FILENAME_COORD_PATTERN.search(path.stem)
    if match:
        return _decode_coord(match.group("lon")), _decode_coord(match.group("lat"))

    columns = {str(column).strip().lower(): column for column in dataframe.columns}
    if {"longitude", "latitude"}.issubset(columns) and not dataframe.empty:
        return (
            float(dataframe.iloc[0][columns["longitude"]]),
            float(dataframe.iloc[0][columns["latitude"]]),
        )
    raise ValueError(f"Cannot determine forecast coordinates from {path}")


def normalize_local_hour(values: pd.Series, timezone: str) -> pd.Series:
    zone = ZoneInfo(timezone)

    def normalize(value: object) -> pd.Timestamp:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert(zone).tz_localize(None)
        return timestamp.floor("h")

    return values.map(normalize)


def forecast_local_hours(dataframe: pd.DataFrame, timezone: str) -> pd.Series:
    columns = {str(column).strip().lower(): column for column in dataframe.columns}
    for candidate in ("date_local_iso", "date_local", "datetime_local", "datetime", "date", "time"):
        if candidate in columns:
            return normalize_local_hour(dataframe[columns[candidate]], timezone)
    raise ValueError("Forecast table has no supported local time column")


def load_radar_point_hours(paths: Iterable[Path], timezone: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in paths:
        dataframe = pd.read_csv(path)
        required = {"hour", "point_lon", "point_lat", "hourly_precip_mm"}
        missing = required - set(dataframe.columns)
        if missing:
            raise ValueError(f"Radar file {path} is missing columns: {', '.join(sorted(missing))}")
        dataframe = dataframe.copy()
        dataframe["observation_hour"] = normalize_local_hour(dataframe["hour"], timezone)
        frames.append(dataframe)

    if not frames:
        raise ValueError("No radar point-hour CSV files were provided")

    radar = pd.concat(frames, ignore_index=True)
    radar["point_lon"] = pd.to_numeric(radar["point_lon"], errors="coerce")
    radar["point_lat"] = pd.to_numeric(radar["point_lat"], errors="coerce")
    radar["hourly_precip_mm"] = pd.to_numeric(radar["hourly_precip_mm"], errors="coerce")
    radar = radar.dropna(subset=["point_lon", "point_lat", "observation_hour", "hourly_precip_mm"])
    duplicate_keys = radar.duplicated(["point_lon", "point_lat", "observation_hour"], keep=False)
    if duplicate_keys.any():
        radar = (
            radar.groupby(["point_lon", "point_lat", "observation_hour"], as_index=False)
            .agg(
                hourly_precip_mm=("hourly_precip_mm", "max"),
                image_count_in_hour=("image_count_in_hour", "max")
                if "image_count_in_hour" in radar.columns
                else ("hourly_precip_mm", "size"),
            )
        )
    return radar


def _nearest_radar_point(
    radar: pd.DataFrame,
    longitude: float,
    latitude: float,
    tolerance_degrees: float,
) -> tuple[float, float] | None:
    points = radar[["point_lon", "point_lat"]].drop_duplicates().copy()
    points["distance"] = ((points["point_lon"] - longitude) ** 2 + (points["point_lat"] - latitude) ** 2) ** 0.5
    if points.empty:
        return None
    nearest = points.sort_values("distance").iloc[0]
    if float(nearest["distance"]) > tolerance_degrees:
        return None
    return float(nearest["point_lon"]), float(nearest["point_lat"])


def merge_forecast_file(
    forecast_path: Path,
    output_path: Path,
    radar: pd.DataFrame,
    *,
    window_start: pd.Timestamp,
    run_started_at: pd.Timestamp,
    timezone: str = "Asia/Shanghai",
    mask_columns: Iterable[str] = DEFAULT_MASK_COLUMNS,
    threshold_mm: float = 0.0,
    coordinate_tolerance: float = 0.001,
    mask_builder: Callable[..., pd.Series] = binary_precipitation_mask,
) -> tuple[int, int, bool]:
    forecast = pd.read_excel(forecast_path)
    longitude, latitude = forecast_file_coordinates(forecast_path, forecast)
    point = _nearest_radar_point(radar, longitude, latitude, coordinate_tolerance)

    forecast = forecast.copy()
    forecast_hours = forecast_local_hours(forecast, timezone)
    forecast["radar_observation_hour"] = forecast_hours
    forecast["radar_hourly_precip_mm"] = pd.NA
    forecast["radar_precip_mask"] = pd.NA
    forecast["radar_image_count_in_hour"] = pd.NA
    forecast["radar_observation_applied"] = False

    observed_count = 0
    masked_count = 0
    if point is not None:
        point_radar = radar.loc[
            (radar["point_lon"] == point[0]) & (radar["point_lat"] == point[1])
        ].copy()
        lookup = point_radar.set_index("observation_hour")
        precipitation = forecast_hours.map(lookup["hourly_precip_mm"])
        image_counts = (
            forecast_hours.map(lookup["image_count_in_hour"])
            if "image_count_in_hour" in lookup.columns
            else pd.Series(pd.NA, index=forecast.index)
        )
        eligible = (
            precipitation.notna()
            & (forecast_hours >= window_start.tz_localize(None))
            & (forecast_hours < run_started_at.tz_localize(None))
        )
        masks = mask_builder(precipitation, threshold_mm=threshold_mm)
        forecast.loc[eligible, "radar_hourly_precip_mm"] = precipitation.loc[eligible]
        forecast.loc[eligible, "radar_precip_mask"] = masks.loc[eligible]
        forecast.loc[eligible, "radar_image_count_in_hour"] = image_counts.loc[eligible]
        forecast.loc[eligible, "radar_observation_applied"] = True
        observed_count = int(eligible.sum())
        masked_count = int((eligible & (masks == 0)).sum())

        for column in mask_columns:
            if column not in forecast.columns:
                continue
            original_column = f"forecast_original_{column}"
            forecast[original_column] = forecast[column]
            numeric = pd.to_numeric(forecast[column], errors="coerce")
            forecast.loc[eligible, column] = numeric.loc[eligible] * masks.loc[eligible]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    forecast.to_excel(output_path, index=False)
    return observed_count, masked_count, point is not None


def merge_forecast_directory(
    forecast_dir: Path,
    output_dir: Path,
    radar: pd.DataFrame,
    **kwargs: object,
) -> MergeStats:
    files = sorted(forecast_dir.glob("*.xlsx"))
    if not files:
        raise ValueError(f"No Open-Meteo .xlsx files found in {forecast_dir}")

    total_rows = 0
    total_observed = 0
    total_masked = 0
    unmatched: list[str] = []
    for path in files:
        row_count = len(pd.read_excel(path, usecols=[0]))
        observed, masked, matched = merge_forecast_file(path, output_dir / path.name, radar, **kwargs)
        total_rows += row_count
        total_observed += observed
        total_masked += masked
        if not matched:
            unmatched.append(path.name)

    return MergeStats(
        file_count=len(files),
        row_count=total_rows,
        observed_row_count=total_observed,
        masked_row_count=total_masked,
        unmatched_point_files=tuple(unmatched),
    )

