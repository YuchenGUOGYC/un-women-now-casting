from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone as datetime_timezone
from pathlib import Path
from urllib.parse import urlparse
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
from nowcast.observation_mask import DEFAULT_MASK_COLUMNS, load_radar_point_hours, merge_forecast_directory


DEFAULT_S3_BASE_URI = "s3://china-data-team-bucket-public/China_radar/"
RADAR_TIMESTAMP_RE = re.compile(r"_(\d{14})\d{3}_")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download Open-Meteo forecasts, calculate Beijing-time radar observations, "
            "mask observed forecast hours, upload both forecast products to S3, and run the picking alert."
        )
    )
    parser.add_argument("--input", required=True, help="Coordinate .xlsx/.csv used by Open-Meteo and alerts.")
    parser.add_argument("--openmeteo-output-dir", required=True, help="Local root for unmodified forecast runs.")
    parser.add_argument("--merged-output-dir", required=True, help="Local root for observation-masked forecast runs.")
    parser.add_argument("--radar-work-dir", required=True, help="Local working root for radar downloads and outputs.")
    parser.add_argument(
        "--radar-classifier-script",
        default=str(ROOT_DIR / "radar" / "processing" / "classify_hourly_radar_weather.py"),
        help="Path to classify_hourly_radar_weather.py.",
    )
    parser.add_argument("--radar-lookup", help="Existing lookup file. If omitted, it is downloaded from S3.")
    parser.add_argument("--openmeteo-script", default=str(ROOT_DIR / "openmetero" / "batch_run_openmetero.py"))
    parser.add_argument(
        "--alert-script",
        default=str(ROOT_DIR / "model" / "run_observation_picking_date_alert.py"),
    )
    parser.add_argument("--python-exe", default=sys.executable)
    parser.add_argument("--radar-python-exe", default=sys.executable)
    parser.add_argument("--alert-python-exe", default=sys.executable)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument(
        "--run-start",
        help="Optional fixed observation cutoff (ISO 8601) for replay/testing. A naive value is interpreted in --timezone.",
    )
    parser.add_argument("--s3-base-uri", default=DEFAULT_S3_BASE_URI)
    parser.add_argument("--radar-csv-s3-prefix", default="roi_pipeline/csv")
    parser.add_argument("--radar-lookup-s3-key", default="roi_pipeline/lookup/radar_highres_to_lonlat_lookup.xlsx")
    parser.add_argument("--forecast-s3-prefix", default="nowcasting/openmeteo_forecast")
    parser.add_argument("--merged-s3-prefix", default="nowcasting/openmeteo_observation_masked")
    parser.add_argument("--aws-cli", default="aws")
    parser.add_argument("--skip-s3-upload", action="store_true", help="For local validation only.")
    parser.add_argument("--allow-missing-current-radar", action="store_true")
    parser.add_argument("--mask-threshold-mm", type=float, default=0.0)
    parser.add_argument("--coordinate-tolerance", type=float, default=0.001)
    parser.add_argument(
        "--mask-column",
        action="append",
        dest="mask_columns",
        help=f"Forecast column to gate; repeatable. Default: {','.join(DEFAULT_MASK_COLUMNS)}",
    )
    parser.add_argument("--caiyun-dir", help="Optional Caiyun root passed unchanged to the alert.")
    parser.add_argument("--wxpusher-config", required=True)
    parser.add_argument("--send", action="store_true")
    parser.add_argument(
        "--alert-extra-arg",
        action="append",
        default=[],
        help="One extra argument passed to the alert script; repeat for each token.",
    )
    add_log_dir_argument(parser, ROOT_DIR / "logs")
    return parser.parse_args()


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def parse_run_start(value: str | None, timezone: str) -> pd.Timestamp:
    zone = ZoneInfo(timezone)
    timestamp = pd.Timestamp(value) if value else pd.Timestamp.now(tz=zone)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(zone)
    else:
        timestamp = timestamp.tz_convert(zone)
    return timestamp


def radar_file_local_timestamp(path: Path, timezone: str) -> pd.Timestamp:
    match = RADAR_TIMESTAMP_RE.search(path.name)
    if not match:
        raise ValueError(f"Cannot parse UTC radar timestamp from filename: {path.name}")
    utc_value = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=datetime_timezone.utc)
    return pd.Timestamp(utc_value).tz_convert(ZoneInfo(timezone))


