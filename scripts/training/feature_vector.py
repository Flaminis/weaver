"""
FeatureVectorBuilder — single source of truth for the model's input row.

Used by BOTH training (build_dataset.py-style feature extraction) AND runtime
inference (event_impact.py). If these ever diverge the model silently gets
garbage at inference and loses us money. Anything that touches model input
goes through THIS file.

Feature set is the 13-dim v2 schema (FEATURES_LIVE in train_model.py).
Graceful fallbacks for missing runtime context (incomplete draft, short
history, rare champion) so a partial match never crashes inference.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

# Canonical feature order — MUST match FEATURES_LIVE in train_model.py.
# The trained model expects columns in this exact order. Any reorder breaks
# inference silently (no error, wrong predictions).
FEATURES_V2: list[str] = [
    "game_minute",
    "kill_diff",
    "tower_diff",
    "drake_diff",
    "baron_diff",
    "inhib_diff",
    "total_kills",
    "total_objectives",
    "kill_diff_delta_3m",
    "obj_diff_delta_3m",
    "peak_kill_diff",
    "lead_retraction",
    "comp_diff",
]

# Stat keys in the order used internally (before blue/red sign flip).
_STAT_KEYS = ("kills", "towers", "drakes", "barons", "inhibs")

# Momentum lag window matches training (build_dataset.LAG_3MIN_SEC).
LAG_3MIN_SEC = 180.0


@dataclass(frozen=True)
class ChampionScoreTable:
    """Lookup of champion → shrunken historical win rate. Loaded once at
    trader boot from champion_scores.parquet.
    """
    scores: dict[str, float]
    global_mean: float

    @classmethod
    def from_parquet(cls, path: str | Path) -> "ChampionScoreTable":
        df = pd.read_parquet(path)
        scores = dict(zip(df["champion"].astype(str), df["winrate_shrunk"].astype(float)))
        # Global mean: games-weighted average of raw rates.
        total_g = int(df["games"].sum())
        total_w = int(df["wins"].sum())
        global_mean = total_w / total_g if total_g else 0.5
        return cls(scores=scores, global_mean=float(global_mean))

    @classmethod
    def empty(cls) -> "ChampionScoreTable":
        """Fallback table when champion_scores.parquet is unavailable —
        everything resolves to 0.5 so comp_diff is always 0."""
        return cls(scores={}, global_mean=0.5)

    def score(self, champion: str | None) -> float:
        if not champion:
            return self.global_mean
        return self.scores.get(champion, self.global_mean)

    def comp_score(self, champions: list[str] | None) -> float:
        """Mean shrunken win rate across a team's picks. Empty/None → global mean."""
        if not champions:
            return self.global_mean
        valid = [c for c in champions if c]
        if not valid:
            return self.global_mean
        return sum(self.score(c) for c in valid) / len(valid)


def _normalize_team_stats(d: dict | None) -> dict[str, int]:
    """Accept LLF-shape ({kills, towers, drakes, nashors, inhibitors}) OR
    training-shape ({kills, towers, drakes, barons, inhibs}) and return a
    canonical dict. Missing fields → 0."""
    if not d:
        return {k: 0 for k in _STAT_KEYS}
    return {
        "kills":  int(d.get("kills",  0) or 0),
        "towers": int(d.get("towers", 0) or 0),
        "drakes": int(d.get("drakes", 0) or 0),
        "barons": int(d.get("nashors", d.get("barons", 0)) or 0),
        "inhibs": int(d.get("inhibitors", d.get("inhibs", 0)) or 0),
    }


def _obj_diff(blue: dict[str, int], red: dict[str, int]) -> int:
    """Sum of tower + drake + baron + inhib diffs, from blue's perspective."""
    return ((blue["towers"] - red["towers"])
            + (blue["drakes"] - red["drakes"])
            + (blue["barons"] - red["barons"])
            + (blue["inhibs"] - red["inhibs"]))


def _kill_diff(blue: dict[str, int], red: dict[str, int]) -> int:
    return blue["kills"] - red["kills"]


