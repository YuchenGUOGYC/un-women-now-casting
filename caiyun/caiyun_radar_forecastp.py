import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common import configure_run_logger, log_exception, log_run_end, log_run_start

API_URL = "https://api.caiyunapp.com/v1/radar/cndata/forecastp"
BEIJING_TZ = timezone(timedelta(hours=8))


def get_beijing_today():
    return datetime.now(tz=BEIJING_TZ).strftime("%Y-%m-%d")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download Caiyun radar forecast precipitation grid files for one province."
    )
    parser.add_argument("--token", required=True, help="Caiyun API token.")
    parser.add_argument("--province-id", required=True, type=int, help="Province ID, for example 11 or 51.")
    parser.add_argument(
        "--output-dir",
        default="caiyun_radar_forecastp",
        help="Base output directory. Default: caiyun_radar_forecastp",
    )
    parser.add_argument(
        "--variable",
        default="precp",
        help="Target variable in cndata. Default: precp",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Request timeout in seconds. Default: 120",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing .nc files if they already exist.",
    )
    return parser.parse_args()


def ensure_absolute_path(path_text):
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def fetch_json(url, params, timeout):
    request_url = f"{url}?{urlencode(params)}"
    request = Request(
        request_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def download_file(url, output_path, timeout):
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        output_path.write_bytes(response.read())


def pick_dataset(cndata_items, variable, province_id):
    for item in cndata_items:
        if item.get("variable") == variable and int(item.get("province_id", -1)) == province_id:
            return item
    if cndata_items:
        return cndata_items[0]
    raise ValueError("API returned an empty cndata list.")


def main():
    logger = configure_run_logger("caiyun.radar", ROOT_DIR / "logs", run_name="caiyun_radar")
    args = parse_args()
    output_dir = ensure_absolute_path(args.output_dir)
    log_run_start(
        logger,
        "Caiyun radar run started",
        province_id=args.province_id,
        variable=args.variable,
        output_dir=output_dir,
        overwrite=args.overwrite,
    )

    try:
        dated_dir = output_dir / get_beijing_today() / f"province_{args.province_id}"
        files_dir = dated_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)

        response_json = fetch_json(
            API_URL,
            params={"token": args.token, "province_id": args.province_id},
            timeout=args.timeout,
        )
        status = response_json.get("status")
        if status != "ok":
            raise RuntimeError(f"API request failed with status={status!r}: {response_json}")

        dataset = pick_dataset(response_json.get("cndata", []), args.variable, args.province_id)
        data_urls = dataset.get("data_url", [])
        if not data_urls:
            raise ValueError("No data_url entries were returned by the API.")

        metadata_path = dated_dir / "forecastp_response.json"
        metadata_path.write_text(json.dumps(response_json, ensure_ascii=False, indent=2), encoding="utf-8")

        manifest_path = dated_dir / "download_manifest.csv"
        rows = []
        for index, data_url in enumerate(data_urls, start=1):
            file_name = Path(urlparse(data_url).path).name
            output_path = files_dir / file_name
            if output_path.exists() and not args.overwrite:
                status_text = "skipped_existing"
            else:
                download_file(data_url, output_path, timeout=args.timeout)
                status_text = "downloaded"

            logger.info(
                "Radar file %s/%s %s -> %s",
                index,
                len(data_urls),
                status_text,
                output_path,
            )
            rows.append(
                {
                    "index": index,
                    "province_id": dataset.get("province_id"),
                    "variable": dataset.get("variable"),
                    "source_datetime": dataset.get("datetime"),
                    "file_name": file_name,
                    "status": status_text,
                    "download_url": data_url,
                    "saved_path": str(output_path),
                }
            )
            print(f"[{index}/{len(data_urls)}] {status_text}: {output_path}")

        with manifest_path.open("w", newline="", encoding="utf-8-sig") as csvfile:
            writer = csv.DictWriter(
                csvfile,
                fieldnames=[
                    "index",
                    "province_id",
                    "variable",
                    "source_datetime",
                    "file_name",
                    "status",
                    "download_url",
                    "saved_path",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)

        summary = {
            "metadata_path": str(metadata_path),
            "manifest_path": str(manifest_path),
            "files_dir": str(files_dir),
            "province_id": dataset.get("province_id"),
            "variable": dataset.get("variable"),
            "source_datetime": dataset.get("datetime"),
            "file_count": len(rows),
        }
        summary_path = dated_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        log_run_end(
            logger,
            "Caiyun radar run completed",
            manifest_path=manifest_path,
            file_count=len(rows),
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception as exc:
        log_exception(logger, "Caiyun radar run failed", exc)
        raise


if __name__ == "__main__":
    main()
