# Polymarket Live-Testing Framework

A read-only, live-data paper-execution framework for binary Polymarket markets. It is the reusable boundary extracted from a running live-testing system: strategy decisions enter through a narrow signal contract; the framework discovers the exact market, waits modeled latency, reads executable CLOB depth, simulates an IOC fill, and later settles both the paper position and an execution-independent validation record.

This repository contains **no trading path**. It has no wallet support, signing code, private API client, or order-creation method. The built-in adapter performs public HTTPS `GET` requests only.

## What is included

- Exact-slug Gamma discovery. Fuzzy market selection is never used.
- Gamma/CLOB condition and token-map reconciliation.
- Delayed, age-checked order-book snapshots.
- Displayed-depth haircut, bounded ask sweep, minimum-size/notional rules, and rounded crypto taker fees.
- Fixed paper risk budget: available cash, account fraction, and per-market cap.
- Atomic account state and an fsynced append-only JSONL audit log.
- Signal and market deduplication; at most one paper position per condition.
- Independent strategy-validation ledger. A selected signal is resolved even when its simulated execution is skipped.
- Immutable market-ID/slug/condition checks at settlement.
- Bearer-authenticated HTTP signal ingress and a read-only dashboard.
- Python strategy and market-data protocols for plug-ins.
- Deterministic offline smoke scenario.

## Architecture

```text
strategy/model plug-in ─┐
                        ├─ PaperSignal ─> execution engine ─> paper ledger
HTTP signal producer ───┘                      │
                                              ├─ exact market discovery
public Gamma + CLOB adapter <──────────────────┼─ delayed book snapshot
                                              ├─ fee/depth IOC simulation
                                              └─ immutable settlement check
```

The framework does not score models, alter strategy decisions, or add an implied-edge filter. A strategy owns its data, features, model, and `trade_intent`. The framework owns timing validation, public-market identity, paper execution, persistence, and settlement.

## Install and prove the package

Python 3.11 or newer is required.

```bash
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
pm-live smoke
pytest
```

`pm-live smoke` uses no network. It exercises plug-in output, a delayed depth fill, fee accounting, durable state, position settlement, and independent strategy validation.

## HTTP integration

Create a token outside the repository:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))" > /secure/path/signal.token
pm-live serve \
  --state-dir /var/lib/polymarket-livetesting \
  --config examples/config.example.json \
  --token-file /secure/path/signal.token \
  --host 127.0.0.1 --port 8765
```

Submit a signal from a model process:

```bash
pm-live submit examples/signal.example.json \
  --token-file /secure/path/signal.token \
  --endpoint http://127.0.0.1:8765/v1/signals
```

Endpoints:

| Method | Path | Authentication | Purpose |
| --- | --- | --- | --- |
| `GET` | `/v1/health` | none | Liveness and paper-only flag |
| `GET` | `/v1/status` | none | Read-only account summary |
| `GET` | `/dashboard` | none | Read-only local dashboard |
| `POST` | `/v1/signals` | bearer token | Submit one strategy decision |

Bind to loopback unless an authenticated reverse proxy or private network controls access. Read-only endpoints expose account statistics and should still be treated as operational metadata.

### Signal contract

```json
{
  "signal_id": "model-v1:exact-market-slug:decision-1",
  "market_slug": "exact-market-slug",
  "outcome": "yes",
  "probability": 0.63,
  "emitted_at_ns": 1789808400000000000,
  "expected_at_ns": 1789808400000000000,
  "model": "model-v1",
  "trade_intent": true,
  "metadata": {"input_receipt": "strategy-owned-provenance"}
}
```

- `signal_id` must be globally unique. Replays are skipped.
- `market_slug` must be the exact Gamma slug.
- `outcome` is case-folded and must match one of the market's two outcomes.
- `probability` is the strategy's probability that its chosen outcome wins. It is recorded for diagnostics; it is not an execution gate.
- `emitted_at_ns` is required. If `expected_at_ns` is present, the configured timing tolerance is enforced.
- `trade_intent=false` records an abstention without market or book access.
- `metadata` is persisted with the signal. Do not put credentials in it.

## Python strategy plug-in

A plug-in implements one method:

```python
from polymarket_livetesting import PaperSignal


class Strategy:
    def next_signal(self, now_ns: int) -> PaperSignal | None: ...
```

Load a plug-in object, a class accepting a JSON settings dictionary, or a factory with the same settings argument:

```bash
pm-live run-plugin examples.simple_strategy:build \
  --plugin-config /path/to/strategy-settings.json \
  --config examples/config.example.json \
  --state-dir /var/lib/polymarket-livetesting
```

Use `--once` for a single evaluation. `examples/simple_strategy.py` is deliberately model-free; replace it with your own causal data/model component.

## Custom market-data adapter

Implement `polymarket_livetesting.protocols.MarketData`:

```python
class MarketData:
    def discover(self, slug: str, refresh: bool = False) -> BinaryMarket: ...
    def book(self, token_id: str, expected_condition: str) -> BookSnapshot: ...
    def resolved_outcome(
        self, market_id: str, expected_slug: str, expected_condition: str
    ) -> str | None: ...
```

Inject it into `PaperExecutionEngine`. This boundary makes deterministic tests and alternate read-only transports possible without changing fill or ledger semantics.

## Execution semantics

For each selected signal, the engine:

1. deduplicates the signal and registers independent validation;
2. rejects stale, future-dated, or mistimed decisions;
3. discovers exactly one market by slug and validates its binary token mapping;
4. requires active/open/order-accepting state and no prior trade for the condition;
5. waits route latency plus the market's taker delay;
6. fetches one internally consistent book for the selected token;
7. rejects empty, crossed, stale, or misidentified depth;
8. haircuts displayed size and sweeps asks only within configured slippage;
9. applies per-level rounded fees and never exceeds available paper cash or budget;
10. records the simulated fill or exact skip reason.

Settlement re-fetches by immutable market ID and requires matching slug and condition ID. A winner is accepted only when exactly one outcome price is at least `0.999`.

## Configuration

`examples/config.example.json` lists every supported execution field. Unknown fields fail closed. Important defaults:

- `$200` starting paper capital;
- maximum `10%` of equity and `$20` per market;
- `10%` displayed-depth haircut;
- at most `2¢` above the best ask;
- `80 ms` route delay plus Polymarket's reported taker delay;
- `2.5 s` maximum book age and `5 s` maximum signal age.

These are simulation assumptions, not claims about achievable real fills. Tune them explicitly for the experiment being run and preserve the configuration with the resulting audit log.

## Persistence and operations

The state directory contains:

- `state.json`: atomically replaced authoritative paper account state;
- `events.jsonl`: fsynced append-only decisions, fills, skips, settlement records, and errors.

Run only one mutating framework process per state directory. Use separate directories for separate strategies or parameter sets. The sample systemd unit in `deploy/` binds the service to loopback and gives it a dedicated state directory; review its user, paths, and sandbox before installation.

## Public-repository boundary

This package intentionally excludes:

- private models, policies, checkpoints, and historical datasets;
- live account state, raw captures, prediction logs, and bearer tokens;
- server addresses, usernames, SSH material, and deployment-specific overrides;
- any production order path.

It is a testing framework, not evidence of model accuracy, execution quality, or profitability.
