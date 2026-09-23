from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ConfigError(ValueError):
    """Raised when notification configuration is invalid."""


def _load_uids_csv(url: str, timeout_seconds: float) -> list[str]:
    request = Request(
        url,
        headers={"Cache-Control": "no-cache", "User-Agent": "un-women-now-casting/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read()
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ConfigError(f"Failed to download WxPusher UID CSV from {url}: {exc}") from exc

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigError("WxPusher UID CSV must be UTF-8 encoded") from exc

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ConfigError("WxPusher UID CSV is empty")
    fieldnames = {name.strip().lower(): name for name in reader.fieldnames if name}
    if "uid" not in fieldnames:
        raise ConfigError("WxPusher UID CSV must contain a 'uid' column")

    uid_column = fieldnames["uid"]
    uids: list[str] = []
    seen: set[str] = set()
    for row_number, row in enumerate(reader, start=2):
        uid = (row.get(uid_column) or "").strip()
        if not uid:
            raise ConfigError(f"WxPusher UID CSV row {row_number} has an empty uid")
        if not uid.startswith("UID_"):
            raise ConfigError(f"WxPusher UID CSV row {row_number} has an invalid uid")
        if uid not in seen:
            seen.add(uid)
            uids.append(uid)

    if not uids:
        raise ConfigError("WxPusher UID CSV contains no recipients")
    return uids


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON config in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be an object in {path}")
    return data


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ConfigError(
            f"YAML config requires PyYAML to be installed: {path}"
        ) from exc

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a mapping in {path}")
    return data


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    suffix = config_path.suffix.lower()
    if suffix == ".json":
        config = _load_json(config_path)
    elif suffix in {".yaml", ".yml"}:
        config = _load_yaml(config_path)
    else:
        raise ConfigError(
            f"Unsupported config format for {config_path}. Use .json, .yaml, or .yml."
        )

    timeout_seconds = config.get("timeout_seconds", 10)
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ConfigError("timeout_seconds must be a positive number")
    timeout_seconds = float(timeout_seconds)

    provider = config.get("provider")
    if provider != "wxpusher":
        raise ConfigError("Only provider 'wxpusher' is supported in this version")

    wxpusher = config.get("wxpusher")
    if not isinstance(wxpusher, dict):
        raise ConfigError("Missing 'wxpusher' configuration block")

    app_token = wxpusher.get("app_token")
    if not isinstance(app_token, str) or not app_token.strip():
        raise ConfigError("Missing required config value: wxpusher.app_token")

    uids = wxpusher.get("uids")
    if not isinstance(uids, list) or not uids or not all(
        isinstance(uid, str) and uid.strip() for uid in uids
    ):
        raise ConfigError("Missing required config value: wxpusher.uids")

    uids = [uid.strip() for uid in uids]
    uids_csv_url = wxpusher.get("uids_csv_url")
    if uids_csv_url is not None:
        if not isinstance(uids_csv_url, str) or not uids_csv_url.strip():
            raise ConfigError("wxpusher.uids_csv_url must be a non-empty URL")
        source_mode = wxpusher.get("uids_source_mode", "replace")
        if source_mode not in {"replace", "append"}:
            raise ConfigError("wxpusher.uids_source_mode must be 'replace' or 'append'")
        remote_uids = _load_uids_csv(uids_csv_url.strip(), timeout_seconds)
        if source_mode == "replace":
            uids = remote_uids
        else:
            uids = list(dict.fromkeys([*uids, *remote_uids]))

    content_type = wxpusher.get("content_type", 1)
    if not isinstance(content_type, int):
        raise ConfigError("wxpusher.content_type must be an integer")

    normalized = {
        "provider": provider,
        "wxpusher": {
            "app_token": app_token.strip(),
            "uids": uids,
            "content_type": content_type,
        },
    }

    normalized["timeout_seconds"] = timeout_seconds

    return normalized
