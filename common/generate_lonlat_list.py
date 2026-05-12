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
    configure_run_logger,
    log_exception,
    log_run_end,
    log_run_start,
    resolve_log_root,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a longitude/latitude grid list from a bounding box and resolution."
    )
    parser.add_argument("--west", type=float, required=True, help="Western boundary longitude.")
    parser.add_argument("--south", type=float, required=True, help="Southern boundary latitude.")
    parser.add_argument("--east", type=float, required=True, help="Eastern boundary longitude.")
    parser.add_argument("--north", type=float, required=True, help="Northern boundary latitude.")
    parser.add_argument(
        "--resolution",
        type=float,
        required=True,
        help="Grid spacing in degrees.",
    )
    parser.add_argument(
        "--output",
        default="lonlat_list.xlsx",
        help="Output file path. Supports .xlsx or .csv. Default: lonlat_list.xlsx",
    )
    add_log_dir_argument(parser, ROOT_DIR / "logs")
    return parser.parse_args()


def build_coordinate_list(west, south, east, north, resolution):
    rows = []
    lat = south
    row_id = 1
    while lat <= north + 1e-12:
        lon = west
        col_id = 1
        while lon <= east + 1e-12:
            rows.append(
                {
                    "latitude": round(lat, 8),
                    "longitude": round(lon, 8),
                    "row_id": row_id,
                    "col_id": col_id,
                }
            )
            lon += resolution
            col_id += 1
        lat += resolution
        row_id += 1
    return pd.DataFrame(rows)


def main():
    args = parse_args()
    logger = configure_run_logger("openmeteo.grid", resolve_log_root(args.log_dir), run_name="openmeteo_grid")
    log_run_start(
        logger,
        "Longitude/latitude grid generation started",
        west=args.west,
        south=args.south,
        east=args.east,
        north=args.north,
        resolution=args.resolution,
        output=args.output,
        log_dir=args.log_dir,
    )

    try:
        if args.west >= args.east:
            raise ValueError("--west must be smaller than --east")
        if args.south >= args.north:
            raise ValueError("--south must be smaller than --north")
        if args.resolution <= 0:
            raise ValueError("--resolution must be greater than 0")

        dataframe = build_coordinate_list(
            west=args.west,
            south=args.south,
            east=args.east,
            north=args.north,
            resolution=args.resolution,
        )

        output_path = Path(args.output).expanduser()
        if not output_path.is_absolute():
            output_path = Path.cwd() / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        suffix = output_path.suffix.lower()
        if suffix == ".csv":
            dataframe.to_csv(output_path, index=False)
        else:
            dataframe.to_excel(output_path, index=False)

        log_run_end(
            logger,
            "Longitude/latitude grid generation completed",
            output_path=output_path,
            row_count=len(dataframe),
        )
        print(f"Generated {len(dataframe)} coordinates")
        print(f"Saved to: {output_path}")
    except Exception as exc:
        log_exception(logger, "Longitude/latitude grid generation failed", exc)
        raise


if __name__ == "__main__":
    main()
