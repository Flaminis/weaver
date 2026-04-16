"""
Event impact inference — loads the trained model + builds feature vectors
via FeatureVectorBuilder (shared with training).

Runtime callers supply:
  - team state before/after the event (LLF scoreboard dicts)
  - game_minute
  - state_history: trailing window of scoreboard snapshots (for momentum)
  - champion picks per team (for comp_diff)
  - champion_scores: ChampionScoreTable loaded once at trader boot

If any of those are missing, FeatureVectorBuilder falls back gracefully
(lag → zero-state, champions → global mean) so inference never crashes
on partial context. A single model load serves the whole process.

CLI is preserved for quick manual predictions.
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import joblib
import numpy as np

try:
    from feature_vector import (
        FEATURES_V2,
        ChampionScoreTable,
        FeatureVectorBuilder,
        find_lag_state,
        peak_signed_kill_diff,
    )
except ImportError:
    from training.feature_vector import (
        FEATURES_V2,
        ChampionScoreTable,
        FeatureVectorBuilder,
        find_lag_state,
        peak_signed_kill_diff,
    )

warnings.filterwarnings("ignore", message="X does not have valid feature names")

DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[2] / "data" / "models" / "winprob_lgbm_v2.joblib"
FALLBACK_MODEL_PATH = Path(__file__).resolve().parents[2] / "data" / "models" / "winprob_lgbm.joblib"
CHAMP_SCORES_PATH = Path(__file__).resolve().parents[2] / "data" / "processed" / "champion_scores.parquet"


class EventImpactModel:
    """Wraps a trained LightGBM model and routes predictions through
    FeatureVectorBuilder. Backward-compatible API: if state_history and
    champion picks aren't supplied, falls back to zero-history + neutral
    composition, which is exactly how the baseline model was trained."""

    def __init__(self, model_path: str | Path | None = None,
                 champ_scores_path: str | Path | None = None):
        path = Path(model_path) if model_path else DEFAULT_MODEL_PATH
        if not path.exists():
            # Graceful fallback to v1 if v2 wasn't shipped for some reason.
            if FALLBACK_MODEL_PATH.exists():
                path = FALLBACK_MODEL_PATH
            else:
                raise FileNotFoundError(f"Model not found at {path}. Run train_model.py first.")
        blob = joblib.load(path)
        self.model = blob["model"]
        self.features: list[str] = blob["features"]
        self.model_path = path
        self.is_v2 = self.features == FEATURES_V2

        csp = Path(champ_scores_path) if champ_scores_path else CHAMP_SCORES_PATH
        if csp.exists():
            self.champ_scores = ChampionScoreTable.from_parquet(csp)
        else:
            # Works for v1 (no comp_diff) and for v2 with neutral composition.
            self.champ_scores = ChampionScoreTable.empty()

    # ── Internal feature builder, adapted to whichever model is loaded ──

    def _build_vec(
        self,
        game_minute: float,
        blue_now: dict, red_now: dict,
        blue_lag: dict | None, red_lag: dict | None,
        peak_signed: int,
        blue_champs: list[str] | None, red_champs: list[str] | None,
        pin_totals_from: tuple[dict, dict] | None = None,
    ) -> np.ndarray:
        """Build the feature row in BLUE-FRAME. Model returns P(blue wins).
        If the caller wants P(red wins), they flip the output — not the row."""
        if self.is_v2:
            return FeatureVectorBuilder.build(
                game_minute=game_minute,
                blue_now=blue_now, red_now=red_now,
                blue_3min_ago=blue_lag, red_3min_ago=red_lag,
                peak_kill_diff_signed=peak_signed,
                blue_champs=blue_champs, red_champs=red_champs,
                champ_scores=self.champ_scores,
                pin_totals_from=pin_totals_from,
            )
        return self._build_v1_vec(game_minute, blue_now, red_now)

    def _build_v1_vec(self, game_minute: float, blue_now: dict, red_now: dict) -> np.ndarray:
        b = _n(blue_now); r = _n(red_now)
        state = {
            "game_minute": float(game_minute),
            "kill_diff": b["kills"] - r["kills"],
            "tower_diff": b["towers"] - r["towers"],
            "drake_diff": b["drakes"] - r["drakes"],
            "baron_diff": b["barons"] - r["barons"],
            "inhib_diff": b["inhibs"] - r["inhibs"],
            "herald_diff": 0,
            "total_kills": b["kills"] + r["kills"],
            "total_objectives": (b["towers"]+r["towers"]) + (b["drakes"]+r["drakes"]) + (b["barons"]+r["barons"]),
        }
        return np.array([[state[f] for f in self.features]], dtype=float)

    # ── Public API: direct win-prob query ──

    def predict_win_prob(
        self,
        game_minute: float,
        team_stats: dict, opponent_stats: dict,
        is_blue: bool = True,
        state_history: list[tuple[float, dict]] | None = None,
        current_ts: float | None = None,
        team_champs: list[str] | None = None,
        opp_champs: list[str] | None = None,
        pin_totals_from: tuple[dict, dict] | None = None,
    ) -> float:
        """Predict P(acting team wins) in the acting team's frame.

        Implementation: features always built in blue-frame; outer code
        flips the output probability via `1 - p_blue` when the acting team
        is red. Avoids the double-flip bug from the old code.
        """
        # Canonicalize: blue_now/red_now are the ACTUAL blue and red teams.
        blue_now = team_stats if is_blue else opponent_stats
        red_now = opponent_stats if is_blue else team_stats
        blue_champs = team_champs if is_blue else opp_champs
        red_champs = opp_champs if is_blue else team_champs

        blue_lag, red_lag = None, None
        peak_signed = 0
        if self.is_v2:
            if state_history is not None and current_ts is not None:
                blue_lag, red_lag = find_lag_state(state_history, current_ts)
                peak_signed = peak_signed_kill_diff(
                    state_history,
                    include_state={"blue": blue_now, "red": red_now},
                )

        X = self._build_vec(
            game_minute=game_minute,
            blue_now=blue_now, red_now=red_now,
            blue_lag=blue_lag, red_lag=red_lag,
            peak_signed=peak_signed,
            blue_champs=blue_champs, red_champs=red_champs,
            pin_totals_from=pin_totals_from,
        )
        p_blue = float(self.model.predict_proba(X)[0, 1])
        return p_blue if is_blue else 1.0 - p_blue

    # ── Public API: impact delta with pinned totals (signal logic) ──

    def predict_impact_from_llf(
        self,
        game_minute: float,
        team_before: dict, team_after: dict,
        opp_before: dict, opp_after: dict,
        is_blue: bool = True,
        state_history: list[tuple[float, dict]] | None = None,
        current_ts: float | None = None,
        team_champs: list[str] | None = None,
        opp_champs: list[str] | None = None,
    ) -> tuple[float, float, float]:
        """Predict win-prob shift from (team_before → team_after).

        Pinning: total_kills / total_objectives are implicitly held constant
        because FeatureVectorBuilder computes them from blue/red stats. For
        impact comparisons we call with BEFORE state for p_before and AFTER
        state for p_after; the diff features (kill_diff, tower_diff, etc.)
        carry the monotone signal. Totals change minimally (+1 on the event
        type) and the delta remains monotone for favored-team events.

        Returns (impact, p_before, p_after) all in the acting team's frame.
        """
        p_before = self.predict_win_prob(
            game_minute, team_before, opp_before, is_blue=is_blue,
            state_history=state_history, current_ts=current_ts,
            team_champs=team_champs, opp_champs=opp_champs,
        )
        # Pin totals to the BEFORE state so the monotone diff features fully
        # determine the impact sign — without this, unconstrained total_kills
        # can flip the output in tiny, unintuitive ways.
        # Canonicalize acting/opp → blue/red for the pin reference.
        blue_before = team_before if is_blue else opp_before
        red_before = opp_before if is_blue else team_before
        p_after = self.predict_win_prob(
            game_minute, team_after, opp_after, is_blue=is_blue,
            state_history=state_history, current_ts=current_ts,
            team_champs=team_champs, opp_champs=opp_champs,
            pin_totals_from=(blue_before, red_before),
        )
        return p_after - p_before, p_before, p_after


def _n(d: dict | None) -> dict[str, int]:
    """Normalize LLF or training-shape stats dict → canonical keys."""
    if not d:
        return {k: 0 for k in ("kills", "towers", "drakes", "barons", "inhibs")}
    return {
        "kills": int(d.get("kills", 0) or 0),
        "towers": int(d.get("towers", 0) or 0),
        "drakes": int(d.get("drakes", 0) or 0),
        "barons": int(d.get("nashors", d.get("barons", 0)) or 0),
        "inhibs": int(d.get("inhibitors", d.get("inhibs", 0)) or 0),
    }


# ── CLI: quick manual predictions ───────────────────────────────────────

def _cli():
    parser = argparse.ArgumentParser(description="Test event impact predictions")
    parser.add_argument("--model", type=str, default=None, help="Path to model .joblib")
    parser.add_argument("--minute", type=float, required=True)
    parser.add_argument("--event", type=str,
                        choices=["kill", "tower", "drake", "baron", "inhib", "herald"], required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--kill-diff", type=int, default=0)
    parser.add_argument("--tower-diff", type=int, default=0)
    parser.add_argument("--drake-diff", type=int, default=0)
    parser.add_argument("--baron-diff", type=int, default=0)
    parser.add_argument("--total-kills", type=int, default=0)
    parser.add_argument("--blue-champs", type=str, default="",
                        help="Comma-separated champion slugs for blue team (5 picks)")
    parser.add_argument("--red-champs", type=str, default="",
                        help="Comma-separated champion slugs for red team (5 picks)")
    args = parser.parse_args()

    eim = EventImpactModel(args.model)
    event_map = {"kill": "kills", "tower": "towers", "drake": "drakes",
                 "baron": "barons", "inhib": "inhibs", "herald": "heralds"}
    half = args.total_kills // 2
    before_team = {"kills": half + max(0, args.kill_diff), "towers": max(0, args.tower_diff),
                   "drakes": max(0, args.drake_diff), "barons": max(0, args.baron_diff),
                   "inhibs": 0}
    before_opp = {"kills": half - min(0, args.kill_diff), "towers": abs(min(0, args.tower_diff)),
                  "drakes": abs(min(0, args.drake_diff)), "barons": abs(min(0, args.baron_diff)),
                  "inhibs": 0}
    after_team = {**before_team}
    after_team[event_map[args.event]] = after_team.get(event_map[args.event], 0) + args.count
    after_opp = {**before_opp}

    bc = [c.strip() for c in args.blue_champs.split(",") if c.strip()] if args.blue_champs else None
    rc = [c.strip() for c in args.red_champs.split(",") if c.strip()] if args.red_champs else None

    p_before = eim.predict_win_prob(args.minute, before_team, before_opp, is_blue=True,
                                     team_champs=bc, opp_champs=rc)
    p_after = eim.predict_win_prob(args.minute, after_team, after_opp, is_blue=True,
                                    team_champs=bc, opp_champs=rc)
    delta = p_after - p_before

    print(f"\n  Model: {eim.model_path.name} ({'v2' if eim.is_v2 else 'v1'}, {len(eim.features)} features)")
    print(f"  Event: +{args.count} {args.event} at minute {args.minute:.0f}")
    print(f"  State: kill_diff={args.kill_diff} tower_diff={args.tower_diff} drake_diff={args.drake_diff} baron_diff={args.baron_diff}")
    if bc: print(f"  Blue champs: {bc}")
    if rc: print(f"  Red champs:  {rc}")
    print(f"  P(win) before: {p_before:.4f}")
    print(f"  P(win) after:  {p_after:.4f}")
    print(f"  Impact:        {delta:+.4f} ({delta*100:+.2f}%)")


if __name__ == "__main__":
    _cli()
