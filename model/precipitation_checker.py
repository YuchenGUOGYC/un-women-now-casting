from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common import (
    add_log_dir_argument,
    build_region_context,
    configure_run_logger,
    get_region_name,
    log_exception,
    log_run_end,
    log_run_start,
    resolve_log_root,
)

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
CAIYUN_PRECIP_COLUMNS = ["precipitation"]
OPENMETEO_PRECIP_COLUMNS = ["precipitation", "rain", "showers"]
FILENAME_COORD_PATTERN = re.compile(r"lat(?P<lat>[mp\d]+)_lon(?P<lon>[mp\d]+)")
SEVERITY_LEVELS = [
    (50.0, "暴雨"),
    (25.0, "大雨"),
    (10.0, "中雨"),
    (0.0, "小雨"),
]


@dataclass
class DetectionRecord:
    source: str
    file_path: str
    region_name: str
    matched_column: str
    max_precipitation: float
    rainy_row_count: int
    first_rain_time: str | None


@dataclass
class RegionRainWindow:
    region_name: str
    start_time: str
    end_time: str
    accumulated_precipitation: float
    severity: str
    affected_point_count: int
    sources: list[str]


@dataclass
class DetectionSummary:
    has_precipitation: bool
    threshold: float
    target_date: str
    checked_files: int
    matched_files: int
    max_precipitation: float
    highest_severity: str | None
    earliest_rain_time: str | None
    latest_rain_time: str | None
    hit_regions: list[str]
    region_windows: list[RegionRainWindow]
    records: list[DetectionRecord]


def get_beijing_today() -> str:
    return pd.Timestamp.now(tz=BEIJING_TZ).strftime("%Y-%m-%d")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check whether downloaded weather data indicates precipitation.")
    parser.add_argument("--caiyun-dir", help="Root output directory for Caiyun batch results.")
    parser.add_argument("--openmeteo-dir", help="Root output directory for Open-Meteo batch results.")
    parser.add_argument(
        "--date",
        default=get_beijing_today(),
        help="Date folder to scan in YYYY-MM-DD format. Default: today in Asia/Shanghai.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.0,
        help="Precipitation threshold in mm. Values greater than this are treated as rain. Default: 0.0",
    )
    parser.add_argument("--coord-file", help="Optional coordinate Excel/CSV used to build region mapping.")
    add_log_dir_argument(parser, ROOT_DIR / "logs")
    return parser.parse_args()


def resolve_input_dir(path_text: str | None) -> Path | None:
    if not path_text:
        return None
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def list_source_files(base_dir: Path | None, target_date: str, pattern: str) -> list[Path]:
    if base_dir is None:
        return []
    dated_dir = base_dir / target_date
    if not dated_dir.exists():
        return []
    return sorted(dated_dir.glob(pattern))


def load_weather_table(file_path: Path) -> pd.DataFrame:
    if file_path.suffix.lower() == ".csv":
        return pd.read_csv(file_path)
    if file_path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(file_path)
    raise ValueError(f"Unsupported weather file format: {file_path}")


def pick_time_column(columns: Iterable[str]) -> str | None:
    normalized = {str(column).strip().lower(): column for column in columns}
    for candidate in ["date_local", "date_local_iso", "datetime", "date", "time"]:
        if candidate in normalized:
            return str(normalized[candidate])
    return None


def decode_filename_coord(text: str) -> float:
    return float(text.replace("m", "-").replace("p", "."))


def extract_query_coordinates(file_path: Path, dataframe: pd.DataFrame) -> tuple[float, float]:
    match = FILENAME_COORD_PATTERN.search(file_path.stem)
    if match:
        return decode_filename_coord(match.group("lon")), decode_filename_coord(match.group("lat"))

    normalized_columns = {str(column).strip().lower(): column for column in dataframe.columns}
    if "longitude" in normalized_columns and "latitude" in normalized_columns and not dataframe.empty:
        longitude = float(dataframe.iloc[0][normalized_columns["longitude"]])
        latitude = float(dataframe.iloc[0][normalized_columns["latitude"]])
        return longitude, latitude

    raise ValueError(f"Could not determine coordinates for file: {file_path}")


