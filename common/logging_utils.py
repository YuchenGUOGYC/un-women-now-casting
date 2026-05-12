from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, tuple)):
        return list(value)  # type: ignore[return-value]
    return repr(value)


def _normalize_for_log(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=_json_default)
    except TypeError:
        return repr(value)


def configure_run_logger(
    logger_name: str,
    log_root: Path,
    *,
    run_name: str | None = None,
    level: int = logging.INFO,
) -> logging.Logger:
    logger = logging.getLogger(logger_name)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    run_label = run_name or logger_name.replace(".", "_")
    dated_dir = Path(log_root).expanduser() / datetime.now().strftime("%Y-%m-%d")
    dated_dir.mkdir(parents=True, exist_ok=True)
    log_path = dated_dir / f"{run_label}.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(level)
    logger.addHandler(stream_handler)

    logger.info("Logger initialized: %s", log_path)
    return logger


def log_exception(logger: logging.Logger, message: str, exc: BaseException) -> None:
    logger.exception("%s: %s", message, exc)


def log_run_start(logger: logging.Logger, event: str, **details: Any) -> None:
    if details:
        logger.info("%s | %s", event, _normalize_for_log(details))
        return
    logger.info("%s", event)


def log_run_end(logger: logging.Logger, event: str, **details: Any) -> None:
    if details:
        logger.info("%s | %s", event, _normalize_for_log(details))
        return
    logger.info("%s", event)