def filter_radar_csvs(
    source_dir: Path,
    target_dir: Path,
    *,
    window_start: pd.Timestamp,
    observation_cutoff: pd.Timestamp,
    timezone: str,
) -> list[Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    selected: list[Path] = []
    for source in sorted(source_dir.glob("*_roi_pixels.csv")):
        local_timestamp = radar_file_local_timestamp(source, timezone)
        if window_start <= local_timestamp < observation_cutoff:
            target = target_dir / source.name
            shutil.copy2(source, target)
            selected.append(target)
    return selected


def s3_join(base: str, *parts: str) -> str:
    parsed = urlparse(base)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Invalid S3 URI: {base}")
    path = "/".join([parsed.path.strip("/"), *(part.strip("/") for part in parts if part)])
    return f"s3://{parsed.netloc}/{path}/" if path else f"s3://{parsed.netloc}/"


def run_command(command: list[str], logger, label: str) -> None:
    logger.info("%s | command=%s", label, command)
    subprocess.run(command, check=True)


def resolve_radar_lookup(args: argparse.Namespace, radar_root: Path, logger) -> Path:
    """Use S3 first, then fall back to an explicit or repository-local lookup."""
    if args.radar_lookup:
        explicit = resolve_path(args.radar_lookup)
        if not explicit.exists():
            raise FileNotFoundError(f"Explicit --radar-lookup does not exist: {explicit}")
        logger.info("Using explicitly configured radar lookup: %s", explicit)
        return explicit

    lookup = radar_root / "lookup" / "radar_highres_to_lonlat_lookup.xlsx"
    lookup.parent.mkdir(parents=True, exist_ok=True)
    lookup_uri = s3_join(args.s3_base_uri, args.radar_lookup_s3_key).rstrip("/")
    try:
        run_command(
            [args.aws_cli, "s3", "cp", lookup_uri, str(lookup)],
            logger,
            "Downloading radar lookup from S3 (preferred)",
        )
        logger.info("Using S3 radar lookup: %s", lookup_uri)
        return lookup
    except subprocess.CalledProcessError as exc:
        logger.warning("S3 radar lookup unavailable (%s); trying local fallback", exc)

    relative_lookup = Path("data") / "nmc_xinan_radar" / "roi_pipeline" / "lookup" / "radar_highres_to_lonlat_lookup.xlsx"
    classifier_path = resolve_path(args.radar_classifier_script)
    candidates = [classifier_path.parent.parent / relative_lookup, Path.cwd() / relative_lookup]
    for candidate in candidates:
        if candidate.exists():
            logger.info("Using local fallback radar lookup: %s", candidate)
            return candidate

    raise FileNotFoundError(
        "Radar lookup was not found in S3 or local fallback paths. "
        f"Tried S3 key {lookup_uri} and local paths: {candidates}"
    )

def stage_openmeteo(args: argparse.Namespace, run_started_at: pd.Timestamp, run_id: str, logger) -> Path:
    run_date = run_started_at.strftime("%Y-%m-%d")
    previous_date = (run_started_at.normalize() - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (run_started_at.normalize() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    run_root = resolve_path(args.openmeteo_output_dir) / f"run={run_id}"
    staging = run_root / ".download"
    command = [
        args.python_exe,
        str(resolve_path(args.openmeteo_script)),
        "--input",
        str(resolve_path(args.input)),
        "--output-dir",
        str(staging),
        "--start-date",
        previous_date,
        "--end-date",
        next_date,
        "--timezone",
        args.timezone,
        "--max-workers",
        str(args.max_workers),
        "--log-dir",
        str(resolve_path(args.log_dir)),
    ]
    run_command(command, logger, "Running Open-Meteo batch")

    downloaded = sorted(staging.rglob("*.xlsx"))
    if not downloaded:
        raise RuntimeError(f"Open-Meteo completed without .xlsx outputs under {staging}")
    canonical_dir = run_root / run_date
    canonical_dir.mkdir(parents=True, exist_ok=True)
    for source in downloaded:
        shutil.copy2(source, canonical_dir / source.name)
    return run_root


def download_and_classify_radar(
    args: argparse.Namespace,
    observation_cutoff: pd.Timestamp,
    run_id: str,
    logger,
) -> tuple[list[Path], Path]:
    radar_root = resolve_path(args.radar_work_dir) / f"run={run_id}"
    lookup = resolve_radar_lookup(args, radar_root, logger)

    dates = [
        (observation_cutoff.normalize() - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
        observation_cutoff.strftime("%Y-%m-%d"),
    ]
    point_paths: list[Path] = []
    for radar_date in dates:
        csv_dir = radar_root / "csv" / f"date={radar_date}"
        csv_dir.mkdir(parents=True, exist_ok=True)
        run_command(
            [
                args.aws_cli,
                "s3",
                "sync",
                s3_join(args.s3_base_uri, args.radar_csv_s3_prefix, f"date={radar_date}"),
                str(csv_dir),
                "--exact-timestamps",
            ],
            logger,
            f"Downloading radar CSVs for {radar_date}",
        )
        downloaded_csv_files = sorted(csv_dir.glob("*_roi_pixels.csv"))
        if not downloaded_csv_files:
            is_current = radar_date == observation_cutoff.strftime("%Y-%m-%d")
            if is_current and args.allow_missing_current_radar:
                logger.warning("No current-date radar CSVs for %s; continuing by request", radar_date)
                continue
            raise RuntimeError(f"No radar ROI CSVs found for Beijing date {radar_date}: {csv_dir}")

        classifier_csv_dir = radar_root / "csv_before_cutoff" / f"date={radar_date}"
        selected_csv_files = filter_radar_csvs(
            csv_dir,
            classifier_csv_dir,
            window_start=observation_cutoff.normalize() - pd.Timedelta(days=1),
            observation_cutoff=observation_cutoff,
            timezone=args.timezone,
        )
        if not selected_csv_files:
            is_current = radar_date == observation_cutoff.strftime("%Y-%m-%d")
            if is_current and args.allow_missing_current_radar:
                logger.warning("No current-date radar CSVs exist before cutoff %s", observation_cutoff.isoformat())
                continue
            raise RuntimeError(
                f"No radar ROI CSVs fall before observation cutoff for Beijing date {radar_date}"
            )
        logger.info(
            "Selected %s/%s radar CSVs before cutoff %s for %s",
            len(selected_csv_files),
            len(downloaded_csv_files),
            observation_cutoff.isoformat(),
            radar_date,
        )

        hourly_root = radar_root / "hourly" / f"date={radar_date}"
        point_dir = hourly_root / "points_by_hour"
        run_command(
            [
                args.radar_python_exe,
                str(resolve_path(args.radar_classifier_script)),
                "--radar-csv-dir",
                str(classifier_csv_dir),
                "--lookup",
                str(lookup),
                "--point-output-dir",
                str(point_dir),
                "--summary-output",
                str(hourly_root / "hourly_weather_summary.csv"),
            ],
            logger,
            f"Classifying radar precipitation for {radar_date}",
        )
        point_paths.extend(sorted(point_dir.glob("hourly_point_weather_*.csv")))
    if not point_paths:
        raise RuntimeError("Radar classification produced no point-hour CSV files")
    return point_paths, radar_root


def upload_directory(args: argparse.Namespace, local_dir: Path, s3_uri: str, logger, label: str) -> None:
    if args.skip_s3_upload:
        logger.warning("Skipping S3 upload: %s -> %s", local_dir, s3_uri)
        return
    run_command(
        [args.aws_cli, "s3", "sync", str(local_dir), s3_uri, "--exact-timestamps"],
        logger,
        label,
    )


def main() -> int:
    args = parse_args()
    logger = configure_run_logger(
        "nowcast.observation_masked_alert",
        resolve_log_root(args.log_dir),
        run_name="observation_masked_alert",
    )
    try:
        if args.timezone != "Asia/Shanghai":
            raise ValueError("The referenced radar classifier emits Beijing time; --timezone must be Asia/Shanghai")
        pipeline_started_at = parse_run_start(args.run_start, args.timezone)
        run_date = pipeline_started_at.strftime("%Y-%m-%d")
        run_id = pipeline_started_at.strftime("%Y%m%dT%H%M%S%z")
        log_run_start(
            logger,
            "Observation-masked forecast workflow started",
            pipeline_started_at=pipeline_started_at.isoformat(),
            timezone=args.timezone,
            run_id=run_id,
        )

        forecast_run_root = stage_openmeteo(args, pipeline_started_at, run_id, logger)
        forecast_date_dir = forecast_run_root / run_date
        forecast_s3_uri = s3_join(args.s3_base_uri, args.forecast_s3_prefix, f"date={run_date}", f"run={run_id}")
        upload_directory(args, forecast_date_dir, forecast_s3_uri, logger, "Uploading original Open-Meteo forecast")

        observation_cutoff = (
            pipeline_started_at
            if args.run_start
            else pd.Timestamp.now(tz=ZoneInfo(args.timezone))
        )
        if observation_cutoff.strftime("%Y-%m-%d") != run_date:
            raise RuntimeError(
                "Workflow crossed Beijing midnight before radar processing; rerun to avoid mixing dates"
            )
        window_start = observation_cutoff.normalize() - pd.Timedelta(days=1)
        logger.info(
            "Radar observation cutoff fixed | cutoff=%s window_start=%s",
            observation_cutoff.isoformat(),
            window_start.isoformat(),
        )
        radar_paths, radar_run_root = download_and_classify_radar(args, observation_cutoff, run_id, logger)
        radar = load_radar_point_hours(radar_paths, args.timezone)
        merged_run_root = resolve_path(args.merged_output_dir) / f"run={run_id}"
        merged_date_dir = merged_run_root / run_date
        stats = merge_forecast_directory(
            forecast_date_dir,
            merged_date_dir,
            radar,
            window_start=window_start,
            run_started_at=observation_cutoff,
            timezone=args.timezone,
            mask_columns=args.mask_columns or DEFAULT_MASK_COLUMNS,
            threshold_mm=args.mask_threshold_mm,
            coordinate_tolerance=args.coordinate_tolerance,
        )
        if stats.unmatched_point_files:
            raise RuntimeError(
                "Radar coordinates did not match forecast files within tolerance: "
                + ", ".join(stats.unmatched_point_files)
            )

        merged_s3_uri = s3_join(args.s3_base_uri, args.merged_s3_prefix, f"date={run_date}", f"run={run_id}")
        manifest = {
            "run_id": run_id,
            "pipeline_started_at": pipeline_started_at.isoformat(),
            "observation_cutoff": observation_cutoff.isoformat(),
            "timezone": args.timezone,
            "observation_window": {
                "start_inclusive": window_start.isoformat(),
                "end_exclusive": observation_cutoff.isoformat(),
            },
            "mask_threshold_mm": args.mask_threshold_mm,
            "mask_columns": list(args.mask_columns or DEFAULT_MASK_COLUMNS),
            "forecast_s3_uri": forecast_s3_uri,
            "merged_s3_uri": merged_s3_uri,
            "radar_work_dir": str(radar_run_root),
            "merge_stats": {
                "file_count": stats.file_count,
                "row_count": stats.row_count,
                "observed_row_count": stats.observed_row_count,
                "masked_row_count": stats.masked_row_count,
            },
        }
        manifest_path = merged_date_dir / "run_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        upload_directory(args, merged_date_dir, merged_s3_uri, logger, "Uploading observation-masked forecast")

        alert_command = [
            args.alert_python_exe,
            str(resolve_path(args.alert_script)),
            "--openmeteo-dir",
            str(merged_run_root),
            "--coord-file",
            str(resolve_path(args.input)),
            "--date",
            run_date,
            "--target-date",
            run_date,
            "--wxpusher-config",
            str(resolve_path(args.wxpusher_config)),
            "--log-dir",
            str(resolve_path(args.log_dir)),
        ]
        if args.caiyun_dir:
            alert_command.extend(["--caiyun-dir", str(resolve_path(args.caiyun_dir))])
        if args.send:
            alert_command.append("--send")
        alert_command.extend(args.alert_extra_arg)
        run_command(alert_command, logger, "Running picking-date alert with merged forecast")
        log_run_end(logger, "Observation-masked forecast workflow completed", **manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        log_exception(logger, "Observation-masked forecast workflow failed", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
