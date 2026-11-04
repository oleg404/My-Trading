"""Sample many independent Kronos futures and keep every one.

KronosPredictor.predict averages its samples into a single line. Here the
normalized history is repeated along the batch axis and each row is decoded
on its own, so N rows come back as N separate paths. Preprocessing matches
KronosPredictor.predict exactly: per-series z-score, clip at 5, then undo it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

KRONOS_DIR = Path(__file__).resolve().parent.parent / "kronos"
COLS = ["open", "high", "low", "close", "volume", "amount"]
MODELS = {
    "mini": ("NeoQuasar/Kronos-mini", "NeoQuasar/Kronos-Tokenizer-2k", 2048),
    "small": ("NeoQuasar/Kronos-small", "NeoQuasar/Kronos-Tokenizer-base", 512),
    "base": ("NeoQuasar/Kronos-base", "NeoQuasar/Kronos-Tokenizer-base", 512),
}


def _kronos():
    if not (KRONOS_DIR / "model").is_dir():
        raise RuntimeError(f"Kronos source not found at {KRONOS_DIR}; see research/README setup steps")
    if str(KRONOS_DIR) not in sys.path:
        sys.path.insert(0, str(KRONOS_DIR))
    import model as kronos_model  # the upstream package is literally named `model`
    return kronos_model


def load(name: str = "small", device: str | None = None):
    """A KronosPredictor in eval mode. Upstream never calls eval(), which leaves dropout on."""
    if name not in MODELS:
        raise ValueError(f"model must be one of {', '.join(MODELS)}")
    km = _kronos()
    model_id, tok_id, max_ctx = MODELS[name]
    tokenizer = km.KronosTokenizer.from_pretrained(tok_id).eval()
    model = km.Kronos.from_pretrained(model_id).eval()
    return km.KronosPredictor(model, tokenizer, device=device, max_context=max_ctx)


def time_features(ts: pd.Series) -> np.ndarray:
    ts = pd.Series(pd.to_datetime(ts))
    return np.stack([ts.dt.minute, ts.dt.hour, ts.dt.weekday, ts.dt.day, ts.dt.month], axis=1).astype(np.float32)


def sample_paths(predictor, history: pd.DataFrame, future_ts: pd.Series, n_paths: int = 1000,
                 chunk: int = 250, temperature: float = 1.0, top_p: float = 0.9, seed: int | None = 7,
                 progress: bool = True) -> np.ndarray:
    """history: candles with `timestamps` and COLS. Returns an array (n_paths, len(future_ts), 6)
    in price units, columns in COLS order."""
    import torch

    if n_paths < 1 or chunk < 1:
        raise ValueError("n_paths and chunk must be positive")
    x = history[COLS].to_numpy(dtype=np.float32)
    if np.isnan(x).any():
        raise ValueError("history has NaN prices or volume")
    mean, std = x.mean(axis=0), x.std(axis=0)
    xn = np.clip((x - mean) / (std + 1e-5), -predictor.clip, predictor.clip)
    xs, ys = time_features(history["timestamps"]), time_features(future_ts)
    horizon = len(future_ts)

    if seed is not None:
        torch.manual_seed(seed)
        np.random.seed(seed)
    out, done, t0 = [], 0, time.time()
    while done < n_paths:
        b = min(chunk, n_paths - done)
        preds = predictor.generate(np.repeat(xn[None], b, axis=0), np.repeat(xs[None], b, axis=0),
                                   np.repeat(ys[None], b, axis=0), horizon, temperature, 0, top_p, 1, False)
        out.append(preds * (std + 1e-5) + mean)
        done += b
        if progress:
            print(f"  sampled {done}/{n_paths} paths ({time.time() - t0:.0f}s)", file=sys.stderr, flush=True)
    return np.concatenate(out, axis=0).astype(np.float64)