def get_severity_name(value: float) -> str:
    for threshold, label in SEVERITY_LEVELS:
        if value >= threshold:
            return label
    return "小雨"


def normalize_timestamp_value(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        return timestamp.tz_convert(BEIJING_TZ).tz_localize(None)
    return timestamp


def detect_precipitation_in_file(
    file_path: Path,
    source: str,
    candidate_columns: list[str],
    threshold: float,
    region_context,
) -> tuple[DetectionRecord | None, list[dict]]:
    dataframe = load_weather_table(file_path)
    normalized_columns = {str(column).strip().lower(): column for column in dataframe.columns}
    matched_column_name = next((name for name in candidate_columns if name in normalized_columns), None)
    if matched_column_name is None:
        return None, []

    source_column = normalized_columns[matched_column_name]
    numeric_series = pd.to_numeric(dataframe[source_column], errors="coerce").fillna(0.0)
    rainy_rows = dataframe.loc[numeric_series > threshold]
    if rainy_rows.empty:
        return None, []

    time_column = pick_time_column(dataframe.columns)
    first_rain_time = None
    if time_column is not None:
        first_rain_time = str(rainy_rows.iloc[0][time_column])

    longitude, latitude = extract_query_coordinates(file_path, dataframe)
    region_name = get_region_name(longitude, latitude, region_context)

    rainy_hits: list[dict] = []
    if time_column is not None:
        for row_index, row in rainy_rows.iterrows():
            rainy_hits.append(
                {
                    "source": source,
                    "region_name": region_name,
                    "file_path": str(file_path),
                    "timestamp": normalize_timestamp_value(row[time_column]),
                    "precipitation": float(numeric_series.loc[row_index]),
                    "longitude": longitude,
                    "latitude": latitude,
                }
            )

    return (
        DetectionRecord(
            source=source,
            file_path=str(file_path),
            region_name=region_name,
            matched_column=matched_column_name,
            max_precipitation=float(numeric_series.max()),
            rainy_row_count=int((numeric_series > threshold).sum()),
            first_rain_time=first_rain_time,
        ),
        rainy_hits,
    )


def build_region_windows(rainy_hits: list[dict]) -> list[RegionRainWindow]:
    if not rainy_hits:
        return []

    dataframe = pd.DataFrame(rainy_hits)
    grouped = (
        dataframe.groupby(["region_name", "timestamp"], as_index=False)
        .agg(
            hour_precipitation=("precipitation", "max"),
            affected_point_count=("file_path", "nunique"),
            sources=("source", lambda values: sorted(set(values))),
        )
        .sort_values(["region_name", "timestamp"])
    )

    windows: list[RegionRainWindow] = []
    for region_name, region_rows in grouped.groupby("region_name"):
        current_rows = []
        previous_timestamp = None
        for row in region_rows.itertuples(index=False):
            current_timestamp = pd.Timestamp(row.timestamp)
            if previous_timestamp is None or current_timestamp - previous_timestamp <= timedelta(hours=1):
                current_rows.append(row)
            else:
                windows.append(_build_window_from_rows(region_name, current_rows))
                current_rows = [row]
            previous_timestamp = current_timestamp
        if current_rows:
            windows.append(_build_window_from_rows(region_name, current_rows))

    return sorted(windows, key=lambda item: (item.start_time, item.region_name))


def _build_window_from_rows(region_name: str, rows: list) -> RegionRainWindow:
    start_timestamp = pd.Timestamp(rows[0].timestamp)
    end_timestamp = pd.Timestamp(rows[-1].timestamp)
    accumulated_precipitation = sum(float(row.hour_precipitation) for row in rows)
    sources = sorted({source for row in rows for source in row.sources})
    affected_point_count = max(int(row.affected_point_count) for row in rows)
    return RegionRainWindow(
        region_name=region_name,
        start_time=start_timestamp.isoformat(),
        end_time=end_timestamp.isoformat(),
        accumulated_precipitation=accumulated_precipitation,
        severity=get_severity_name(accumulated_precipitation),
        affected_point_count=affected_point_count,
        sources=sources,
    )


def collect_detection_records(
    caiyun_dir: Path | None,
    openmeteo_dir: Path | None,
    target_date: str,
    threshold: float,
    coord_file: str | None = None,
) -> tuple[list[DetectionRecord], int, list[dict]]:
    records: list[DetectionRecord] = []
    checked_files = 0
    rainy_hits: list[dict] = []
    region_context = build_region_context(coord_file)

    for file_path in list_source_files(caiyun_dir, target_date, "*.csv"):
        checked_files += 1
        record, file_hits = detect_precipitation_in_file(
            file_path=file_path,
            source="caiyun",
            candidate_columns=CAIYUN_PRECIP_COLUMNS,
            threshold=threshold,
            region_context=region_context,
        )
        if record is not None:
            records.append(record)
            rainy_hits.extend(file_hits)

    for file_path in list_source_files(openmeteo_dir, target_date, "*.xlsx"):
        checked_files += 1
        record, file_hits = detect_precipitation_in_file(
            file_path=file_path,
            source="openmeteo",
            candidate_columns=OPENMETEO_PRECIP_COLUMNS,
            threshold=threshold,
            region_context=region_context,
        )
        if record is not None:
            records.append(record)
            rainy_hits.extend(file_hits)

    return records, checked_files, rainy_hits


def build_summary(
    records: list[DetectionRecord],
    checked_files: int,
    threshold: float,
    target_date: str,
    rainy_hits: list[dict],
) -> DetectionSummary:
    max_precipitation = max((record.max_precipitation for record in records), default=0.0)
    region_windows = build_region_windows(rainy_hits)
    hit_regions = sorted({window.region_name for window in region_windows})
    highest_severity = None
    if region_windows:
        highest_severity = max(region_windows, key=lambda item: item.accumulated_precipitation).severity
    timestamp_candidates = [pd.Timestamp(hit["timestamp"]) for hit in rainy_hits]
    earliest_rain_time = min(timestamp_candidates).isoformat() if timestamp_candidates else None
    latest_rain_time = max(timestamp_candidates).isoformat() if timestamp_candidates else None
    return DetectionSummary(
        has_precipitation=bool(records),
        threshold=threshold,
        target_date=target_date,
        checked_files=checked_files,
        matched_files=len(records),
        max_precipitation=max_precipitation,
        highest_severity=highest_severity,
        earliest_rain_time=earliest_rain_time,
        latest_rain_time=latest_rain_time,
        hit_regions=hit_regions,
        region_windows=region_windows,
        records=records,
    )


def main() -> int:
    args = parse_args()
    logger = configure_run_logger(
        "model.precipitation_checker",
        resolve_log_root(args.log_dir),
        run_name="precipitation_checker",
    )
    log_run_start(
        logger,
        "Precipitation checker started",
        caiyun_dir=args.caiyun_dir,
        openmeteo_dir=args.openmeteo_dir,
        coord_file=args.coord_file,
        target_date=args.date,
        threshold=args.threshold,
        log_dir=args.log_dir,
    )

    try:
        caiyun_dir = resolve_input_dir(args.caiyun_dir)
        openmeteo_dir = resolve_input_dir(args.openmeteo_dir)
        if caiyun_dir is None and openmeteo_dir is None:
            raise ValueError("At least one of --caiyun-dir or --openmeteo-dir must be provided.")

        records, checked_files, rainy_hits = collect_detection_records(
            caiyun_dir=caiyun_dir,
            openmeteo_dir=openmeteo_dir,
            target_date=args.date,
            threshold=args.threshold,
            coord_file=args.coord_file,
        )
        summary = build_summary(
            records=records,
            checked_files=checked_files,
            threshold=args.threshold,
            target_date=args.date,
            rainy_hits=rainy_hits,
        )
        payload = asdict(summary)

        log_run_end(
            logger,
            "Precipitation checker completed",
            has_precipitation=summary.has_precipitation,
            checked_files=summary.checked_files,
            matched_files=summary.matched_files,
            max_precipitation=summary.max_precipitation,
            highest_severity=summary.highest_severity,
            earliest_rain_time=summary.earliest_rain_time,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        log_exception(logger, "Precipitation checker failed", exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
