from __future__ import annotations

import argparse
import json
import re
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

RAIN_THRESHOLD_MM = 0.1
RAIN_LOOKBACK_HOURS = 12
RH_THRESHOLD = 85.0
CURRENT_SOLAR_THRESHOLD_WM2 = 80.0
DRYING_SOLAR_THRESHOLD_WM2 = 120.0
MIN_DRYING_SUN_HOURS = 4
MIN_DRYING_SOLAR_ENERGY_MJ = 1.5
AREA_SUITABLE_FRACTION_THRESHOLD = 0.5
GOOD_PICKING_HOURS = 6
PARTIAL_PICKING_HOURS = 3


@dataclass
class RegionPickingSummary:
    region_name: str
    picking_class: str
    suitable: bool
    suitable_hour_count: int
    suitable_windows: list[str]
    mean_suitable_fraction: float
    mean_raining_fraction: float
    mean_humid_fraction: float
    mean_post_rain_dry_enough_fraction: float
    total_rain_mm: float
    mean_temperature_c: float | None
    mean_relative_humidity: float | None
    mean_solar_radiation_wm2: float | None
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
    parser.add_argument("--rain-threshold", type=float, default=RAIN_THRESHOLD_MM, help="Rain threshold in mm/h.")
    parser.add_argument(
        "--rain-lookback-hours",
        type=int,
        default=RAIN_LOOKBACK_HOURS,
        help="Recent rain lookback window in hours.",
    )
    parser.add_argument("--max-relative-humidity", type=float, default=RH_THRESHOLD, help="Maximum suitable RH.")
    parser.add_argument(
        "--current-solar-threshold",
        type=float,
        default=CURRENT_SOLAR_THRESHOLD_WM2,
        help="Current-hour suitable shortwave radiation threshold, W/m2.",
    )
    parser.add_argument(
        "--drying-solar-threshold",
        type=float,
        default=DRYING_SOLAR_THRESHOLD_WM2,
        help="Effective post-rain drying shortwave radiation threshold, W/m2.",
    )
    parser.add_argument(
        "--min-drying-sun-hours",
        type=int,
        default=MIN_DRYING_SUN_HOURS,
        help="Minimum effective drying sun hours after recent rain.",
    )
    parser.add_argument(
        "--min-drying-solar-energy",
        type=float,
        default=MIN_DRYING_SOLAR_ENERGY_MJ,
        help="Minimum post-rain accumulated solar energy, MJ/m2.",
    )
    parser.add_argument(
        "--area-suitable-fraction",
        type=float,
        default=AREA_SUITABLE_FRACTION_THRESHOLD,
        help="Minimum suitable point fraction for a region-hour.",
    )
    parser.add_argument("--good-picking-hours", type=int, default=GOOD_PICKING_HOURS)
    parser.add_argument("--partial-picking-hours", type=int, default=PARTIAL_PICKING_HOURS)
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


def build_point_id(longitude: float, latitude: float) -> str:
    return f"lon{longitude:.6f}_lat{latitude:.6f}"


def extract_query_coordinates(file_path: Path, dataframe: pd.DataFrame) -> tuple[float, float]:
    match = FILENAME_COORD_PATTERN.search(file_path.stem)
    if match:
        return decode_filename_coord(match.group("lon")), decode_filename_coord(match.group("lat"))

    normalized_columns = {str(column).strip().lower(): column for column in dataframe.columns}
    if "longitude" in normalized_columns and "latitude" in normalized_columns and not dataframe.empty:
        return float(dataframe.iloc[0][normalized_columns["longitude"]]), float(
            dataframe.iloc[0][normalized_columns["latitude"]]
        )
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


def normalize_relative_humidity(value) -> float | None:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return None
    if numeric <= 1.5:
        return float(numeric) * 100
    return float(numeric)


