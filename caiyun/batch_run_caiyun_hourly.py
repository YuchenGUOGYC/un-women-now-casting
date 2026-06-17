import argparse
import csv
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def parse_list_argument(values):
    items = []
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if part:
                items.append(part)
    return items


def get_beijing_today():
    return datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run caiyun_hourly.py in batch mode from a latitude/longitude list."
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Input coordinate file path (.xlsx or .csv). Must contain latitude and longitude columns.",
    )
    parser.add_argument(
        "--token",
        required=True,
        help="Caiyun API token.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for per-point output CSV files.",
    )
    parser.add_argument(
        "--caiyun-script",
        default=str(SCRIPT_DIR / "caiyun_hourly.py"),
        help="Path to caiyun_hourly.py. Default: the sibling script in this folder.",
    )
    parser.add_argument(
        "--python-exe",
        default=sys.executable,
        help="Python executable used to run caiyun_hourly.py. Default: current Python.",
    )
    parser.add_argument(
        "--hourlysteps",
        type=int,
        default=48,
        help="Hourly steps to request from Caiyun. Default: 48",
    )
    parser.add_argument(
        "--fields",
        action="append",
        help="Hourly sections to extract. Can be repeated or passed as a comma-separated list.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional limit on number of coordinate rows to run.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=4,
        help="Number of parallel workers. Default: 4",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Timeout passed to caiyun_hourly.py. Default: 60",
    )
    parser.add_argument(
        "--run-alert-after-download",
        action="store_true",
        help="Run model/run_precipitation_alert.py after the Caiyun batch completes successfully.",
    )
    parser.add_argument(
        "--alert-script",
        default=str(ROOT_DIR / "model" / "run_precipitation_alert.py"),
        help="Path to run_precipitation_alert.py.",
    )
    parser.add_argument(
        "--alert-python-exe",
        default=sys.executable,
        help="Python executable used to run the alert script. Default: current Python.",
    )
    parser.add_argument(
        "--alert-caiyun-dir",
        help="Override Caiyun root output directory passed to the alert script. Default: current --output-dir.",
    )
    parser.add_argument(
        "--alert-openmeteo-dir",
        help="Optional Open-Meteo root output directory passed to the alert script.",
    )
    parser.add_argument(
        "--alert-wxpusher-config",
        help="WxPusher config path passed to the alert script.",
    )
    parser.add_argument(
        "--alert-state-file",
        help="Optional state file path for alert deduplication.",
    )
    parser.add_argument(
        "--alert-threshold",
        type=float,
        help="Optional precipitation threshold passed to the alert script.",
    )
    parser.add_argument(
        "--alert-resend-hours",
        type=float,
        help="Optional resend interval in hours passed to the alert script.",
    )
    parser.add_argument(
        "--alert-title",
        help="Optional notification title passed to the alert script.",
    )
    add_log_dir_argument(parser, ROOT_DIR / "logs")
    return parser.parse_args()


def read_coordinate_table(input_path):
    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        with input_path.open("r", encoding="utf-8-sig", newline="") as csvfile:
            reader = csv.DictReader(csvfile)
            rows = [normalize_row_keys(row) for row in reader]
    else:
        dataframe = pd.read_excel(input_path)
        dataframe.columns = [str(col).strip().lower() for col in dataframe.columns]
        rows = [normalize_row_keys(row) for row in dataframe.to_dict(orient="records")]

    required_columns = {"latitude", "longitude"}
    available_columns = set(rows[0].keys()) if rows else set()
    missing = required_columns - available_columns
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")

    return rows


def normalize_row_keys(row):
    return {str(key).strip().lower(): value for key, value in row.items()}

def build_output_path(output_dir, latitude, longitude, row_number):
    safe_lat = f"{latitude:.6f}".replace("-", "m").replace(".", "p")
    safe_lon = f"{longitude:.6f}".replace("-", "m").replace(".", "p")
    return output_dir / f"caiyun_row{row_number:03d}_lat{safe_lat}_lon{safe_lon}.csv"


