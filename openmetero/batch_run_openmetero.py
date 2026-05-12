import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
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


def parse_list_argument(values):
    items = []
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if part:
                items.append(part)
    return items


def get_beijing_today():
    return pd.Timestamp.now(tz=ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def parse_args():
    beijing_today = get_beijing_today()
    parser = argparse.ArgumentParser(
        description="Run openmetero.py in batch mode from a latitude/longitude list."
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Input coordinate file path (.xlsx or .csv). Must contain latitude and longitude columns.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for per-point output Excel files.",
    )
    parser.add_argument(
        "--openmetero-script",
        default=str(SCRIPT_DIR / "openmetero.py"),
        help="Path to openmetero.py. Default: the sibling script in this folder.",
    )
    parser.add_argument(
        "--python-exe",
        default=sys.executable,
        help="Python executable used to run openmetero.py. Default: current Python.",
    )
    parser.add_argument("--start-date", default=beijing_today, help="Start date in YYYY-MM-DD format.")
    parser.add_argument("--end-date", default=beijing_today, help="End date in YYYY-MM-DD format.")
    parser.add_argument(
        "--timezone",
        default="Asia/Shanghai",
        help="Timezone name such as Asia/Shanghai or GMT.",
    )
    parser.add_argument(
        "--hourly",
        action="append",
        help="Hourly variables to request. Can be repeated or passed as a comma-separated list.",
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
    add_log_dir_argument(parser, ROOT_DIR / "logs")
    return parser.parse_args()


def read_coordinate_table(input_path):
    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        dataframe = pd.read_csv(input_path)
    else:
        dataframe = pd.read_excel(input_path)

    dataframe.columns = [str(col).strip().lower() for col in dataframe.columns]
    required_columns = {"latitude", "longitude"}
    missing = required_columns - set(dataframe.columns)
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")

    return dataframe


def build_output_path(output_dir, latitude, longitude, row_number):
    safe_lat = f"{latitude:.6f}".replace("-", "m").replace(".", "p")
    safe_lon = f"{longitude:.6f}".replace("-", "m").replace(".", "p")
    return output_dir / f"openmeteo_row{row_number:03d}_lat{safe_lat}_lon{safe_lon}.xlsx"


def run_one_point(
    row_index,
    total,
    latitude,
    longitude,
    output_dir,
    python_exe,
    openmetero_script,
    start_date,
    end_date,
    timezone,
    hourly_vars,
    log_dir,
    logger,
):
    output_path = build_output_path(output_dir, latitude, longitude, row_index)
    command = [
        python_exe,
        str(openmetero_script),
        "--latitude",
        str(latitude),
        "--longitude",
        str(longitude),
        "--start-date",
        start_date,
        "--end-date",
        end_date,
        "--timezone",
        timezone,
        "--output",
        str(output_path),
    ]

    for variable in hourly_vars:
        command.extend(["--hourly", variable])
    command.extend(["--log-dir", str(log_dir)])

    logger.info(
        "Starting batch item %s/%s for lat=%s lon=%s output=%s",
        row_index,
        total,
        latitude,
        longitude,
        output_path,
    )
    print(f"[{row_index}/{total}] Running latitude={latitude}, longitude={longitude}")
    subprocess.run(command, check=True)
    logger.info("Finished batch item %s/%s -> %s", row_index, total, output_path)
    return output_path


def main():
    args = parse_args()
    logger = configure_run_logger("openmeteo.batch", resolve_log_root(args.log_dir), run_name="openmeteo_batch")

    input_path = Path(args.input).expanduser()
    if not input_path.is_absolute():
        input_path = Path.cwd() / input_path

    output_dir = Path(args.output_dir).expanduser()
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    openmetero_script = Path(args.openmetero_script).expanduser()
    if not openmetero_script.is_absolute():
        openmetero_script = Path.cwd() / openmetero_script

    log_run_start(
        logger,
        "Open-Meteo batch started",
        input_path=input_path,
        output_dir=output_dir,
        start_date=args.start_date,
        end_date=args.end_date,
        timezone=args.timezone,
        max_workers=args.max_workers,
        log_dir=args.log_dir,
    )

    try:
        dataframe = read_coordinate_table(input_path)
        if args.limit is not None:
            dataframe = dataframe.head(args.limit)

        hourly_vars = parse_list_argument(args.hourly)
        total = len(dataframe)
        if total == 0:
            raise ValueError("No coordinate rows found in input file.")

        if args.max_workers < 1:
            raise ValueError("--max-workers must be at least 1")

        futures = []
        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            for row_index, row in enumerate(dataframe.itertuples(index=False), start=1):
                latitude = float(row.latitude)
                longitude = float(row.longitude)
                futures.append(
                    executor.submit(
                        run_one_point,
                        row_index,
                        total,
                        latitude,
                        longitude,
                        output_dir,
                        args.python_exe,
                        openmetero_script,
                        args.start_date,
                        args.end_date,
                        args.timezone,
                        hourly_vars,
                        args.log_dir,
                        logger,
                    )
                )

            for future in as_completed(futures):
                output_path = future.result()
                print(f"Finished: {output_path}")

        log_run_end(
            logger,
            "Open-Meteo batch completed",
            output_dir=output_dir,
            total_points=total,
        )
        print(f"Batch run completed. Files saved to: {output_dir}")
    except Exception as exc:
        log_exception(logger, "Open-Meteo batch failed", exc)
        raise


if __name__ == "__main__":
    main()
