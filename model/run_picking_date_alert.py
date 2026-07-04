from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
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
from wxpusher.wxpusher_notify import ConfigError, send_notification

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
FILENAME_COORD_PATTERN = re.compile(r"lat(?P<lat>[mp\d]+)_lon(?P<lon>[mp\d]+)")


@dataclass
class RegionPickingSummary:
    region_name: str
    suitable: bool
    suitable_hour_count: int
    suitable_windows: list[str]
    total_rain_mm: float
    max_lookback_rain_mm: float
    mean_temperature_c: float | None
    mean_relative_humidity: float | None
    max_solar_radiation_wm2: float | None
    reasons: list[str]


@dataclass
class PickingDateSummary:
    target_date: str
    has_suitable_region: bool
    suitable_regions: list[str]
    checked_files: int
    matched_files: int
    region_summaries: list[RegionPickingSummary]


def get_beijing_today() -> str:
    return pd.Timestamp.now(tz=BEIJING_TZ).strftime("%Y-%m-%d")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate tea-picking suitability for a Beijing-time day and optionally send a WxPusher alert."
    )
    parser.add_argument("--caiyun-dir", help="Root output directory for Caiyun batch results.")
    parser.add_argument("--openmeteo-dir", help="Root output directory for Open-Meteo batch results.")
    parser.add_argument(
        "--date",
        default=get_beijing_today(),
        help="Download date folder to scan in YYYY-MM-DD format. Default: today in Asia/Shanghai.",
    )
    parser.add_argument(
        "--target-date",
        default=get_beijing_today(),
        help="Beijing-time date to evaluate in YYYY-MM-DD format. Default: today in Asia/Shanghai.",
    )
    parser.add_argument("--coord-file", help="Optional coordinate Excel/CSV used to build region mapping.")
    parser.add_argument("--picking-start-hour", type=int, default=8, help="First local picking hour. Default: 8.")
    parser.add_argument("--picking-end-hour", type=int, default=17, help="Last local picking hour. Default: 17.")
    parser.add_argument(
        "--rain-lookback-hours",
        type=int,
        default=12,
        help="Hours of rain lookback before each picking hour. Default: 12.",
    )
    parser.add_argument("--max-hourly-rain", type=float, default=0.1, help="Maximum rain in picking hour, mm.")
    parser.add_argument("--max-lookback-rain", type=float, default=0.5, help="Maximum lookback rain sum, mm.")
    parser.add_argument("--min-temperature", type=float, default=10.0, help="Minimum suitable temperature, C.")
    parser.add_argument("--max-temperature", type=float, default=30.0, help="Maximum suitable temperature, C.")
    parser.add_argument("--max-relative-humidity", type=float, default=90.0, help="Maximum suitable RH, percent.")
    parser.add_argument(
        "--min-solar-radiation",
        type=float,
        default=20.0,
        help="Minimum suitable shortwave radiation, W/m2. Default: 20.",
    )
    parser.add_argument(
        "--min-suitable-hours",
        type=int,
        default=3,
        help="Minimum suitable hours required for a region to be marked suitable. Default: 3.",
    )
    parser.add_argument(
        "--wxpusher-config",
        default=str(ROOT_DIR / "wxpusher" / "wxpusher.config.json"),
        help="Path to WxPusher JSON or YAML config.",
    )
    parser.add_argument("--title", default="采茶适宜性提醒", help="Notification title.")
    parser.add_argument("--send", action="store_true", help="Send WxPusher notification. Default: only print JSON.")
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


