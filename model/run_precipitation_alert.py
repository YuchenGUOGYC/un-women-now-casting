from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
from model.precipitation_checker import (
    DetectionSummary,
    build_summary,
    collect_detection_records,
    get_beijing_today,
    resolve_input_dir,
)
from wxpusher.wxpusher_notify import ConfigError, send_notification

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a simple precipitation model and send a WxPusher alert when rain is detected."
    )
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
    parser.add_argument(
        "--wxpusher-config",
        default=str(ROOT_DIR / "wxpusher" / "wxpusher.config.json"),
        help="Path to WxPusher JSON or YAML config.",
    )
    parser.add_argument(
        "--title",
        default="降水提醒",
        help="Notification title sent to WxPusher when precipitation is detected.",
    )
    parser.add_argument(
        "--state-file",
        default=str(SCRIPT_DIR / "precipitation_alert_state.json"),
        help="Path to the local alert deduplication state file.",
    )
    parser.add_argument(
        "--resend-hours",
        type=float,
        default=6.0,
        help="Allow a repeat alert for the same event after this many hours. Default: 6",
    )
    add_log_dir_argument(parser, ROOT_DIR / "logs")
    return parser.parse_args()


def build_notification_message(summary: DetectionSummary) -> str:
    if not summary.has_precipitation:
        return f"{summary.target_date} 未检测到降水。"

    headline_parts = [
        f"{summary.target_date} 研究区检测到降水。",
        f"最高级别：{summary.highest_severity or '小雨'}。",
        f"最大点位降水值：{summary.max_precipitation:.1f} mm。",
    ]
    if summary.earliest_rain_time and summary.latest_rain_time:
        headline_parts.append(f"整体时段：{format_time_window(summary.earliest_rain_time, summary.latest_rain_time)}。")
    header = " ".join(headline_parts)

    grouped_windows: dict[str, list] = {}
    for window in summary.region_windows:
        grouped_windows.setdefault(window.region_name, []).append(window)

    region_lines = []
    for region_name in sorted(grouped_windows.keys()):
        segment_text = "；".join(
            f"{format_time_window(window.start_time, window.end_time)} {window.severity} {window.accumulated_precipitation:.1f} mm"
            for window in grouped_windows[region_name]
        )
        region_lines.append(f"{region_name}：{segment_text}")

    if not region_lines:
        return header
    return header + "\n" + "\n".join(region_lines)


def format_time_window(start_time: str, end_time: str) -> str:
    start = datetime.fromisoformat(start_time)
    end = datetime.fromisoformat(end_time)
    if start == end:
        return start.strftime("%H:%M")
    return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"


def resolve_path(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def round_precipitation(value: float) -> float:
    return round(value, 1)


def build_alert_key(summary: DetectionSummary) -> str | None:
    if not summary.has_precipitation:
        return None

    payload = {
        "date": summary.target_date,
        "highest_severity": summary.highest_severity,
        "max_precipitation": round_precipitation(summary.max_precipitation),
        "region_windows": [
            {
                "region_name": window.region_name,
                "start_time": window.start_time,
                "end_time": window.end_time,
                "severity": window.severity,
                "accumulated_precipitation": round_precipitation(window.accumulated_precipitation),
            }
            for window in summary.region_windows
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid state file JSON: {state_file}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"State file must contain a JSON object: {state_file}")
    return data


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=BEIJING_TZ)
    return parsed


def should_send_alert(
    summary: DetectionSummary,
    state: dict,
    resend_hours: float,
    now: datetime,
) -> tuple[bool, str, str | None]:
    alert_key = build_alert_key(summary)
    if alert_key is None:
        return False, "no_precipitation", None

    last_alert_key = state.get("last_alert_key")
    if not isinstance(last_alert_key, str) or last_alert_key != alert_key:
        return True, "new_event", alert_key

    last_alert_at = parse_iso_datetime(state.get("last_alert_at"))
    resend_after = timedelta(hours=resend_hours)
    if last_alert_at is None:
        return True, "missing_last_alert_time", alert_key
    if now - last_alert_at >= resend_after:
        return True, "cooldown_elapsed", alert_key

    return False, "duplicate_event", alert_key


def save_state(
    state_file: Path,
    *,
    alert_key: str | None,
    summary: DetectionSummary,
    notification_sent: bool,
    notification_reason: str,
    now: datetime,
) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_payload = {
        "last_checked_at": now.isoformat(),
        "last_alert_key": alert_key,
        "last_alert_at": now.isoformat() if notification_sent else None,
        "last_notification_sent": notification_sent,
        "last_notification_reason": notification_reason,
        "last_summary": asdict(summary),
    }
    state_file.write_text(json.dumps(state_payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    logger = configure_run_logger(
        "model.run_precipitation_alert",
        resolve_log_root(args.log_dir),
        run_name="run_precipitation_alert",
    )
    log_run_start(
        logger,
        "Precipitation alert workflow started",
        caiyun_dir=args.caiyun_dir,
        openmeteo_dir=args.openmeteo_dir,
        coord_file=args.coord_file,
        target_date=args.date,
        threshold=args.threshold,
        wxpusher_config=args.wxpusher_config,
        state_file=args.state_file,
        resend_hours=args.resend_hours,
        log_dir=args.log_dir,
    )

    try:
        caiyun_dir = resolve_input_dir(args.caiyun_dir)
        openmeteo_dir = resolve_input_dir(args.openmeteo_dir)
        state_file = resolve_path(args.state_file)
        now = datetime.now(BEIJING_TZ)
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
        state = load_state(state_file)
        should_send, notification_reason, alert_key = should_send_alert(
            summary=summary,
            state=state,
            resend_hours=args.resend_hours,
            now=now,
        )

        result_payload = {
            "summary": asdict(summary),
            "notification_sent": False,
            "notification_reason": notification_reason,
            "notification_result": None,
            "state_file": str(state_file),
        }

        if should_send:
            notification_message = build_notification_message(summary)
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
        else:
            notification_message = build_notification_message(summary)
            result_payload["notification_result"] = {"message": notification_message}

        save_state(
            state_file,
            alert_key=alert_key,
            summary=summary,
            notification_sent=result_payload["notification_sent"],
            notification_reason=notification_reason,
            now=now,
        )

        log_run_end(
            logger,
            "Precipitation alert workflow completed",
            has_precipitation=summary.has_precipitation,
            checked_files=summary.checked_files,
            matched_files=summary.matched_files,
            notification_sent=result_payload["notification_sent"],
            notification_reason=notification_reason,
            state_file=state_file,
        )
        print(json.dumps(result_payload, ensure_ascii=False, indent=2))
        return 0
    except (ConfigError, ValueError) as exc:
        log_exception(logger, "Precipitation alert workflow failed", exc)
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        log_exception(logger, "Precipitation alert workflow failed", exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
