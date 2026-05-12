from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when notification configuration is invalid."""


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

    content_type = wxpusher.get("content_type", 1)
    if not isinstance(content_type, int):
        raise ConfigError("wxpusher.content_type must be an integer")

    normalized = {
        "provider": provider,
        "wxpusher": {
            "app_token": app_token.strip(),
            "uids": [uid.strip() for uid in uids],
            "content_type": content_type,
        },
    }

    timeout_seconds = config.get("timeout_seconds", 10)
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ConfigError("timeout_seconds must be a positive number")
    normalized["timeout_seconds"] = float(timeout_seconds)

    return normalized
