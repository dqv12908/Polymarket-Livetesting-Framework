"""Example: train a model on recorded data, then test it like any other strategy.

    pip install lightgbm numpy pandas
    pm-live dataset --data data --out dataset.csv.gz
    python examples/train_lightgbm.py dataset.csv.gz model.txt
    pm-live backtest examples/lightgbm_strategy.py --settings examples/lightgbm_settings.json --data new_data

The model learns what the market price does not already say, because the price is its starting
point. The last part of the data is held back: this script never trains on it. Backtest on data
recorded after the training period, otherwise the result means nothing.
"""

from __future__ import annotations

import sys

import lightgbm as lgb
import numpy as np
import pandas as pd

NOT_INPUTS = ("slug", "series", "t", "outcome_a", "bid", "ask")


def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 0.005, 0.995)
    return np.log(p / (1 - p))


def loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def main(path: str, out: str) -> None:
    data = pd.read_csv(path).sort_values("t")
    data = data[data.mid.between(0.03, 0.97)]
    inputs = [c for c in data.columns if c not in NOT_INPUTS and not c.startswith("label")]
    cut_valid, cut_test = data.t.quantile(0.6), data.t.quantile(0.8)
    train, valid, test = data[data.t < cut_valid], data[data.t.between(cut_valid, cut_test)], data[data.t > cut_test]
    train_set = lgb.Dataset(train[inputs], train.label_a_won, init_score=logit(train.mid.values))
    valid_set = lgb.Dataset(valid[inputs], valid.label_a_won, init_score=logit(valid.mid.values), reference=train_set)
    model = lgb.train(
        {"objective": "binary", "learning_rate": 0.03, "num_leaves": 15, "min_data_in_leaf": 200,
         "feature_fraction": 0.7, "lambda_l2": 20, "verbose": -1},
        train_set,
        2000,
        valid_sets=[valid_set],
        callbacks=[lgb.early_stopping(100, verbose=False)],
    )
    raw = model.predict(test[inputs], raw_score=True) + logit(test.mid.values)
    predicted = 1 / (1 + np.exp(-raw))
    print(f"held-back rows: {len(test)}, trees: {model.best_iteration}")
    print(f"log loss, market price alone: {loss(test.label_a_won.values, test.mid.clip(0.005, 0.995).values):.5f}")
    print(f"log loss, model:              {loss(test.label_a_won.values, predicted):.5f}  (lower is better)")
    print("If the model does not beat the market price here, it has no edge to trade.")
    model.save_model(out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
