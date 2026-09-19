from __future__ import annotations

import hmac
import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit

from .engine import PaperExecutionEngine
from .models import PaperSignal

MAX_BODY_BYTES = 64 * 1024


class SignalService:
    """Bearer-authenticated signal ingress around one serialized paper engine."""

    def __init__(self, engine: PaperExecutionEngine, token: str) -> None:
        if len(token) < 32:
            raise ValueError("service token must contain at least 32 characters")
        self.engine = engine
        self.token = token
        self.started_at = time.time()
        self._stop = threading.Event()
        self._settler: threading.Thread | None = None

    def authorized(self, header: str) -> bool:
        supplied = header.removeprefix("Bearer ")
        return hmac.compare_digest(supplied, self.token)

    def status(self) -> dict[str, Any]:
        return {
            "schema": "polymarket-livetesting-status-v1",
            "paper_only": True,
            "order_submission_enabled": False,
            "uptime_seconds": time.time() - self.started_at,
            "account": self.engine.ledger.snapshot(),
        }

    def start_settlement_loop(self, interval_seconds: float = 15) -> None:
        if interval_seconds <= 0:
            raise ValueError("settlement interval must be positive")
        if self._settler and self._settler.is_alive():
            return

        def settle() -> None:
            while not self._stop.wait(interval_seconds):
                try:
                    self.engine.resolve()
                except (
                    ConnectionError,
                    LookupError,
                    OSError,
                    TimeoutError,
                    TypeError,
                    ValueError,
                ) as error:
                    self.engine.ledger.append_event(
                        {
                            "status": "settlement_error",
                            "error_type": type(error).__name__,
                            "recorded_at_ns": time.time_ns(),
                        }
                    )

        self._settler = threading.Thread(target=settle, name="paper-settlement", daemon=True)
        self._settler.start()

    def stop(self) -> None:
        self._stop.set()
        if self._settler:
            self._settler.join(timeout=2)


def create_server(
    engine: PaperExecutionEngine,
    token: str,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> tuple[ThreadingHTTPServer, SignalService]:
    service = SignalService(engine, token)
    dashboard = files("polymarket_livetesting").joinpath("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "PolymarketLiveTesting/0.1"

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def respond(self, status: int, value: Any, content_type: str = "application/json") -> None:
            body = (
                value
                if isinstance(value, bytes)
                else json.dumps(value, separators=(",", ":"), default=str).encode()
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/v1/health":
                self.respond(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "paper_only": True,
                        "order_submission_enabled": False,
                    },
                )
            elif path == "/v1/status":
                self.respond(HTTPStatus.OK, service.status())
            elif path in {"/", "/dashboard"}:
                self.respond(HTTPStatus.OK, dashboard, "text/html; charset=utf-8")
            else:
                self.respond(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/v1/signals":
                self.respond(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not service.authorized(self.headers.get("Authorization", "")):
                self.respond(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY_BYTES:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise TypeError("signal must be a JSON object")
                result = service.engine.execute(PaperSignal.from_dict(payload))
                self.respond(HTTPStatus.OK, result.to_dict())
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                self.respond(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    server = ThreadingHTTPServer((host, port), Handler)
    return server, service
