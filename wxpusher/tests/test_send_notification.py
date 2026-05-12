from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

SCRIPT_DIR = Path(__file__).resolve().parent
WXPUSHER_DIR = SCRIPT_DIR.parent
if str(WXPUSHER_DIR) not in sys.path:
    sys.path.insert(0, str(WXPUSHER_DIR))

from wxpusher_notify import ConfigError, load_config, send_notification


def write_config(directory: Path, payload: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "wxpusher.config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class WxPusherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(__file__).resolve().parent / ".tmp"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        for path in sorted(self.workspace.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        if self.workspace.exists():
            self.workspace.rmdir()

    def test_load_config_reads_required_values(self) -> None:
        config_path = write_config(
            self.workspace / "test_load_config",
            {
                "provider": "wxpusher",
                "timeout_seconds": 12,
                "wxpusher": {
                    "app_token": "AT_test",
                    "uids": ["UID_1"],
                    "content_type": 1,
                },
            },
        )

        config = load_config(config_path)

        self.assertEqual(config["wxpusher"]["app_token"], "AT_test")
        self.assertEqual(config["wxpusher"]["uids"], ["UID_1"])
        self.assertEqual(config["timeout_seconds"], 12.0)

    def test_send_notification_maps_summary_and_title(self) -> None:
        config_path = write_config(
            self.workspace / "test_send_notification_maps",
            {
                "provider": "wxpusher",
                "wxpusher": {
                    "app_token": "AT_test",
                    "uids": ["UID_1"],
                    "content_type": 1,
                },
            },
        )

        captured = {}

        def fake_urlopen(request, timeout):
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(200, {"code": 1000, "msg": "处理成功"})

        with patch("wxpusher_notify.client.urlopen", side_effect=fake_urlopen):
            result = send_notification(
                title="天气提醒",
                summary="现在下雨",
                config_path=str(config_path),
            )

        self.assertTrue(result.success)
        self.assertEqual(captured["payload"]["content"], "现在下雨")
        self.assertEqual(captured["payload"]["summary"], "天气提醒")
        self.assertEqual(captured["payload"]["uids"], ["UID_1"])
        self.assertEqual(captured["timeout"], 10.0)

    def test_send_notification_requires_app_token(self) -> None:
        config_path = write_config(
            self.workspace / "test_requires_app_token",
            {
                "provider": "wxpusher",
                "wxpusher": {
                    "uids": ["UID_1"],
                    "content_type": 1,
                },
            },
        )

        with self.assertRaises(ConfigError):
            send_notification(
                title="天气提醒",
                summary="现在下雨",
                config_path=str(config_path),
            )

    def test_send_notification_requires_uids(self) -> None:
        config_path = write_config(
            self.workspace / "test_requires_uids",
            {
                "provider": "wxpusher",
                "wxpusher": {
                    "app_token": "AT_test",
                    "content_type": 1,
                },
            },
        )

        with self.assertRaises(ConfigError):
            send_notification(
                title="天气提醒",
                summary="现在下雨",
                config_path=str(config_path),
            )

    def test_send_notification_returns_error_on_api_failure(self) -> None:
        config_path = write_config(
            self.workspace / "test_api_failure",
            {
                "provider": "wxpusher",
                "wxpusher": {
                    "app_token": "AT_test",
                    "uids": ["UID_1"],
                    "content_type": 1,
                },
            },
        )

        with patch(
            "wxpusher_notify.client.urlopen",
            side_effect=URLError("network down"),
        ):
            result = send_notification(
                title="天气提醒",
                summary="现在下雨",
                config_path=str(config_path),
            )

        self.assertFalse(result.success)
        self.assertEqual(result.provider, "wxpusher")
        self.assertIn("network down", result.error or "")


if __name__ == "__main__":
    unittest.main()
