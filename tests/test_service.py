from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from polymarket_livetesting import ExecutionConfig, PaperExecutionEngine, PaperLedger
from polymarket_livetesting.demo import DemoMarketData
from polymarket_livetesting.service import create_server

TOKEN = "test-token-that-is-at-least-thirty-two-characters"


def request(url: str, method: str = "GET", payload=None, token: str | None = None):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    call = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(call, timeout=5) as response:
        return response.status, json.load(response)


def test_service_rejects_unauthorized_signal_and_executes_authorized_signal(tmp_path) -> None:
    config = ExecutionConfig(route_latency_ms=0)
    ledger = PaperLedger(tmp_path / "state.json", tmp_path / "events.jsonl", config)
    engine = PaperExecutionEngine(DemoMarketData(), ledger, config)
    server, service = create_server(engine, TOKEN, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    payload = {
        "signal_id": "http:1",
        "market_slug": DemoMarketData.slug,
        "outcome": "yes",
        "probability": 0.7,
        "emitted_at_ns": time.time_ns(),
        "model": "http-test",
    }
    try:
        status, health = request(base + "/v1/health")
        assert status == 200 and health["order_submission_enabled"] is False
        with pytest.raises(urllib.error.HTTPError) as failure:
            request(base + "/v1/signals", "POST", payload)
        assert failure.value.code == 401

        status, result = request(base + "/v1/signals", "POST", payload, TOKEN)
        _, snapshot = request(base + "/v1/status")

        assert status == 200 and result["status"] == "filled"
        assert snapshot["paper_only"] is True
        assert snapshot["account"]["open_positions"] == 1
    finally:
        server.shutdown()
        service.stop()
        server.server_close()
        thread.join(timeout=2)