def numeric_value(value) -> float | None:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(numeric):
        return None
    return float(numeric)


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
        file_records = extract_file_records(
            file_path, load_weather_table(file_path), "caiyun", region_context, start_time, end_time
        )
        if file_records:
            matched_files += 1
            records.extend(file_records)

    for file_path in list_source_files(openmeteo_dir, download_date, "*.xlsx"):
        checked_files += 1
        file_records = extract_file_records(
            file_path, load_weather_table(file_path), "openmeteo", region_context, start_time, end_time
        )
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
    point_id = build_point_id(longitude, latitude)
    region_name = get_region_name(longitude, latitude, region_context)
    timestamps = dataframe[time_column].map(normalize_local_timestamp)
    filtered = dataframe.loc[(timestamps >= start_time) & (timestamps < end_time)].copy()
    filtered_timestamps = timestamps.loc[filtered.index]
    if filtered.empty:
        return []

    if source == "openmeteo":
        columns = {
            "rain_mm": pick_column(filtered.columns, ["rain", "precipitation"]),
            "temp_c": pick_column(filtered.columns, ["temperature_2m", "temperature", "temp_c"]),
            "relative_humidity": pick_column(filtered.columns, ["relative_humidity_2m", "humidity", "relative_humidity"]),
            "solar_wm2": pick_column(filtered.columns, ["shortwave_radiation", "solar_radiation", "solar_wm2"]),
        }
    else:
        columns = {
            "rain_mm": pick_column(filtered.columns, ["precipitation", "rain"]),
            "temp_c": pick_column(filtered.columns, ["temperature", "temperature_2m", "temp_c"]),
            "relative_humidity": pick_column(filtered.columns, ["humidity", "relative_humidity", "relative_humidity_2m"]),
            "solar_wm2": pick_column(filtered.columns, ["solar_radiation", "shortwave_radiation", "solar_wm2"]),
        }

    output: list[dict] = []
    for row_index, row in filtered.iterrows():
        solar_wm2 = numeric_value(row[columns["solar_wm2"]]) if columns["solar_wm2"] else None
        output.append(
            {
                "point_id": point_id,
                "region_name": region_name,
                "timestamp": filtered_timestamps.loc[row_index],
                "rain_mm": numeric_value(row[columns["rain_mm"]]) if columns["rain_mm"] else 0.0,
                "temp_c": numeric_value(row[columns["temp_c"]]) if columns["temp_c"] else None,
                "relative_humidity": normalize_relative_humidity(row[columns["relative_humidity"]])
                if columns["relative_humidity"]
                else None,
                "solar_wm2": solar_wm2,
                "is_daylight": int(solar_wm2 is not None and solar_wm2 > 0),
            }
        )
    return output


