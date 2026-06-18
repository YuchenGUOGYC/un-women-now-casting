from __future__ import annotations

import argparse
import sys
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
    get_region_name,
    log_exception,
    log_run_end,
    log_run_start,
    resolve_log_root,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Annotate a lon/lat Excel or CSV file with region names.")
    parser.add_argument("--input", required=True, help="Input coordinate Excel/CSV file.")
    parser.add_argument(
        "--output",
        help="Optional output file path. Default: overwrite the input file.",
    )
    parser.add_argument(
        "--region-column",
        default="region_name",
        help="Output region column name. Default: region_name",
    )
    add_log_dir_argument(parser, ROOT_DIR / "logs")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = configure_run_logger(
        "common.annotate_lonlat_regions",
        resolve_log_root(args.log_dir),
        run_name="annotate_lonlat_regions",
    )
    log_run_start(
        logger,
        "Annotate lonlat regions started",
        input=args.input,
        output=args.output,
        region_column=args.region_column,
        log_dir=args.log_dir,
    )

    try:
        input_path = Path(args.input).expanduser()
        if not input_path.is_absolute():
            input_path = Path.cwd() / input_path

        output_path = Path(args.output).expanduser() if args.output else input_path
        if not output_path.is_absolute():
            output_path = Path.cwd() / output_path

        if input_path.suffix.lower() == ".csv":
            dataframe = pd.read_csv(input_path)
        else:
            dataframe = pd.read_excel(input_path)

        dataframe.columns = [str(col).strip() for col in dataframe.columns]
        lower_map = {str(col).strip().lower(): col for col in dataframe.columns}
        if "latitude" not in lower_map or "longitude" not in lower_map:
            raise ValueError("Input file must contain latitude and longitude columns.")

        latitude_col = lower_map["latitude"]
        longitude_col = lower_map["longitude"]
        context = build_region_context(input_path)
        dataframe[args.region_column] = dataframe.apply(
            lambda row: get_region_name(float(row[longitude_col]), float(row[latitude_col]), context),
            axis=1,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.suffix.lower() == ".csv":
            dataframe.to_csv(output_path, index=False)
        else:
            dataframe.to_excel(output_path, index=False)

        log_run_end(
            logger,
            "Annotate lonlat regions completed",
            output_path=output_path,
            row_count=len(dataframe),
            region_column=args.region_column,
        )
        print(f"Saved annotated coordinate file to: {output_path}")
        return 0
    except Exception as exc:
        log_exception(logger, "Annotate lonlat regions failed", exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
