from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common import configure_run_logger, log_exception, log_run_end, log_run_start
from wxpusher_notify import ConfigError, send_notification


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a WxPusher notification.")
    parser.add_argument(
        "--config",
        default=str(SCRIPT_DIR / "wxpusher.config.json"),
        help="Path to a JSON or YAML config file.",
    )
    parser.add_argument(
        "--title",
        default="天气提醒",
        help="Notification title mapped to WxPusher summary.",
    )
    parser.add_argument(
        "--summary",
        default="现在下雨",
        help="Notification content mapped to WxPusher content.",
    )
    return parser


def main() -> int:
    logger = configure_run_logger("wxpusher.send", ROOT_DIR / "logs", run_name="wxpusher_send")
    parser = build_parser()
    args = parser.parse_args()
    log_run_start(
        logger,
        "WxPusher notification started",
        config_path=args.config,
        title=args.title,
    )

    try:
        result = send_notification(
            title=args.title,
            summary=args.summary,
            config_path=args.config,
        )
    except (ConfigError, ValueError) as exc:
        log_exception(logger, "WxPusher notification failed", exc)
        print(str(exc), file=sys.stderr)
        return 1

    log_run_end(
        logger,
        "WxPusher notification completed",
        success=result.success,
        status_code=result.status_code,
        response_summary=result.response_summary,
    )
    print(
        json.dumps(
            {
                "success": result.success,
                "provider": result.provider,
                "response_summary": result.response_summary,
                "error": result.error,
                "status_code": result.status_code,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