def normalize_local_timestamp(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        return timestamp.tz_convert(BEIJING_TZ).tz_localize(None)
    return timestamp


def pick_column(columns: Iterable[str], candidates: list[str]) -> str | None:
    normalized = {str(column).strip().lower(): column for column in columns}
    for candidate in candidates:
        if candidate in normalized:
            return str(normalized[candidate])
    return None


def normalize_relative_humidity(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.dropna().empty:
        return numeric
    if numeric.max(skipna=True) <= 1.5:
        return numeric * 100
    return numeric


def collect_hourly_records(
    caiyun_dir: Path | None,
    openmeteo_dir: Path | None,
    download_date: str,
    target_date: str,
    coord_file: str | None,
) -> tuple[pd.DataFrame, int, int]:
    records: list[dict] = []
    checked_files = 0
    matched_files = 0
    region_context = build_region_context(coord_file)
    start_time = pd.Timestamp(target_date)
    end_time = start_time + pd.Timedelta(days=1)

    for file_path in list_source_files(caiyun_dir, download_date, "*.csv"):
        checked_files += 1
        dataframe = load_weather_table(file_path)
        file_records = extract_file_records(file_path, dataframe, "caiyun", region_context, start_time, end_time)
        if file_records:
            matched_files += 1
            records.extend(file_records)

    for file_path in list_source_files(openmeteo_dir, download_date, "*.xlsx"):
        checked_files += 1
        dataframe = load_weather_table(file_path)
        file_records = extract_file_records(file_path, dataframe, "openmeteo", region_context, start_time, end_time)
        if file_records:
            matched_files += 1
            records.extend(file_records)

    return pd.DataFrame(records), checked_files, matched_files


def extract_file_records(
    file_path: Path,
    dataframe: pd.DataFrame,
    source: str,
    region_context,
    start_time: pd.Timestamp,
    end_time: pd.Timestamp,
) -> list[dict]:
    time_column = pick_column(dataframe.columns, ["date_local", "datetime_local", "datetime", "date", "time"])
    if time_column is None:
        return []

    longitude, latitude = extract_query_coordinates(file_path, dataframe)
    region_name = get_region_name(longitude, latitude, region_context)
    timestamps = dataframe[time_column].map(normalize_local_timestamp)
    filtered = dataframe.loc[(timestamps >= start_time) & (timestamps < end_time)].copy()
    filtered_timestamps = timestamps.loc[filtered.index]
    if filtered.empty:
        return []

    if source == "openmeteo":
        columns = {
            "rain_mm": pick_column(filtered.columns, ["rain", "precipitation"]),
            "temperature_c": pick_column(filtered.columns, ["temperature_2m", "temperature", "temp_c"]),
            "relative_humidity": pick_column(filtered.columns, ["relative_humidity_2m", "humidity", "relative_humidity"]),
            "solar_radiation_wm2": pick_column(filtered.columns, ["shortwave_radiation", "solar_radiation", "solar_wm2"]),
        }
    else:
        columns = {
            "rain_mm": pick_column(filtered.columns, ["precipitation", "rain"]),
            "temperature_c": pick_column(filtered.columns, ["temperature", "temperature_2m", "temp_c"]),
            "relative_humidity": pick_column(filtered.columns, ["humidity", "relative_humidity", "relative_humidity_2m"]),
            "solar_radiation_wm2": pick_column(filtered.columns, ["solar_radiation", "shortwave_radiation", "solar_wm2"]),
        }

    output: list[dict] = []
    for row_index, row in filtered.iterrows():
        record = {
            "source": source,
            "file_path": str(file_path),
            "region_name": region_name,
            "timestamp": filtered_timestamps.loc[row_index],
            "longitude": longitude,
            "latitude": latitude,
        }
        for metric, column in columns.items():
            if column is None:
                record[metric] = None
            elif metric == "relative_humidity":
                record[metric] = normalize_relative_humidity(pd.Series([row[column]])).iloc[0]
            else:
                record[metric] = pd.to_numeric(pd.Series([row[column]]), errors="coerce").iloc[0]
        output.append(record)
    return output


def build_region_hourly(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe

    metric_columns = ["rain_mm", "temperature_c", "relative_humidity", "solar_radiation_wm2"]
    source_hour = (
        dataframe.groupby(["region_name", "timestamp", "source"], as_index=False)
        .agg({column: "mean" for column in metric_columns})
        .sort_values(["region_name", "timestamp", "source"])
    )
    region_hour = (
        source_hour.groupby(["region_name", "timestamp"], as_index=False)
        .agg({column: "mean" for column in metric_columns})
        .sort_values(["region_name", "timestamp"])
    )
    region_hour["rain_mm"] = region_hour["rain_mm"].fillna(0).round(1)
    for column in ["temperature_c", "relative_humidity", "solar_radiation_wm2"]:
        region_hour[column] = region_hour[column].round(1)
    return region_hour


def merge_hour_windows(timestamps: list[pd.Timestamp]) -> list[str]:
    if not timestamps:
        return []

    timestamps = sorted(timestamps)
    windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    start = timestamps[0]
    previous = timestamps[0]
    for timestamp in timestamps[1:]:
        if timestamp - previous <= pd.Timedelta(hours=1):
            previous = timestamp
            continue
        windows.append((start, previous))
        start = timestamp
        previous = timestamp
    windows.append((start, previous))

    result = []
    for start_time, end_time in windows:
        if start_time == end_time:
            result.append(start_time.strftime("%H:%M"))
        else:
            result.append(f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}")
    return result


def evaluate_picking_suitability(region_hour: pd.DataFrame, args: argparse.Namespace) -> PickingDateSummary:
    region_summaries: list[RegionPickingSummary] = []
    if region_hour.empty:
        return PickingDateSummary(
            target_date=args.target_date,
            has_suitable_region=False,
            suitable_regions=[],
            checked_files=0,
            matched_files=0,
            region_summaries=[],
        )

    for region_name, rows in region_hour.groupby("region_name"):
        rows = rows.sort_values("timestamp").copy()
        rows["lookback_rain_mm"] = rows["rain_mm"].rolling(args.rain_lookback_hours + 1, min_periods=1).sum()
        picking_rows = rows[
            (rows["timestamp"].dt.hour >= args.picking_start_hour)
            & (rows["timestamp"].dt.hour <= args.picking_end_hour)
        ].copy()

        suitable_mask = (
            (picking_rows["rain_mm"] <= args.max_hourly_rain)
            & (picking_rows["lookback_rain_mm"] <= args.max_lookback_rain)
            & (picking_rows["temperature_c"].between(args.min_temperature, args.max_temperature))
            & (picking_rows["relative_humidity"] <= args.max_relative_humidity)
        )

        if "solar_radiation_wm2" in picking_rows.columns and not picking_rows["solar_radiation_wm2"].isna().all():
            suitable_mask = suitable_mask & (picking_rows["solar_radiation_wm2"] >= args.min_solar_radiation)

        suitable_rows = picking_rows.loc[suitable_mask]
        suitable_hour_count = len(suitable_rows)
        suitable = suitable_hour_count >= args.min_suitable_hours
        total_rain = round(float(rows["rain_mm"].sum()), 1)
        max_lookback_rain = round(float(picking_rows["lookback_rain_mm"].max()), 1) if not picking_rows.empty else 0.0

        reasons = []
        if total_rain > args.max_lookback_rain:
            reasons.append(f"全天累计降水 {total_rain:.1f} mm")
        if max_lookback_rain > args.max_lookback_rain:
            reasons.append(f"采摘前 {args.rain_lookback_hours} 小时最大累计降水 {max_lookback_rain:.1f} mm")
        if picking_rows["temperature_c"].dropna().empty:
            reasons.append("缺少温度数据")
        elif not picking_rows["temperature_c"].between(args.min_temperature, args.max_temperature).any():
            reasons.append("采摘时段温度不在适宜范围")
        if picking_rows["relative_humidity"].dropna().empty:
            reasons.append("缺少相对湿度数据")
        elif (picking_rows["relative_humidity"] > args.max_relative_humidity).all():
            reasons.append("采摘时段相对湿度偏高")
        if not suitable and not reasons:
            reasons.append(f"满足条件小时数不足 {args.min_suitable_hours} 小时")

        region_summaries.append(
            RegionPickingSummary(
                region_name=region_name,
                suitable=suitable,
                suitable_hour_count=suitable_hour_count,
                suitable_windows=merge_hour_windows(list(suitable_rows["timestamp"])),
                total_rain_mm=total_rain,
                max_lookback_rain_mm=max_lookback_rain,
                mean_temperature_c=round(float(picking_rows["temperature_c"].mean()), 1)
                if not picking_rows["temperature_c"].dropna().empty
                else None,
                mean_relative_humidity=round(float(picking_rows["relative_humidity"].mean()), 1)
                if not picking_rows["relative_humidity"].dropna().empty
                else None,
                max_solar_radiation_wm2=round(float(picking_rows["solar_radiation_wm2"].max()), 1)
                if not picking_rows["solar_radiation_wm2"].dropna().empty
                else None,
                reasons=reasons,
            )
        )

    suitable_regions = sorted(item.region_name for item in region_summaries if item.suitable)
    return PickingDateSummary(
        target_date=args.target_date,
        has_suitable_region=bool(suitable_regions),
        suitable_regions=suitable_regions,
        checked_files=0,
        matched_files=0,
        region_summaries=sorted(region_summaries, key=lambda item: item.region_name),
    )


def build_notification_message(summary: PickingDateSummary) -> str:
    if not summary.region_summaries:
        return f"{summary.target_date} 未找到可用于采茶适宜性判断的气象数据。"

    if summary.has_suitable_region:
        header = f"{summary.target_date} 存在适宜采茶区域：{', '.join(summary.suitable_regions)}。"
    else:
        header = f"{summary.target_date} 暂无适宜采茶区域。"

    lines = [header]
    for item in summary.region_summaries:
        status = "适宜" if item.suitable else "不适宜"
        windows = "、".join(item.suitable_windows) if item.suitable_windows else "无"
        reason_text = "；".join(item.reasons[:2]) if item.reasons else "条件满足"
        lines.append(
            f"{item.region_name}：{status}，适宜时段 {windows}，"
            f"雨量 {item.total_rain_mm:.1f} mm，均温 {format_optional(item.mean_temperature_c)} C，"
            f"均湿 {format_optional(item.mean_relative_humidity)}%，原因：{reason_text}"
        )
    return "\n".join(lines)


def format_optional(value: float | None) -> str:
    if value is None:
        return "NA"
    return f"{value:.1f}"


def main() -> int:
    args = parse_args()
    logger = configure_run_logger(
        "model.run_picking_date_alert",
        resolve_log_root(args.log_dir),
        run_name="run_picking_date_alert",
    )
    log_run_start(
        logger,
        "Picking-date alert workflow started",
        caiyun_dir=args.caiyun_dir,
        openmeteo_dir=args.openmeteo_dir,
        download_date=args.date,
        target_date=args.target_date,
        coord_file=args.coord_file,
        log_dir=args.log_dir,
    )

    try:
        caiyun_dir = resolve_input_dir(args.caiyun_dir)
        openmeteo_dir = resolve_input_dir(args.openmeteo_dir)
        if caiyun_dir is None and openmeteo_dir is None:
            raise ValueError("At least one of --caiyun-dir or --openmeteo-dir must be provided.")

        records, checked_files, matched_files = collect_hourly_records(
            caiyun_dir=caiyun_dir,
            openmeteo_dir=openmeteo_dir,
            download_date=args.date,
            target_date=args.target_date,
            coord_file=args.coord_file,
        )
        region_hour = build_region_hourly(records)
        summary = evaluate_picking_suitability(region_hour, args)
        summary.checked_files = checked_files
        summary.matched_files = matched_files

        notification_message = build_notification_message(summary)
        result_payload = {
            "summary": asdict(summary),
            "notification_sent": False,
            "notification_result": {"message": notification_message},
        }

        if args.send:
            send_result = send_notification(
                title=args.title,
                summary=notification_message,
                config_path=args.wxpusher_config,
            )
            result_payload["notification_sent"] = send_result.success
            result_payload["notification_result"] = {
                "success": send_result.success,
                "provider": send_result.provider,
                "response_summary": send_result.response_summary,
                "error": send_result.error,
                "status_code": send_result.status_code,
            }

        log_run_end(
            logger,
            "Picking-date alert workflow completed",
            has_suitable_region=summary.has_suitable_region,
            checked_files=summary.checked_files,
            matched_files=summary.matched_files,
        )
        print(json.dumps(result_payload, ensure_ascii=False, indent=2))
        return 0
    except (ConfigError, ValueError) as exc:
        log_exception(logger, "Picking-date alert workflow failed", exc)
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        log_exception(logger, "Picking-date alert workflow failed", exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
