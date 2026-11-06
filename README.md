# Polymarket Live-Testing Framework

Test a Polymarket trading idea on your own machine before risking anything: **record real market data, backtest on it, then run the same strategy live with simulated fills.**

- **One strategy file, two modes.** Backtests and live paper tests run the same engine, so results are comparable.
- **Realistic fills.** Orders fill at the real ask after a delay you set, with fees and displayed depth.
- **Honest reports.** You get a confidence range, a delay comparison and plain warnings. "Not distinguishable from zero" is a normal answer.
- **No real orders.** There is no wallet, no signing and no order code in this project. It only reads public data.

It runs on Windows, macOS and Linux with Python 3.11 or newer. No account or API key is needed.

## Try it in one minute

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install "git+https://github.com/dqv12908/Polymarket-Livetesting-Framework"
pm-live demo
```

`pm-live demo` makes synthetic market data, backtests a baseline strategy and writes a report. It needs no internet connection for the data or the test. The result says nothing about real markets, and the report says so.

## The workflow

```bash
pm-live doctor                   # 1. check that this machine can reach everything
pm-live init mybot && cd mybot   # 2. create a strategy to edit
pm-live demo --strategy strategy.py

# 3. record real data, and leave it running for as long as you can
pm-live record --series btc-updown-5m --out data

# 4. backtest on what you recorded
pm-live backtest strategy.py --data data

# 5. run it live with simulated fills, then open http://127.0.0.1:8765/
pm-live paper strategy.py
pm-live report                   # build the same report from the paper session
```

Polymarket does not publish historical order books, so **a useful backtest starts with your own recording**. A day of data is a start; weeks are better.

A market's result appears some minutes after it ends. If you stop recording before then, run `pm-live results --data data` later to fetch the missing ones.

`record` uses about 1 GB per coin per day. Busy markets use more: BTC 5-minute markets can use several GB a day.

## Write a strategy

```python
from polymarket_livetesting import MarketView, Order, Strategy


class MyStrategy(Strategy):
    name = "my_strategy"
    series = ("btc-updown-5m", "eth-updown-5m")   # markets to follow
    tick_seconds = 1.0                            # on_tick runs this often per market

    def on_tick(self, market: MarketView) -> Order | None:
        if market.seconds_left > 60:
            return None
        ask = market.best_ask("up")
        move = market.reference_return_bp(30)     # outside price change over 30 s
        if ask is not None and ask < 0.80 and move is not None and move > 2:
            return Order("up", probability=ask + 0.03)
        return None
```

You get at most one position per market, held until it settles. `probability` is logged for your own analysis and never decides the fill.

What `market` gives you, all of it limited to data received before that moment:

| Use | Gives you |
|---|---|
| `slug`, `series`, `asset`, `outcomes` | Which market this is |
| `seconds_in`, `seconds_left`, `start`, `end` | Where you are in its time window |
| `best_bid(o)`, `best_ask(o)`, `mid(o)`, `spread(o)`, `favourite()` | Current prices |
| `depth(o, side="ask", within=0.02)` | Shares displayed near the best price |
| `mid_ago(o, seconds)`, `mid_change(o, seconds)` | Recent price moves |
| `trades(o, seconds)`, `flow(o, seconds)` | Recent trades, and takers' net buying |
| `reference()`, `reference_return_bp(seconds)` | Outside price, from Binance by default |

`Strategy` also has `settings` (from `--settings file.json`), `pre_open_seconds` to start before a window opens, and `on_settle(record)`.

Load a strategy as `file.py`, `file.py:ClassName`, an importable `module:ClassName`, or a baseline: `builtin:late_favorite` and `builtin:random_baseline`. **Run the baselines first.** If your idea can't beat them, it isn't adding anything.

## Choosing markets

- `--series btc-updown-5m,eth-updown-15m` follows rolling markets, whose slugs end in a start time. The name must end in a length such as `5m` or `1h`.
- `--slug exact-market-slug` follows any single two-outcome market.

## Reading a report

```text
VERDICT    NOT DISTINGUISHABLE FROM ZERO

