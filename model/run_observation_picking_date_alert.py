from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common import (
    add_log_dir_argument,
    build_region_context,
    configure_run_logger,
    log_exception,
    log_run_end,
    log_run_start,
    resolve_log_root,
)
from model.run_picking_date_alert import (
    AREA_SUITABLE_FRACTION_THRESHOLD,
    CURRENT_SOLAR_THRESHOLD_WM2,
    DRYING_SOLAR_THRESHOLD_WM2,
    GOOD_PICKING_HOURS,
    MIN_DRYING_SOLAR_ENERGY_MJ,
    MIN_DRYING_SUN_HOURS,
    PARTIAL_PICKING_HOURS,
    RAIN_LOOKBACK_HOURS,
    RAIN_THRESHOLD_MM,
    RH_THRESHOLD,
    build_notification_message,
    build_point_hourly,
    build_point_suitability,
    build_region_hourly,
    evaluate_picking_suitability,
    extract_file_records,
    get_beijing_today,
    list_source_files,
    load_weather_table,
    resolve_input_dir,
)
from wxpusher.wxpusher_notify import ConfigError, send_notification


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate a Beijing-time tea-picking day using preceding history "
            "for rain-lookback calculations."
        )
    )
    parser.add_argument("--caiyun-dir")
    parser.add_argument("--openmeteo-dir")
    parser.add_argument("--date", default=get_beijing_today(), help="Download folder in Beijing date.")
    parser.add_argument("--target-date", default=get_beijing_today(), help="Beijing date to report.")
    parser.add_argument("--history-hours", type=int, default=24)
    parser.add_argument("--coord-file")
    parser.add_argument("--rain-threshold", type=float, default=RAIN_THRESHOLD_MM)
    parser.add_argument("--rain-lookback-hours", type=int, default=RAIN_LOOKBACK_HOURS)
    parser.add_argument("--max-relative-humidity", type=float, default=RH_THRESHOLD)
    parser.add_argument("--current-solar-threshold", type=float, default=CURRENT_SOLAR_THRESHOLD_WM2)
    parser.add_argument("--drying-solar-threshold", type=float, default=DRYING_SOLAR_THRESHOLD_WM2)
    parser.add_argument("--min-drying-sun-hours", type=int, default=MIN_DRYING_SUN_HOURS)
    parser.add_argument("--min-drying-solar-energy", type=float, default=MIN_DRYING_SOLAR_ENERGY_MJ)
    parser.add_argument("--area-suitable-fraction", type=float, default=AREA_SUITABLE_FRACTION_THRESHOLD)
    parser.add_argument("--good-picking-hours", type=int, default=GOOD_PICKING_HOURS)
    parser.add_argument("--partial-picking-hours", type=int, default=PARTIAL_PICKING_HOURS)
    parser.add_argument(
        "--wxpusher-config",
        default=str(ROOT_DIR / "wxpusher" / "wxpusher.config.json"),
    )
    parser.add_argument("--title", default="采茶适宜性提醒")
    parser.add_argument("--send", action="store_true")
    add_log_dir_argument(parser, ROOT_DIR / "logs")
    return parser.parse_args()


def collect_hourly_records_with_history(
    caiyun_dir: Path | None,
    openmeteo_dir: Path | None,
    download_date: str,
    target_date: str,
    history_hours: int,
    coord_file: str | None,
) -> tuple[pd.DataFrame, int, int]:
    if history_hours < 0:
        raise ValueError("--history-hours cannot be negative")
    target_start = pd.Timestamp(target_date)
    history_start = target_start - pd.Timedelta(hours=history_hours)
    target_end = target_start + pd.Timedelta(days=1)
    region_context = build_region_context(coord_file)
    records: list[dict] = []
    checked_files = 0
    matched_files = 0

    sources = (
        (caiyun_dir, "*.csv", "caiyun"),
        (openmeteo_dir, "*.xlsx", "openmeteo"),
    )
    for base_dir, pattern, source in sources:
        for file_path in list_source_files(base_dir, download_date, pattern):
            checked_files += 1
            file_records = extract_file_records(
                file_path,
                load_weather_table(file_path),
                source,
                region_context,
                history_start,
                target_end,
            )
            if file_records:
                matched_files += 1
                records.extend(file_records)
    return pd.DataFrame(records), checked_files, matched_files


def main() -> int:
    args = parse_args()
    logger = configure_run_logger(
        "model.run_observation_picking_date_alert",
        resolve_log_root(args.log_dir),
        run_name="run_observation_picking_date_alert",
    )
    log_run_start(
        logger,
        "History-aware picking alert started",
        download_date=args.date,
        target_date=args.target_date,
        history_hours=args.history_hours,
    )
    try:
        caiyun_dir = resolve_input_dir(args.caiyun_dir)
        openmeteo_dir = resolve_input_dir(args.openmeteo_dir)
        if caiyun_dir is None and openmeteo_dir is None:
            raise ValueError("At least one of --caiyun-dir or --openmeteo-dir must be provided.")

        records, checked_files, matched_files = collect_hourly_records_with_history(
            caiyun_dir,
            openmeteo_dir,
            args.date,
            args.target_date,
            args.history_hours,
            args.coord_file,
        )
        point_hourly = build_point_hourly(records)
        point_suitability = build_point_suitability(point_hourly, args)
        target_start = pd.Timestamp(args.target_date)
        target_end = target_start + pd.Timedelta(days=1)
        if point_suitability.empty:
            target_rows = point_suitability
        else:
            target_rows = point_suitability.loc[
                (point_suitability["timestamp"] >= target_start)
                & (point_suitability["timestamp"] < target_end)
            ].copy()

        region_hourly = build_region_hourly(target_rows, args)
        summary = evaluate_picking_suitability(region_hourly, args)
        summary.checked_files = checked_files
        summary.matched_files = matched_files
        notification_message = build_notification_message(summary)
        result_payload = {
            "summary": asdict(summary),
            "history_hours": args.history_hours,
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
            "History-aware picking alert completed",
            checked_files=checked_files,
            matched_files=matched_files,
            has_suitable_region=summary.has_suitable_region,
        )
        print(json.dumps(result_payload, ensure_ascii=False, indent=2))
        return 0
    except (ConfigError, ValueError) as exc:
        log_exception(logger, "History-aware picking alert failed", exc)
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        log_exception(logger, "History-aware picking alert failed", exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())






