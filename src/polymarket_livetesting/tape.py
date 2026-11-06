"""Recorded market data ("tape"): hourly gzip JSONL files, one event per line, in receive order.

Each line is ``{"t": <receive time in ns>, "k": <kind>, "m": <payload>}``. Kinds:

- ``hdr``: file header with the schema version and whether the data is synthetic.
- ``market``: identity of one market (slug, tokens, start, end, fee rate).
- ``pm``: one raw message from Polymarket's public market WebSocket.
- ``ref``: one reference price update (for example Binance best bid and ask).
- ``result``: the winning outcome of a resolved market.
"""

from __future__ import annotations

import gzip
import json
import time
import uuid
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import orjson
except ImportError:  # optional speed-up; the standard library is enough
    orjson = None

SCHEMA = "pm-live-tape-v1"
FLUSH_SECONDS = 15.0


def dumps(value: Any) -> bytes:
    if orjson is not None:
        return orjson.dumps(value)
    return json.dumps(value, separators=(",", ":")).encode()


def loads(line: bytes) -> Any:
    return orjson.loads(line) if orjson is not None else json.loads(line)


class TapeWriter:
    """Append events to one gzip file per UTC hour. A crash loses at most the last flush."""

    def __init__(self, directory: Path, synthetic: bool = False) -> None:
        self.directory = Path(directory)
        self.synthetic = synthetic
        self.run = uuid.uuid4().hex[:8]
        self._hour: str | None = None
        self._handle: Any = None
        self._last_flush = 0.0
        self.events = 0

    def write(self, t_ns: int, kind: str, payload: Any) -> None:
        hour = datetime.fromtimestamp(t_ns / 1e9, UTC).strftime("%Y%m%dT%H")
        if hour != self._hour:
            self._open(hour, t_ns)
        self._handle.write(dumps({"t": t_ns, "k": kind, "m": payload}) + b"\n")
        self.events += 1
        now = time.monotonic()
        if now - self._last_flush > FLUSH_SECONDS:
            self._handle.flush()
            self._last_flush = now

    def _open(self, hour: str, t_ns: int) -> None:
        self.close()
        folder = self.directory / hour[:8]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"tape-{hour}-{self.run}.jsonl.gz"
        self._handle = gzip.open(path, "ab", compresslevel=6)  # noqa: SIM115 - stays open across writes
        self._hour = hour
        header = {"schema": SCHEMA, "synthetic": self.synthetic}
        self._handle.write(dumps({"t": t_ns, "k": "hdr", "m": header}) + b"\n")

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
            self._hour = None


def tape_files(path: Path) -> list[Path]:
    path = Path(path)
    if path.is_file():
        return [path]
    files = sorted(path.rglob("tape-*.jsonl.gz"), key=lambda item: item.name)
    if not files:
        raise FileNotFoundError(
            f"no tape files under {path}. Run `pm-live record` to collect data, or `pm-live demo`."
        )
    return files


def read_file(path: Path, problems: list[str] | None = None) -> Iterator[dict[str, Any]]:
    """Yield events from one file. A damaged tail is reported and the readable part is kept."""
    try:
        with gzip.open(path, "rb") as handle:
            for line in handle:
                try:
                    yield loads(line)
                except ValueError:
                    continue
    except (EOFError, OSError, zlib.error) as error:
        if problems is not None:
            problems.append(f"{Path(path).name}: {type(error).__name__}, kept the readable part")


def read_tape(path: Path, problems: list[str] | None = None) -> Iterator[dict[str, Any]]:
    """All events in time order. Files from separate runs in the same hour are merged."""
    groups: dict[str, list[Path]] = {}
    for file in tape_files(path):
        parts = file.name.split("-")
        groups.setdefault(parts[1] if len(parts) > 1 else file.name, []).append(file)
    for hour in sorted(groups):
        members = groups[hour]
        if len(members) == 1:
            yield from read_file(members[0], problems)
        else:
            merged = [event for file in members for event in read_file(file, problems)]
            merged.sort(key=lambda event: event["t"])
            yield from merged


def scan_metadata(path: Path) -> dict[str, Any]:
    """Quick first pass over results and market identities, skipping book messages unparsed."""
    winners: dict[str, str] = {}
    markets: dict[str, dict[str, Any]] = {}
    synthetic = False
    first = last = None
    problems: list[str] = []
    for file in tape_files(path):
        try:
            with gzip.open(file, "rb") as handle:
                for line in handle:
                    head = line[:60]
                    stamp = int(head[5:head.index(b",")]) if head.startswith(b'{"t":') else None
                    if stamp is not None:
                        first = stamp if first is None else min(first, stamp)
                        last = stamp if last is None else max(last, stamp)
                    if b'"k":"pm"' in head or b'"k":"ref"' in head:
                        continue
                    try:
                        event = loads(line)
                    except ValueError:
                        continue
                    kind = event["k"]
                    if kind == "result":
                        winners[event["m"]["slug"]] = str(event["m"]["winner"]).casefold()
                    elif kind == "market":
                        markets[event["m"]["slug"]] = event["m"]
                    elif kind == "hdr":
                        synthetic = synthetic or bool(event["m"].get("synthetic"))
        except (EOFError, OSError, zlib.error) as error:
            problems.append(f"{file.name}: {type(error).__name__}, kept the readable part")
    return {
        "winners": winners,
        "markets": markets,
        "synthetic": synthetic,
        "first_ns": first,
        "last_ns": last,
        "problems": problems,
    }
