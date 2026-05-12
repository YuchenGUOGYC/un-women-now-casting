import argparse
import csv
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common import configure_run_logger, log_exception, log_run_end, log_run_start


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
    return parser.parse_args()


def read_coordinate_table(input_path):
    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        with input_path.open("r", encoding="utf-8-sig", newline="") as csvfile:
            reader = csv.DictReader(csvfile)
            rows = [normalize_row_keys(row) for row in reader]
    else:
        rows = read_xlsx_rows(input_path)

    required_columns = {"latitude", "longitude"}
    available_columns = set(rows[0].keys()) if rows else set()
    missing = required_columns - available_columns
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")

    return rows


def normalize_row_keys(row):
    return {str(key).strip().lower(): value for key, value in row.items()}


def read_xlsx_rows(input_path):
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(input_path) as workbook_zip:
        shared_strings = []
        if "xl/sharedStrings.xml" in workbook_zip.namelist():
            shared_root = ET.fromstring(workbook_zip.read("xl/sharedStrings.xml"))
            for item in shared_root.findall("main:si", namespace):
                text_parts = [node.text or "" for node in item.findall(".//main:t", namespace)]
                shared_strings.append("".join(text_parts))

        sheet_root = ET.fromstring(workbook_zip.read("xl/worksheets/sheet1.xml"))
        rows_xml = sheet_root.findall(".//main:sheetData/main:row", namespace)

        values = []
        for row_xml in rows_xml:
            row_values = []
            current_index = 0
            for cell in row_xml.findall("main:c", namespace):
                cell_ref = cell.attrib.get("r", "")
                target_index = column_letters_to_index("".join(ch for ch in cell_ref if ch.isalpha()))
                while current_index < target_index:
                    row_values.append(None)
                    current_index += 1

                cell_type = cell.attrib.get("t")
                value_node = cell.find("main:v", namespace)
                if value_node is None:
                    cell_value = ""
                elif cell_type == "s":
                    cell_value = shared_strings[int(value_node.text)]
                else:
                    cell_value = value_node.text

                row_values.append(cell_value)
                current_index += 1

            values.append(row_values)

    if not values:
        return []

    headers = [str(cell).strip().lower() if cell is not None else "" for cell in values[0]]
    rows = []
    for value_row in values[1:]:
        row = {}
        for index, header in enumerate(headers):
            if header:
                row[header] = value_row[index] if index < len(value_row) else None
        rows.append(row)
    return rows


def column_letters_to_index(letters):
    result = 0
    for char in letters:
        result = result * 26 + (ord(char.upper()) - ord("A") + 1)
    return max(result - 1, 0)


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


def main():
    logger = configure_run_logger("caiyun.batch", ROOT_DIR / "logs", run_name="caiyun_batch")
    args = parse_args()

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
        print(f"Batch run completed. Files saved to: {output_dir}")
    except Exception as exc:
        log_exception(logger, "Caiyun batch failed", exc)
        raise


if __name__ == "__main__":
    main()
