import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import openmeteo_requests
import pandas as pd
import requests_cache
from retry_requests import retry

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

DEFAULT_HOURLY_VARS = [
    "precipitation_probability",
    "precipitation",
    "rain",
    "showers",
    "temperature_2m",
]
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def get_beijing_today():
    return pd.Timestamp.now(tz=BEIJING_TZ).strftime("%Y-%m-%d")


def parse_list_argument(values):
    items = []
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if part:
                items.append(part)
    return items


def parse_args():
    beijing_today = get_beijing_today()
    parser = argparse.ArgumentParser(description="Download Open-Meteo hourly forecast data.")
    parser.add_argument("--latitude", type=float, default=52.52, help="Latitude of the target point.")
    parser.add_argument("--longitude", type=float, default=13.41, help="Longitude of the target point.")
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
        help=(
            "Hourly variables to request. "
            "Can be repeated or passed as a comma-separated list. "
            f"Default: {','.join(DEFAULT_HOURLY_VARS)}"
        ),
    )
    parser.add_argument(
        "--output",
        default="openmeteo_hourly.xlsx",
        help="Output Excel path. Default: openmeteo_hourly.xlsx",
    )
    add_log_dir_argument(parser, ROOT_DIR / "logs")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = configure_run_logger("openmeteo.single", resolve_log_root(args.log_dir), run_name="openmeteo")
    hourly_vars = parse_list_argument(args.hourly) or DEFAULT_HOURLY_VARS
    log_run_start(
        logger,
        "Open-Meteo run started",
        latitude=args.latitude,
        longitude=args.longitude,
        start_date=args.start_date,
        end_date=args.end_date,
        timezone=args.timezone,
        hourly_vars=hourly_vars,
        log_dir=args.log_dir,
    )

    try:
        cache_session = requests_cache.CachedSession(str(SCRIPT_DIR / ".cache"), expire_after=3600)
        retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
        openmeteo = openmeteo_requests.Client(session=retry_session)

        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": args.latitude,
            "longitude": args.longitude,
            "hourly": hourly_vars,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "timezone": args.timezone,
        }
        logger.info("Requesting Open-Meteo API")
        responses = openmeteo.weather_api(url, params=params)
        all_hourly_frames = []

        for response in responses:
            logger.info(
                "Received response for coordinates lat=%s lon=%s",
                response.Latitude(),
                response.Longitude(),
            )
            print(f"\nCoordinates: {response.Latitude()} N {response.Longitude()} E")
            print(f"Elevation: {response.Elevation()} m asl")
            print(f"Timezone difference to GMT+0: {response.UtcOffsetSeconds()}s")

            hourly = response.Hourly()
            start = pd.to_datetime(hourly.Time(), unit="s", utc=True).tz_convert(args.timezone)
            end = pd.to_datetime(hourly.TimeEnd(), unit="s", utc=True).tz_convert(args.timezone)

            hourly_data = {
                "date": pd.date_range(
                    start=start,
                    end=end,
                    freq=pd.Timedelta(seconds=hourly.Interval()),
                    inclusive="left",
                )
            }

            for idx, variable_name in enumerate(hourly_vars):
                hourly_data[variable_name] = hourly.Variables(idx).ValuesAsNumpy()

            hourly_dataframe = pd.DataFrame(data=hourly_data)
            hourly_dataframe["latitude"] = response.Latitude()
            hourly_dataframe["longitude"] = response.Longitude()
            hourly_dataframe["timezone"] = args.timezone
            hourly_dataframe["utc_offset_seconds"] = response.UtcOffsetSeconds()
            all_hourly_frames.append(hourly_dataframe)
            print(f"Timezone used: {args.timezone}")
            print(f"Hourly variables: {', '.join(hourly_vars)}")
            print("\nHourly data\n", hourly_dataframe)

        output_path = Path(args.output).expanduser()
        if not output_path.is_absolute():
            output_path = Path.cwd() / output_path

        date_folder_name = get_beijing_today()
        output_path = output_path.parent / date_folder_name / output_path.name

        output_path.parent.mkdir(parents=True, exist_ok=True)
        combined_hourly_dataframe = pd.concat(all_hourly_frames, ignore_index=True)
        combined_hourly_dataframe["date_local"] = combined_hourly_dataframe["date"].dt.tz_localize(None)
        combined_hourly_dataframe["date_local_iso"] = combined_hourly_dataframe["date"].map(lambda dt: dt.isoformat())
        combined_hourly_dataframe["date_utc_iso"] = combined_hourly_dataframe["date"].dt.tz_convert("UTC").map(lambda dt: dt.isoformat())
        combined_hourly_dataframe = combined_hourly_dataframe.drop(columns=["date"])

        front_columns = [
            "date_local",
            "date_local_iso",
            "date_utc_iso",
            "timezone",
            "utc_offset_seconds",
            "latitude",
            "longitude",
        ]
        remaining_columns = [col for col in combined_hourly_dataframe.columns if col not in front_columns]
        combined_hourly_dataframe = combined_hourly_dataframe[front_columns + remaining_columns]

        combined_hourly_dataframe.to_excel(output_path, index=False)
        logger.info("Saved Open-Meteo output to %s", output_path)
        log_run_end(
            logger,
            "Open-Meteo run completed",
            output_path=output_path,
            row_count=len(combined_hourly_dataframe),
        )
        print(f"\nSaved Excel to: {output_path}")
        return 0
    except Exception as exc:
        log_exception(logger, "Open-Meteo run failed", exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
