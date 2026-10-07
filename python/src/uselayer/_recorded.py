"""Recorded HTTP answers, so examples and tests run the same way every time without the network.

Set ``USELAYER_RECORDED=path.json`` to answer every request from that file (paper and backtest mode
only; live mode ignores it). Set ``USELAYER_RECORD=path.json`` to save real answers to it while running.
"""

from __future__ import annotations

import email.utils
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx


def _key(request: httpx.Request) -> str:
    u = urlsplit(str(request.url))
    q = urlencode(sorted(httpx.QueryParams(u.query).multi_items()))
    return f"{request.method} {u.scheme}://{u.netloc}{u.path}" + (f"?{q}" if q else "")


class ReplayTransport(httpx.BaseTransport):
    """Answers requests from a recording; a request it doesn't have fails loudly."""

    def __init__(self, path: str | Path) -> None:
        self.answers: dict[str, list[dict[str, Any]]] = json.loads(Path(path).read_text())
        self.seen: dict[str, int] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        k = _key(request)
        options = self.answers.get(k)
        if not options:
            raise httpx.ConnectError(f"not in the recording: {k}", request=request)
        i = self.seen.get(k, 0)
        self.seen[k] = i + 1
        a = options[min(i, len(options) - 1)]
        # A replayed answer is served now, fresh from the "venue": Date is now and Age is 0.
        now = email.utils.format_datetime(datetime.now(UTC), usegmt=True)
        headers = {
            "content-type": "application/json",
            "date": now,
            "age": "0",
            "cache-control": "public, max-age=30",
        }
        return httpx.Response(a["status"], json=a["body"], headers=headers, request=request)


class RecordTransport(httpx.BaseTransport):
    """Sends requests for real and saves each answer to a file. Never records request headers."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.inner = httpx.HTTPTransport()
        self.answers: dict[str, list[dict[str, Any]]] = (
            json.loads(self.path.read_text()) if self.path.exists() else {}
        )
        self.lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        resp = self.inner.handle_request(request)
        resp.read()
        if "uselayer.sh" not in (request.url.host or ""):
            try:
                body = json.loads(resp.content)
            except ValueError:
                body = resp.text
            with self.lock:
                self.answers.setdefault(_key(request), []).append({"status": resp.status_code, "body": body})
                self.path.write_text(json.dumps(self.answers, indent=1))
        return resp
