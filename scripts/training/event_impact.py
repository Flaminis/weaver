"""
Phase 4 — Event impact inference module.

Given game state before and after an event, predicts the shift in win probability.
Designed to replace the hardcoded impact priors in lol_signal.py.

Usage as library:
    from scripts.training.event_impact import EventImpactModel
    eim = EventImpactModel("data/models/winprob_lgbm.joblib")
    delta = eim.predict_impact(
        game_minute=35.0,
        state_before={"kills": 10, "towers": 3, "drakes": 2, "barons": 0, "inhibs": 0},
        state_after={"kills": 10, "towers": 3, "drakes": 2, "barons": 1, "inhibs": 0},
        opponent_before={"kills": 8, "towers": 2, "drakes": 1, "barons": 0, "inhibs": 0},
        opponent_after={"kills": 8, "towers": 2, "drakes": 1, "barons": 0, "inhibs": 0},
    )
    # delta > 0 means event favors the acting team

CLI usage:
    python event_impact.py --minute 35 --event baron --kill-diff 2 --tower-diff -1
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import joblib
import numpy as np

warnings.filterwarnings("ignore", message="X does not have valid feature names")

DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[2] / "data" / "models" / "winprob_lgbm.joblib"

FEATURES = [
    "game_minute",
    "kill_diff",
    "tower_diff",
    "drake_diff",
    "baron_diff",
    "inhib_diff",
    "herald_diff",
    "total_kills",
    "total_objectives",
]


class EventImpactModel:
    """Wraps the trained LightGBM win-probability model for event impact scoring."""

    def __init__(self, model_path: str | Path | None = None):
        path = Path(model_path) if model_path else DEFAULT_MODEL_PATH
        if not path.exists():
            raise FileNotFoundError(f"Model not found at {path}. Run train_model.py first.")
        blob = joblib.load(path)
        self.model = blob["model"]
        self.features = blob["features"]

    def _build_feature_vec(
        self,
        game_minute: float,
        team_stats: dict,
        opponent_stats: dict,
        is_blue: bool = True,
        pin_totals_from: tuple[dict, dict] | None = None,
    ) -> np.ndarray:
        """Build the feature vector from team perspective.

        The model is trained with blue-side as reference. If the acting team
        is red, we flip the sign of all diff features so the model sees it
        from blue's perspective, then we flip the output probability.

        pin_totals_from: if set, total_kills and total_objectives are
        sourced from (pin_team, pin_opp) instead of the current stats.
        """
        tk = team_stats.get("kills", 0)
        ok = opponent_stats.get("kills", 0)
        tt = team_stats.get("towers", 0)
        ot = opponent_stats.get("towers", 0)
        td = team_stats.get("drakes", 0)
        od = opponent_stats.get("drakes", 0)
        tb = team_stats.get("barons", 0)
        ob = opponent_stats.get("barons", 0)
        ti = team_stats.get("inhibs", 0)
        oi = opponent_stats.get("inhibs", 0)
        th = team_stats.get("heralds", 0)
        oh = opponent_stats.get("heralds", 0)
        sign = 1 if is_blue else -1

        if pin_totals_from:
            pt, po = pin_totals_from
            total_k = pt.get("kills", 0) + po.get("kills", 0)
            total_o = ((pt.get("towers", 0) + po.get("towers", 0))
                       + (pt.get("drakes", 0) + po.get("drakes", 0))
                       + (pt.get("barons", 0) + po.get("barons", 0)))
        else:
            total_k = tk + ok
            total_o = (tt + ot) + (td + od) + (tb + ob)

        state = {
            "game_minute": game_minute,
            "kill_diff": sign * (tk - ok),
            "tower_diff": sign * (tt - ot),
            "drake_diff": sign * (td - od),
            "baron_diff": sign * (tb - ob),
            "inhib_diff": sign * (ti - oi),
            "herald_diff": sign * (th - oh),
            "total_kills": total_k,
            "total_objectives": total_o,
        }
        return np.array([[state[f] for f in self.features]])

    def predict_win_prob(
        self,
        game_minute: float,
        team_stats: dict,
        opponent_stats: dict,
        is_blue: bool = True,
        pin_totals_from: tuple[dict, dict] | None = None,
    ) -> float:
        """Predict P(team wins) given current state.

        If pin_totals_from is provided, total_kills and total_objectives
        are computed from that (team, opp) pair instead, keeping them
        constant across before/after comparisons so that monotone
        constraints on diff features are respected.
        """
        X = self._build_feature_vec(
            game_minute, team_stats, opponent_stats, is_blue,
            pin_totals_from=pin_totals_from,
        )
        p_blue = self.model.predict_proba(X)[0, 1]
        return p_blue if is_blue else 1 - p_blue

    def predict_impact(
        self,
        game_minute: float,
        state_before: dict,
        state_after: dict,
        opponent_before: dict,
        opponent_after: dict,
        is_blue: bool = True,
    ) -> float:
        """Predict the win-probability delta from a state change.

        Returns positive value if the event favors the acting team.

        Totals (total_kills, total_objectives) are held constant between
        before/after so the monotone-constrained diff features fully
        determine the sign of the impact.
        """
        p_before = self.predict_win_prob(game_minute, state_before, opponent_before, is_blue)
        p_after = self.predict_win_prob(
            game_minute, state_after, opponent_after, is_blue,
            pin_totals_from=(state_before, opponent_before),
        )
        return p_after - p_before

    def predict_impact_from_llf(
        self,
        game_minute: float,
        team_before: dict,
        team_after: dict,
        opp_before: dict,
        opp_after: dict,
        is_blue: bool = True,
    ) -> tuple[float, float, float]:
        """Convenience method matching LLF scoreboard format.

        team_before/after: {"kills": N, "towers": N, "drakes": N, "nashors": N, "inhibitors": N}
        (field names match LLF scoreboard keys)

        Returns (delta, p_before, p_after).
        """
        def _normalize(d: dict) -> dict:
            return {
                "kills": d.get("kills", 0),
                "towers": d.get("towers", 0),
                "drakes": d.get("drakes", 0),
                "barons": d.get("nashors", d.get("barons", 0)),
                "inhibs": d.get("inhibitors", d.get("inhibs", 0)),
                "heralds": d.get("heralds", 0),
            }

        tb = _normalize(team_before)
        ta = _normalize(team_after)
        ob = _normalize(opp_before)
        oa = _normalize(opp_after)

        p_before = self.predict_win_prob(game_minute, tb, ob, is_blue=is_blue)
        p_after = self.predict_win_prob(
            game_minute, ta, oa, is_blue=is_blue,
            pin_totals_from=(tb, ob),
        )
        return p_after - p_before, p_before, p_after


def _cli():
    parser = argparse.ArgumentParser(description="Test event impact predictions")
    parser.add_argument("--model", type=str, default=None, help="Path to model .joblib")
    parser.add_argument("--minute", type=float, required=True, help="Game minute")
    parser.add_argument("--event", type=str, choices=["kill", "tower", "drake", "baron", "inhib", "herald"],
                        required=True, help="Event type")
    parser.add_argument("--count", type=int, default=1, help="Number of events (e.g., 3 for triple kill)")
    parser.add_argument("--kill-diff", type=int, default=0, help="Current kill differential (positive = acting team ahead)")
    parser.add_argument("--tower-diff", type=int, default=0, help="Current tower differential")
    parser.add_argument("--drake-diff", type=int, default=0, help="Current drake differential")
    parser.add_argument("--baron-diff", type=int, default=0, help="Current baron differential")
    parser.add_argument("--total-kills", type=int, default=0, help="Total kills in game so far")
    args = parser.parse_args()

    eim = EventImpactModel(args.model)

    event_map = {
        "kill": "kills", "tower": "towers", "drake": "drakes",
        "baron": "barons", "inhib": "inhibs", "herald": "heralds",
    }

    # Build before/after from perspective of acting team (blue)
    # Diff values are from blue's perspective
    half_kills = args.total_kills // 2
    before_team = {
        "kills": half_kills + max(0, args.kill_diff),
        "towers": max(0, args.tower_diff),
        "drakes": max(0, args.drake_diff),
        "barons": max(0, args.baron_diff),
        "inhibs": 0,
        "heralds": 0,
    }
    before_opp = {
        "kills": half_kills - min(0, args.kill_diff),
        "towers": abs(min(0, args.tower_diff)),
        "drakes": abs(min(0, args.drake_diff)),
        "barons": abs(min(0, args.baron_diff)),
        "inhibs": 0,
        "heralds": 0,
    }

    after_team = {**before_team}
    stat_key = event_map[args.event]
    after_team[stat_key] = after_team.get(stat_key, 0) + args.count

    after_opp = {**before_opp}

    p_before = eim.predict_win_prob(args.minute, before_team, before_opp, is_blue=True)
    p_after = eim.predict_win_prob(args.minute, after_team, after_opp, is_blue=True)
    delta = p_after - p_before

    print(f"\n  Event: +{args.count} {args.event} at minute {args.minute:.0f}")
    print(f"  State: kill_diff={args.kill_diff} tower_diff={args.tower_diff} "
          f"drake_diff={args.drake_diff} baron_diff={args.baron_diff}")
    print(f"  P(win) before: {p_before:.4f}")
    print(f"  P(win) after:  {p_after:.4f}")
    print(f"  Impact (delta): {delta:+.4f} ({delta*100:+.2f}%)")
    print(f"  Verdict: {'SIGNAL' if abs(delta) > 0.02 else 'noise'} "
          f"({'strong' if abs(delta) > 0.05 else 'weak' if abs(delta) > 0.02 else 'negligible'})")


if __name__ == "__main__":
    _cli()
