from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import load_config

WXPUSHER_API_URL = "https://wxpusher.zjiecode.com/api/send/message"


@dataclass
class SendResult:
    success: bool
    provider: str
    response_summary: str
    error: str | None = None
    status_code: int | None = None
    response_data: dict[str, Any] | None = None


def _build_payload(config: dict[str, Any], title: str | None, summary: str) -> dict[str, Any]:
    wxpusher = config["wxpusher"]
    return {
        "appToken": wxpusher["app_token"],
        "content": summary,
        "summary": title or "通知",
        "contentType": wxpusher["content_type"],
        "uids": wxpusher["uids"],
    }


def _parse_response(status_code: int, body: bytes) -> SendResult:
    try:
        response = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return SendResult(
            success=False,
            provider="wxpusher",
            response_summary="WxPusher returned a non-JSON response",
            error=body.decode("utf-8", errors="replace"),
            status_code=status_code,
        )

    code = response.get("code")
    msg = response.get("msg", "")
    success = status_code == 200 and code == 1000
    return SendResult(
        success=success,
        provider="wxpusher",
        response_summary=msg or ("Message sent" if success else "WxPusher request failed"),
        error=None if success else msg or "WxPusher request failed",
        status_code=status_code,
        response_data=response,
    )


def send_notification(
    title: str | None,
    summary: str,
    config_path: str = "wxpusher.config.json",
) -> SendResult:
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary must be a non-empty string")

    config = load_config(config_path)
    payload = _build_payload(config, title=title, summary=summary.strip())
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        WXPUSHER_API_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=config["timeout_seconds"]) as response:
            return _parse_response(response.status, response.read())
    except HTTPError as exc:
        body = exc.read()
        result = _parse_response(exc.code, body)
        if result.error is None:
            result.error = f"HTTP error {exc.code}"
        return result
    except URLError as exc:
        return SendResult(
            success=False,
            provider="wxpusher",
            response_summary="Failed to reach WxPusher",
            error=str(exc.reason),
        )

