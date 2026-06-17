from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
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
    configure_run_logger,
    log_exception,
    log_run_end,
    log_run_start,
    resolve_log_root,
)

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
CAIYUN_PRECIP_COLUMNS = ["precipitation"]
OPENMETEO_PRECIP_COLUMNS = ["precipitation", "rain", "showers"]


@dataclass
class DetectionRecord:
    source: str
    file_path: str
    matched_column: str
    max_precipitation: float
    rainy_row_count: int
    first_rain_time: str | None


@dataclass
class DetectionSummary:
    has_precipitation: bool
    threshold: float
    target_date: str
    checked_files: int
    matched_files: int
    max_precipitation: float
    earliest_rain_time: str | None
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


def detect_precipitation_in_file(
    file_path: Path,
    source: str,
    candidate_columns: list[str],
    threshold: float,
) -> DetectionRecord | None:
    dataframe = load_weather_table(file_path)
    normalized_columns = {str(column).strip().lower(): column for column in dataframe.columns}
    matched_column_name = next((name for name in candidate_columns if name in normalized_columns), None)
    if matched_column_name is None:
        return None

    source_column = normalized_columns[matched_column_name]
    numeric_series = pd.to_numeric(dataframe[source_column], errors="coerce").fillna(0.0)
    rainy_rows = dataframe.loc[numeric_series > threshold]
    if rainy_rows.empty:
        return None

    time_column = pick_time_column(dataframe.columns)
    first_rain_time = None
    if time_column is not None:
        first_rain_time = str(rainy_rows.iloc[0][time_column])

    return DetectionRecord(
        source=source,
        file_path=str(file_path),
        matched_column=matched_column_name,
        max_precipitation=float(numeric_series.max()),
        rainy_row_count=int((numeric_series > threshold).sum()),
        first_rain_time=first_rain_time,
    )


def collect_detection_records(
    caiyun_dir: Path | None,
    openmeteo_dir: Path | None,
    target_date: str,
    threshold: float,
) -> tuple[list[DetectionRecord], int]:
    records: list[DetectionRecord] = []
    checked_files = 0

    for file_path in list_source_files(caiyun_dir, target_date, "*.csv"):
        checked_files += 1
        record = detect_precipitation_in_file(
            file_path=file_path,
            source="caiyun",
            candidate_columns=CAIYUN_PRECIP_COLUMNS,
            threshold=threshold,
        )
        if record is not None:
            records.append(record)

    for file_path in list_source_files(openmeteo_dir, target_date, "*.xlsx"):
        checked_files += 1
        record = detect_precipitation_in_file(
            file_path=file_path,
            source="openmeteo",
            candidate_columns=OPENMETEO_PRECIP_COLUMNS,
            threshold=threshold,
        )
        if record is not None:
            records.append(record)

    return records, checked_files


def build_summary(
    records: list[DetectionRecord],
    checked_files: int,
    threshold: float,
    target_date: str,
) -> DetectionSummary:
    max_precipitation = max((record.max_precipitation for record in records), default=0.0)
    time_candidates = [record.first_rain_time for record in records if record.first_rain_time]
    earliest_rain_time = min(time_candidates) if time_candidates else None
    return DetectionSummary(
        has_precipitation=bool(records),
        threshold=threshold,
        target_date=target_date,
        checked_files=checked_files,
        matched_files=len(records),
        max_precipitation=max_precipitation,
        earliest_rain_time=earliest_rain_time,
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
        target_date=args.date,
        threshold=args.threshold,
        log_dir=args.log_dir,
    )

    try:
        caiyun_dir = resolve_input_dir(args.caiyun_dir)
        openmeteo_dir = resolve_input_dir(args.openmeteo_dir)
        if caiyun_dir is None and openmeteo_dir is None:
            raise ValueError("At least one of --caiyun-dir or --openmeteo-dir must be provided.")

        records, checked_files = collect_detection_records(
            caiyun_dir=caiyun_dir,
            openmeteo_dir=openmeteo_dir,
            target_date=args.date,
            threshold=args.threshold,
        )
        summary = build_summary(
            records=records,
            checked_files=checked_files,
            threshold=args.threshold,
            target_date=args.date,
        )
        payload = asdict(summary)

        log_run_end(
            logger,
            "Precipitation checker completed",
            has_precipitation=summary.has_precipitation,
            checked_files=summary.checked_files,
            matched_files=summary.matched_files,
            max_precipitation=summary.max_precipitation,
            earliest_rain_time=summary.earliest_rain_time,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        log_exception(logger, "Precipitation checker failed", exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