def run_one_point(
    row_index,
    total,
    latitude,
    longitude,
    output_dir,
    python_exe,
    caiyun_script,
    token,
    hourlysteps,
    fields,
    timeout,
    log_dir,
    logger,
):
    output_path = build_output_path(output_dir, latitude, longitude, row_index)
    command = [
        python_exe,
        str(caiyun_script),
        "--token",
        token,
        "--latitude",
        str(latitude),
        "--longitude",
        str(longitude),
        "--hourlysteps",
        str(hourlysteps),
        "--timeout",
        str(timeout),
        "--no-date-subdir",
        "--output",
        str(output_path),
    ]

    for field_name in fields:
        command.extend(["--fields", field_name])
    command.extend(["--log-dir", str(log_dir)])

    logger.info(
        "Starting Caiyun batch item %s/%s for lat=%s lon=%s output=%s",
        row_index,
        total,
        latitude,
        longitude,
        output_path,
    )
    print(f"[{row_index}/{total}] Running latitude={latitude}, longitude={longitude}")
    subprocess.run(command, check=True)
    logger.info("Finished Caiyun batch item %s/%s -> %s", row_index, total, output_path)
    return output_path


def maybe_run_alert_after_download(args, logger):
    if not args.run_alert_after_download:
        return

    alert_script = Path(args.alert_script).expanduser()
    if not alert_script.is_absolute():
        alert_script = Path.cwd() / alert_script

    caiyun_dir = args.alert_caiyun_dir or args.output_dir
    command = [
        args.alert_python_exe,
        str(alert_script),
        "--caiyun-dir",
        caiyun_dir,
        "--log-dir",
        str(args.log_dir),
    ]

    if args.alert_openmeteo_dir:
        command.extend(["--openmeteo-dir", args.alert_openmeteo_dir])
    if args.alert_wxpusher_config:
        command.extend(["--wxpusher-config", args.alert_wxpusher_config])
    if args.alert_state_file:
        command.extend(["--state-file", args.alert_state_file])
    if args.alert_threshold is not None:
        command.extend(["--threshold", str(args.alert_threshold)])
    if args.alert_resend_hours is not None:
        command.extend(["--resend-hours", str(args.alert_resend_hours)])
    if args.alert_title:
        command.extend(["--title", args.alert_title])

    logger.info("Running alert workflow after Caiyun batch: %s", command)
    subprocess.run(command, check=True)
    logger.info("Alert workflow completed after Caiyun batch")


def main():
    args = parse_args()
    logger = configure_run_logger("caiyun.batch", resolve_log_root(args.log_dir), run_name="caiyun_batch")

    input_path = Path(args.input).expanduser()
    if not input_path.is_absolute():
        input_path = Path.cwd() / input_path

    output_dir = Path(args.output_dir).expanduser()
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    output_dir = output_dir / get_beijing_today()
    output_dir.mkdir(parents=True, exist_ok=True)

    caiyun_script = Path(args.caiyun_script).expanduser()
    if not caiyun_script.is_absolute():
        caiyun_script = Path.cwd() / caiyun_script

    log_run_start(
        logger,
        "Caiyun batch started",
        input_path=input_path,
        output_dir=output_dir,
        hourlysteps=args.hourlysteps,
        max_workers=args.max_workers,
        log_dir=args.log_dir,
    )

    try:
        rows = read_coordinate_table(input_path)
        if args.limit is not None:
            rows = rows[: args.limit]

        fields = parse_list_argument(args.fields)
        total = len(rows)
        if total == 0:
            raise ValueError("No coordinate rows found in input file.")

        if args.max_workers < 1:
            raise ValueError("--max-workers must be at least 1")

        futures = []
        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            for row_index, row in enumerate(rows, start=1):
                latitude = float(row["latitude"])
                longitude = float(row["longitude"])
                futures.append(
                    executor.submit(
                        run_one_point,
                        row_index,
                        total,
                        latitude,
                        longitude,
                        output_dir,
                        args.python_exe,
                        caiyun_script,
                        args.token,
                        args.hourlysteps,
                        fields,
                        args.timeout,
                        args.log_dir,
                        logger,
                    )
                )

            for future in as_completed(futures):
                output_path = future.result()
                print(f"Finished: {output_path}")

        log_run_end(
            logger,
            "Caiyun batch completed",
            output_dir=output_dir,
            total_points=total,
        )
        maybe_run_alert_after_download(args, logger)
        print(f"Batch run completed. Files saved to: {output_dir}")
    except Exception as exc:
        log_exception(logger, "Caiyun batch failed", exc)
        raise


if __name__ == "__main__":
    main()
