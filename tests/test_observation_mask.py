from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from nowcast.observation_mask import binary_precipitation_mask, merge_forecast_file
from nowcast.run_observation_masked_alert import parse_run_start, s3_join
from model.run_picking_date_alert import build_point_suitability


class ObservationMaskTests(unittest.TestCase):
    def test_binary_mask_policy_is_isolated(self) -> None:
        values = pd.Series([0.0, 0.1, 0.2, None])
        self.assertEqual(binary_precipitation_mask(values, threshold_mm=0.1).tolist(), [0, 0, 1, 0])

    def test_run_start_is_converted_to_beijing_time(self) -> None:
        result = parse_run_start("2026-08-20T18:30:00Z", "Asia/Shanghai")
        self.assertEqual(result.isoformat(), "2026-08-21T02:30:00+08:00")

    def test_s3_paths_do_not_mix_products(self) -> None:
        self.assertEqual(
            s3_join("s3://bucket/base/", "nowcasting/openmeteo_forecast", "date=2026-08-21"),
            "s3://bucket/base/nowcasting/openmeteo_forecast/date=2026-08-21/",
        )

    def test_only_observed_hours_before_run_start_are_masked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "openmeteo_row001_lat28p200000_lon105p800000.xlsx"
            output = root / "merged.xlsx"
            forecast = pd.DataFrame(
                {
                    "date_local_iso": [
                        "2026-08-20T23:00:00+08:00",
                        "2026-08-21T01:00:00+08:00",
                        "2026-08-21T03:00:00+08:00",
                    ],
                    "rain": [2.0, 3.0, 4.0],
                    "precipitation": [2.5, 3.5, 4.5],
                    "latitude": [28.2] * 3,
                    "longitude": [105.8] * 3,
                }
            )
            forecast.to_excel(source, index=False)
            radar = pd.DataFrame(
                {
                    "point_lon": [105.8, 105.8, 105.8],
                    "point_lat": [28.2, 28.2, 28.2],
                    "observation_hour": pd.to_datetime(
                        ["2026-08-20 23:00", "2026-08-21 01:00", "2026-08-21 03:00"]
                    ),
                    "hourly_precip_mm": [0.0, 0.4, 0.0],
                    "image_count_in_hour": [10, 10, 10],
                }
            )
            observed, masked, matched = merge_forecast_file(
                source,
                output,
                radar,
                window_start=pd.Timestamp("2026-08-20T00:00:00+08:00"),
                run_started_at=pd.Timestamp("2026-08-21T02:30:00+08:00"),
            )
            result = pd.read_excel(output)

            self.assertTrue(matched)
            self.assertEqual(observed, 2)
            self.assertEqual(masked, 1)
            self.assertEqual(result["rain"].tolist(), [0.0, 3.0, 4.0])
            self.assertEqual(result["forecast_original_rain"].tolist(), [2.0, 3.0, 4.0])
            self.assertEqual(result["radar_observation_applied"].tolist(), [True, True, False])

    def test_previous_day_rain_affects_target_day_lookback(self) -> None:
        rows = pd.DataFrame(
            {
                "point_id": ["point", "point"],
                "region_name": ["region", "region"],
                "timestamp": pd.to_datetime(["2026-08-20 23:00", "2026-08-21 00:00"]),
                "rain_mm": [1.0, 0.0],
                "temp_c": [20.0, 20.0],
                "relative_humidity": [70.0, 70.0],
                "solar_wm2": [0.0, 200.0],
                "is_daylight": [0, 1],
            }
        )
        args = argparse.Namespace(
            rain_threshold=0.1,
            max_relative_humidity=85.0,
            current_solar_threshold=80.0,
            drying_solar_threshold=120.0,
            rain_lookback_hours=12,
            min_drying_sun_hours=4,
            min_drying_solar_energy=1.5,
        )
        result = build_point_suitability(rows, args)
        target_row = result.loc[result["timestamp"] == pd.Timestamp("2026-08-21 00:00")].iloc[0]
        self.assertEqual(target_row["post_rain_dry_enough"], 0)
        self.assertEqual(target_row["suitable_for_picking"], 0)

if __name__ == "__main__":
    unittest.main()