Net per share        +0.85c   95% range -6.50c to +8.20c
  skill vs price     +3.00c (how often you beat the market's own odds)
  cost to trade      -2.15c (fee, spread, and movement during the delay)
```

- **Net per share** is the result per $1 payout share, after fees. The range is what matters: if it includes zero, you have not shown an edge.
- **Skill vs cost** splits the result. Skill above zero with a loss overall means trading costs are eating a real signal.
- **The delay table** shows the same strategy at faster and slower speeds. A result that only works at 100 ms needs a fast connection to be real.
- **Independent time windows** is the honest sample size, because coins that settle together tend to win or lose together.
- **Trial count** is how many versions you've tried on this data. Try enough of them and one will look good by luck.

Each run also writes `report.html`, `summary.json` and `trades.csv`.

## Mistakes this tool is built to stop

These are easy ways to fool yourself in short-dated prediction markets:

1. **Backtesting on trade prints.** A printed price exists only because someone chose to trade there, often faster than you. Results at prints can be strongly positive and vanish at the real ask. This tool fills at the ask only.
2. **Forgetting the delay.** The book moves while your order travels. Fills use the book as it stood after your delay, never the one you decided on.
3. **Slow recording.** If your recorder falls behind, old prices look tradable and fake a profit. Fills are refused while the feed is too far behind (`--max-feed-lag-ms`), and reports warn about it.
4. **Ignoring fees.** Takers pay about `0.07 x price x (1 - price)` per share on many crypto markets. Prices can be fair and takers can still lose steadily. The rate is read from each market.
5. **Dropping unresolved markets.** A position without a result is left out of the numbers, and the report tells you how many. Fetch them with `pm-live results`.
6. **Trying many versions on one data set.** Test your final version on data recorded after you stopped changing it.

## Train your own models

```bash
pm-live dataset --data data --out dataset.csv.gz
pip install lightgbm numpy pandas
python examples/train_lightgbm.py dataset.csv.gz model.txt
pm-live backtest examples/lightgbm_strategy.py --settings examples/lightgbm_settings.json --data newer_data
```

Each row is one market at one moment, built only from data received by then. Columns starting with `label` are the answers, so never use them as inputs. The `examples/` folder is in this repository, so clone it to use these files. Backtest a model only on data recorded after its training period.

## Settings

`--latency-ms` sets the delay from decision to fill. The default of 450 ms is deliberately cautious, so measure your own. Use `--config file.json` to change limits; unknown fields are rejected:

```json
{
  "execution": {
    "starting_capital": 200.0,
    "max_capital_fraction": 0.1,
    "max_trade_usd": 20.0,
    "max_price_slippage": 0.02,
    "depth_haircut": 0.9
  }
}
```

A paper session keeps its state in `--state-dir`: `state.json`, an append-only `events.jsonl` and the recorded `tape/`. You can stop and restart it; open positions keep settling. Use a separate folder for each strategy. You can also backtest that `tape/` folder to compare a replay with what happened live. Expect the same trades. Sizes can differ a little, because a replay gets each result right after the market closes, which changes the cash available for later trades.

The dashboard listens on your own machine only (`127.0.0.1`) by default. Don't expose it to the internet.

## What is not simulated

- **Maker orders.** Only taker orders are modelled, because queue position can't be known from public data.
- **Exits before settlement.** Every position is held to the end.
- **Your order's effect on the market.** Your simulated fill doesn't remove liquidity from the recorded book, and other traders don't react to it. A haircut on displayed size partly allows for this.
- **Rejections and outages.** Real exchanges can refuse orders or go down. A paper fill is not a promise of a real one.

## Using this responsibly

This is a research tool and not financial advice. A good backtest or paper result is not evidence of future profit. You are responsible for following Polymarket's terms and the laws where you live, including any restrictions on access. This project doesn't provide or support ways around those restrictions. It is not affiliated with Polymarket.

## Connect a model from another program

If your model runs outside Python or in a separate process, the original signal server is still here: `pm-live serve` accepts authenticated decisions over HTTP, and `pm-live submit` and `pm-live run-plugin` feed it. Signals use exact market slugs and are checked against market identity at settlement.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))" > signal.token
pm-live serve --state-dir signals --config examples/config.example.json --token-file signal.token
pm-live submit examples/signal.example.json --token-file signal.token
```

See `examples/signal.example.json` for the format, and `examples/simple_strategy.py` for the plug-in form.

## Development

```bash
pip install -e ".[dev,fast]"
pytest
ruff check .
```

`fast` adds `orjson`, which speeds up replays. Report security problems as described in [SECURITY.md](SECURITY.md).

Licensed under [MIT](LICENSE).
