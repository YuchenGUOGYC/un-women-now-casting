from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from nowcast.run_observation_masked_alert import filter_radar_csvs, radar_file_local_timestamp


class RadarCutoffTests(unittest.TestCase):
    def test_utc_filename_is_converted_to_beijing_time(self) -> None:
        path = Path("Z_RADR_I_Z9999_20260820183000000_O_DOR_SA_CAP.bin_roi_pixels.csv")
        timestamp = radar_file_local_timestamp(path, "Asia/Shanghai")
        self.assertEqual(timestamp.isoformat(), "2026-08-21T02:30:00+08:00")

    def test_files_at_or_after_cutoff_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            before = source / "radar_20260820182959000_x_roi_pixels.csv"
            at_cutoff = source / "radar_20260820183000000_x_roi_pixels.csv"
            before.write_text("x", encoding="utf-8")
            at_cutoff.write_text("x", encoding="utf-8")

            selected = filter_radar_csvs(
                source,
                target,
                window_start=pd.Timestamp("2026-08-20T00:00:00+08:00"),
                observation_cutoff=pd.Timestamp("2026-08-21T02:30:00+08:00"),
                timezone="Asia/Shanghai",
            )
            self.assertEqual([path.name for path in selected], [before.name])
            self.assertFalse((target / at_cutoff.name).exists())


if __name__ == "__main__":
    unittest.main()
