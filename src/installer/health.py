"""The post-start health check: ``GET http://127.0.0.1:<port><path>`` until it reports the
installed version (specification 6.3, step 9)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from .versions import Version


@dataclass(frozen=True)
class HealthResponse:
    status: int
    body: bytes


# Raises ``OSError`` when nothing answers (the application may still be starting).
HealthChecker = Callable[[str], HealthResponse]


def http_health(url: str, timeout: float = 5.0) -> HealthResponse:
    """Plain ``GET`` with a short timeout. HTTP error statuses are answers, not exceptions."""
    request = urllib.request.Request(url, headers={"User-Agent": "installer"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return HealthResponse(response.status, response.read(65536))
    except urllib.error.HTTPError as exc:
        try:
            return HealthResponse(exc.code, exc.read(65536))
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        raise OSError(str(exc.reason)) from exc


def is_healthy(response: HealthResponse, version_field: str, expected: Version) -> bool:
    """Healthy means: HTTP 200 and the JSON ``version_field`` equals the installed version."""
    if response.status != 200:
        return False
    try:
        data = json.loads(response.body)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    reported = data.get(version_field)
    if not isinstance(reported, str):
        return False
    parsed = Version.try_parse(reported)
    return parsed == expected if parsed is not None else False