def build_point_hourly(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe
    point_hourly = (
        dataframe.groupby(["point_id", "region_name", "timestamp"], as_index=False)
        .agg(
            rain_mm=("rain_mm", "mean"),
            temp_c=("temp_c", "mean"),
            relative_humidity=("relative_humidity", "mean"),
            solar_wm2=("solar_wm2", "mean"),
        )
        .sort_values(["point_id", "timestamp"])
    )
    point_hourly["rain_mm"] = point_hourly["rain_mm"].fillna(0).clip(lower=0)
    point_hourly["solar_wm2"] = point_hourly["solar_wm2"].clip(lower=0)
    point_hourly["is_daylight"] = (point_hourly["solar_wm2"].fillna(0) > 0).astype(int)
    return point_hourly


def calculate_point_suitability(group: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    rows = group.sort_values("timestamp").copy()
    rows["is_raining"] = (rows["rain_mm"] >= args.rain_threshold).astype(int)
    rows["too_humid"] = (rows["relative_humidity"] > args.max_relative_humidity).astype(int)
    rows["current_enough_sun"] = (rows["solar_wm2"] >= args.current_solar_threshold).astype(int)
    rows["effective_drying_sun"] = (
        (rows["is_daylight"] == 1) & (rows["solar_wm2"] >= args.drying_solar_threshold)
    ).astype(int)
    rows["solar_energy_mj"] = rows["solar_wm2"].fillna(0) * 3600 / 1_000_000

    datetimes = rows["timestamp"].to_numpy()
    is_rain = rows["is_raining"].to_numpy()
    effective = rows["effective_drying_sun"].to_numpy()
    energy = rows["solar_energy_mj"].to_numpy()
    cum_effective = [0]
    cum_energy = [0.0]
    for value in effective:
        cum_effective.append(cum_effective[-1] + int(value))
    for value in energy:
        cum_energy.append(cum_energy[-1] + float(value))

    post_dry_enough = [1] * len(rows)
    last_rain_idx = None

    for index in range(len(rows)):
        if is_rain[index] == 1:
            last_rain_idx = index
        if last_rain_idx is None:
            continue
        delta_hours = (pd.Timestamp(datetimes[index]) - pd.Timestamp(datetimes[last_rain_idx])).total_seconds() / 3600
        if delta_hours <= args.rain_lookback_hours:
            start = last_rain_idx + 1
            end = index + 1
            hours = int(cum_effective[end] - cum_effective[start])
            energy_mj = float(cum_energy[end] - cum_energy[start])
            post_dry_enough[index] = int(
                hours >= args.min_drying_sun_hours and energy_mj >= args.min_drying_solar_energy
            )

    rows["post_rain_dry_enough"] = post_dry_enough
    rows["suitable_for_picking"] = (
        (rows["is_daylight"] == 1)
        & (rows["is_raining"] == 0)
        & (rows["too_humid"] == 0)
        & (rows["current_enough_sun"] == 1)
        & (rows["post_rain_dry_enough"] == 1)
    ).astype(int)
    return rows


def build_point_suitability(point_hourly: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    if point_hourly.empty:
        return point_hourly
    groups = (
        calculate_point_suitability(group, args)
        for _, group in point_hourly.groupby("point_id", sort=False)
    )
    return pd.concat(groups, ignore_index=True)


def build_region_hourly(point_hourly: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    if point_hourly.empty:
        return point_hourly
    region_hourly = (
        point_hourly.groupby(["region_name", "timestamp"], as_index=False)
        .agg(
            mean_rain_mm=("rain_mm", "mean"),
            mean_temp_c=("temp_c", "mean"),
            mean_relative_humidity=("relative_humidity", "mean"),
            mean_solar_wm2=("solar_wm2", "mean"),
            raining_fraction=("is_raining", "mean"),
            humid_fraction=("too_humid", "mean"),
            current_enough_sun_fraction=("current_enough_sun", "mean"),
            post_rain_dry_enough_fraction=("post_rain_dry_enough", "mean"),
            suitable_fraction=("suitable_for_picking", "mean"),
        )
        .sort_values(["region_name", "timestamp"])
    )
    region_hourly["area_suitable_for_picking"] = (
        region_hourly["suitable_fraction"] >= args.area_suitable_fraction
    ).astype(int)
    return region_hourly


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
    return [
        start_time.strftime("%H:%M")
        if start_time == end_time
        else f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}"
        for start_time, end_time in windows
    ]


def classify_picking_day(suitable_hours: int, args: argparse.Namespace) -> str:
    if suitable_hours >= args.good_picking_hours:
        return "适宜采茶"
    if suitable_hours >= args.partial_picking_hours:
        return "部分时段适宜"
    return "暂不适宜采茶"


def evaluate_picking_suitability(region_hourly: pd.DataFrame, args: argparse.Namespace) -> PickingDateSummary:
    if region_hourly.empty:
        return PickingDateSummary(args.target_date, False, [], 0, 0, [])

    region_summaries: list[RegionPickingSummary] = []
    for region_name, rows in region_hourly.groupby("region_name"):
        rows = rows.sort_values("timestamp").copy()
        suitable_rows = rows.loc[rows["area_suitable_for_picking"] == 1]
        suitable_hour_count = int(rows["area_suitable_for_picking"].sum())
        picking_class = classify_picking_day(suitable_hour_count, args)
        suitable = picking_class != "暂不适宜采茶"

        reasons = []
        if rows["raining_fraction"].mean() > 0:
            reasons.append(f"降雨覆盖比例{rows['raining_fraction'].mean():.0%}")
        if rows["humid_fraction"].mean() > 0:
            reasons.append(f"高湿覆盖比例{rows['humid_fraction'].mean():.0%}")
        if rows["post_rain_dry_enough_fraction"].mean() < args.area_suitable_fraction:
            reasons.append("雨后干燥条件不足")
        if rows["suitable_fraction"].mean() < args.area_suitable_fraction:
            reasons.append("综合适采覆盖比例偏低")
        if not reasons:
            reasons.append("降雨、湿度和光照条件满足")

        region_summaries.append(
            RegionPickingSummary(
                region_name=region_name,
                picking_class=picking_class,
                suitable=suitable,
                suitable_hour_count=suitable_hour_count,
                suitable_windows=merge_hour_windows(list(suitable_rows["timestamp"])),
                mean_suitable_fraction=round(float(rows["suitable_fraction"].mean()), 3),
                mean_raining_fraction=round(float(rows["raining_fraction"].mean()), 3),
                mean_humid_fraction=round(float(rows["humid_fraction"].mean()), 3),
                mean_post_rain_dry_enough_fraction=round(float(rows["post_rain_dry_enough_fraction"].mean()), 3),
                total_rain_mm=round(float(rows["mean_rain_mm"].sum()), 1),
                mean_temperature_c=round(float(rows["mean_temp_c"].mean()), 1)
                if not rows["mean_temp_c"].dropna().empty
                else None,
                mean_relative_humidity=round(float(rows["mean_relative_humidity"].mean()), 1)
                if not rows["mean_relative_humidity"].dropna().empty
                else None,
                mean_solar_radiation_wm2=round(float(rows["mean_solar_wm2"].mean()), 1)
                if not rows["mean_solar_wm2"].dropna().empty
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


def format_optional(value: float | None) -> str:
    if value is None:
        return "NA"
    return f"{value:.1f}"


def build_notification_message(summary: PickingDateSummary) -> str:
    if not summary.region_summaries:
        return "采茶建议尚未计算。"

    lines = []
    for item in summary.region_summaries:
        windows = "、".join(item.suitable_windows) if item.suitable_windows else "无"
        reason_text = "、".join(item.reasons[:2]) if item.reasons else "降雨、湿度和光照条件满足"
        lines.append(
            f"{item.region_name}：{item.picking_class}；适采{item.suitable_hour_count}小时；"
            f"建议时段：{windows}；预计雨量{item.total_rain_mm:.1f}毫米；"
            f"平均湿度{format_optional(item.mean_relative_humidity)}%；说明：{reason_text}"
        )
    return "\n".join(lines)


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
        point_hourly = build_point_hourly(records)
        point_suitability = build_point_suitability(point_hourly, args)
        region_hourly = build_region_hourly(point_suitability, args)
        summary = evaluate_picking_suitability(region_hourly, args)
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