class FeatureVectorBuilder:
    """Builds the 13-dim feature vector for both training and inference.

    Training path (build_dataset.py): feed historical frames in order, use
    `build_for_frame` with the prior 3-min snapshot + running peak.

    Runtime path (event_impact.py): feed the current state + a short history
    buffer maintained per-match, plus champion picks.
    """

    FEATURES = FEATURES_V2

    @staticmethod
    def build(
        game_minute: float,
        blue_now: dict,
        red_now: dict,
        blue_3min_ago: dict | None,
        red_3min_ago: dict | None,
        peak_kill_diff_signed: int,
        blue_champs: list[str] | None,
        red_champs: list[str] | None,
        champ_scores: ChampionScoreTable,
        pin_totals_from: tuple[dict, dict] | None = None,
    ) -> np.ndarray:
        """Return a (1, 13) numpy array in canonical feature order.

        Output is ALWAYS in blue-frame (blue−red diffs, `P(blue wins)` target).
        Callers that want P(red wins) compute `1 - model.predict(...)`; they
        must NOT swap blue/red inputs to this function. The sign-flip trick
        in the old event_impact.py is gone because its combination with the
        outer `1 - p_blue` flip double-inverted the probability.

        Graceful fallbacks:
          - blue_3min_ago / red_3min_ago None → lag features = zeros (pre-game).
          - blue_champs / red_champs None/empty → each team's comp_score
            defaults to the global mean → comp_diff = 0.
          - peak_kill_diff_signed = 0 → no swing yet, lead_retraction = 0.
        """
        b_now = _normalize_team_stats(blue_now)
        r_now = _normalize_team_stats(red_now)
        # When history doesn't reach 3 min back (game just started), match
        # the training-time convention: "before the game, everything was 0".
        _zero_state = {k: 0 for k in _STAT_KEYS}
        b_lag = _normalize_team_stats(blue_3min_ago) if blue_3min_ago is not None else _zero_state
        r_lag = _normalize_team_stats(red_3min_ago) if red_3min_ago is not None else _zero_state

        kd_now = _kill_diff(b_now, r_now)
        kd_lag = _kill_diff(b_lag, r_lag)
        od_now = _obj_diff(b_now, r_now)
        od_lag = _obj_diff(b_lag, r_lag)

        peak = int(peak_kill_diff_signed)
        if abs(peak) < abs(kd_now):
            peak = kd_now  # self-correct if caller lagged

        blue_comp = champ_scores.comp_score(blue_champs)
        red_comp = champ_scores.comp_score(red_champs)

        # Pin totals when asked — used by predict_impact_from_llf so the
        # before/after delta is driven purely by monotone-constrained diff
        # features. Without this, unconstrained totals can flip the sign.
        if pin_totals_from is not None:
            pb, pr = pin_totals_from
            pb_n = _normalize_team_stats(pb)
            pr_n = _normalize_team_stats(pr)
            total_k = pb_n["kills"] + pr_n["kills"]
            total_o = ((pb_n["towers"] + pr_n["towers"])
                       + (pb_n["drakes"] + pr_n["drakes"])
                       + (pb_n["barons"] + pr_n["barons"]))
        else:
            total_k = b_now["kills"] + r_now["kills"]
            total_o = ((b_now["towers"] + r_now["towers"])
                       + (b_now["drakes"] + r_now["drakes"])
                       + (b_now["barons"] + r_now["barons"]))

        row = {
            "game_minute": float(game_minute),
            "kill_diff": kd_now,
            "tower_diff": b_now["towers"] - r_now["towers"],
            "drake_diff": b_now["drakes"] - r_now["drakes"],
            "baron_diff": b_now["barons"] - r_now["barons"],
            "inhib_diff": b_now["inhibs"] - r_now["inhibs"],
            "total_kills": total_k,
            "total_objectives": total_o,
            "kill_diff_delta_3m": kd_now - kd_lag,
            "obj_diff_delta_3m": od_now - od_lag,
            "peak_kill_diff": peak,
            "lead_retraction": peak - kd_now,
            "comp_diff": blue_comp - red_comp,
        }
        return np.array([[row[f] for f in FEATURES_V2]], dtype=float)


def find_lag_state(
    history: list[tuple[float, dict]] | None,
    current_ts: float,
    lag_sec: float = LAG_3MIN_SEC,
) -> tuple[dict | None, dict | None]:
    """Look up the most recent state snapshot at least `lag_sec` seconds
    before `current_ts`. Returns (blue_state, red_state) or (None, None).

    history: list of (ts_seconds, {"blue": {...}, "red": {...}}) ordered by ts.
    Trader maintains this as a ring buffer (~15 min retention).
    """
    if not history:
        return None, None
    target = current_ts - lag_sec
    # Walk backwards to find the latest entry ≤ target.
    found = None
    for ts, state in reversed(history):
        if ts <= target:
            found = state
            break
    if found is None:
        return None, None
    return found.get("blue"), found.get("red")


def peak_signed_kill_diff(history: list[tuple[float, dict]] | None,
                           include_state: dict | None = None) -> int:
    """Running signed extremum of kill_diff across the history plus the
    optional current snapshot. include_state lets callers make sure the
    current frame is covered even if they haven't pushed it to history yet.
    """
    peak = 0
    snapshots = list(history or [])
    if include_state is not None:
        snapshots = snapshots + [(0.0, include_state)]
    for _, st in snapshots:
        b = _normalize_team_stats(st.get("blue"))
        r = _normalize_team_stats(st.get("red"))
        kd = _kill_diff(b, r)
        if abs(kd) > abs(peak):
            peak = kd
    return int(peak)
