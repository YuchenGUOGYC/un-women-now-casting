import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common import configure_run_logger, log_exception, log_run_end, log_run_start

API_VERSION = "v2.6"
API_BASE_URL = "https://api.caiyunapp.com"
BEIJING_TZ = timezone(timedelta(hours=8))
DEFAULT_FIELDS = [
    "precipitation",
    "temperature",
    "wind",
    "humidity",
    "cloudrate",
    "skycon",
]


def get_beijing_today():
    return datetime.now(tz=BEIJING_TZ).strftime("%Y-%m-%d")


def parse_list_argument(values):
    items = []
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if part:
                items.append(part)
    return items


def parse_args():
    parser = argparse.ArgumentParser(description="Download Caiyun hourly weather data for one point.")
    parser.add_argument("--token", required=True, help="Caiyun API token.")
    parser.add_argument("--latitude", type=float, default=39.2072, help="Latitude of the target point.")
    parser.add_argument("--longitude", type=float, default=101.6656, help="Longitude of the target point.")
    parser.add_argument(
        "--hourlysteps",
        type=int,
        default=48,
        help="Hourly steps to request from Caiyun. Default: 48",
    )
    parser.add_argument(
        "--fields",
        action="append",
        help=(
            "Hourly sections to extract. "
            "Can be repeated or passed as a comma-separated list. "
            f"Default: {','.join(DEFAULT_FIELDS)}"
        ),
    )
    parser.add_argument(
        "--output",
        default="caiyun_hourly_precipitation.csv",
        help="Output CSV path. Default: caiyun_hourly_precipitation.csv",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Request timeout in seconds. Default: 60",
    )
    parser.add_argument(
        "--no-date-subdir",
        action="store_true",
        help="Write output directly to the target folder without creating a dated subdirectory.",
    )
    return parser.parse_args()


def build_api_url(token, longitude, latitude, hourlysteps):
    query = urlencode({"hourlysteps": hourlysteps})
    return f"{API_BASE_URL}/{API_VERSION}/{token}/{longitude},{latitude}/hourly?{query}"


def fetch_json(url, timeout, retries=3, backoff_seconds=2):
    last_error = None
    for attempt in range(1, retries + 1):
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_error = exc
            if exc.code != 429 or attempt == retries:
                raise
            time.sleep(backoff_seconds * attempt)

    if last_error is not None:
        raise last_error


def ensure_absolute_path(path_text):
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def set_row_value(row, field_name, item):
    if field_name == "precipitation":
        row["precipitation"] = item.get("value")
        row["precipitation_probability"] = item.get("probability")
        return

    if field_name == "wind":
        row["wind_speed"] = item.get("speed")
        row["wind_direction"] = item.get("direction")
        return

    value = item.get("value")
    if isinstance(value, dict):
        for key, nested_value in value.items():
            row[f"{field_name}_{key}"] = nested_value
        return

    row[field_name] = value


def extract_hourly_rows(response_json, selected_fields):
    hourly = response_json.get("result", {}).get("hourly", {})
    rows_by_datetime = {}

    for field_name in selected_fields:
        entries = hourly.get(field_name, [])
        if not isinstance(entries, list):
            continue
        for item in entries:
            dt_text = item.get("datetime")
            if not dt_text:
                continue
            row = rows_by_datetime.setdefault(
                dt_text,
                {
                    "datetime": dt_text,
                },
            )
            set_row_value(row, field_name, item)

    ordered_datetimes = sorted(rows_by_datetime.keys())
    return [rows_by_datetime[dt_text] for dt_text in ordered_datetimes]


def build_output_paths(output_arg, no_date_subdir=False):
    output_path = ensure_absolute_path(output_arg)
    if no_date_subdir:
        dated_output_path = output_path
    else:
        dated_output_path = output_path.parent / get_beijing_today() / output_path.name
    response_path = dated_output_path.with_name(f"{dated_output_path.stem}_response.json")
    summary_path = dated_output_path.with_name(f"{dated_output_path.stem}_summary.json")
    return dated_output_path, response_path, summary_path


def write_csv(output_path, rows):
    fieldnames = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)

    with output_path.open("w", newline="", encoding="utf-8-sig") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    logger = configure_run_logger("caiyun.hourly", ROOT_DIR / "logs", run_name="caiyun_hourly")
    args = parse_args()
    selected_fields = parse_list_argument(args.fields) or DEFAULT_FIELDS
    log_run_start(
        logger,
        "Caiyun hourly run started",
        latitude=args.latitude,
        longitude=args.longitude,
        hourlysteps=args.hourlysteps,
        selected_fields=selected_fields,
    )
    try:
        api_url = build_api_url(args.token, args.longitude, args.latitude, args.hourlysteps)
        logger.info("Requesting Caiyun hourly API")
        response_json = fetch_json(api_url, timeout=args.timeout)

        if response_json.get("status") != "ok":
            raise RuntimeError(f"Caiyun API request failed: {response_json}")

        rows = extract_hourly_rows(response_json, selected_fields)
        output_path, response_path, summary_path = build_output_paths(
            args.output,
            no_date_subdir=args.no_date_subdir,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_csv(output_path, rows)
        response_path.write_text(json.dumps(response_json, ensure_ascii=False, indent=2), encoding="utf-8")

        result = response_json.get("result", {})
        summary = {
            "api_version": response_json.get("api_version"),
            "location": response_json.get("location"),
            "timezone": response_json.get("timezone"),
            "hourlysteps": args.hourlysteps,
            "row_count": len(rows),
            "selected_fields": selected_fields,
            "forecast_keypoint": result.get("forecast_keypoint"),
            "description": result.get("hourly", {}).get("description"),
            "csv_path": str(output_path),
            "response_path": str(response_path),
        }
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        log_run_end(
            logger,
            "Caiyun hourly run completed",
            csv_path=output_path,
            response_path=response_path,
            row_count=len(rows),
        )
        print(json.dumps(summary, ensure_ascii=True, indent=2))
    except Exception as exc:
        log_exception(logger, "Caiyun hourly run failed", exc)
        raise


if __name__ == "__main__":
    main()
