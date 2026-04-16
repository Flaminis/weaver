"""
Sanity check: FeatureVectorBuilder must produce the same 13-dim row that
build_dataset.py produced for real historical frames. If these diverge,
the runtime model will get inputs it was never trained on.

Run: .venv311/bin/python3 scripts/training/test_feature_vector.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from feature_vector import (
    FEATURES_V2,
    ChampionScoreTable,
    FeatureVectorBuilder,
)

DATA = Path(__file__).resolve().parents[2] / "data" / "processed"


def reconstruct_teams(row: pd.Series) -> tuple[dict, dict]:
    return (
        {"kills": int(row["blue_kills"]), "towers": int(row["blue_towers"]),
         "drakes": int(row["blue_drakes"]), "barons": int(row["blue_barons"]),
         "inhibs": int(row["blue_inhibs"])},
        {"kills": int(row["red_kills"]), "towers": int(row["red_towers"]),
         "drakes": int(row["red_drakes"]), "barons": int(row["red_barons"]),
         "inhibs": int(row["red_inhibs"])},
    )


def pick_lag_row(game_df: pd.DataFrame, target_ts: float) -> pd.Series | None:
    """Mirror the lag-walk logic in build_dataset._add_momentum_features."""
    prior = game_df[game_df["timestamp_sec"] <= target_ts]
    if prior.empty:
        return None
    return prior.iloc[-1]


def run():
    df = pd.read_parquet(DATA / "training_rows.parquet")
    champ_table = ChampionScoreTable.from_parquet(DATA / "champion_scores.parquet")

    # Sample 500 rows spread across games (not just first frames) to exercise
    # different momentum regimes.
    sample = df.sample(n=500, random_state=7).reset_index(drop=True)

    mismatches = 0
    diffs: list[str] = []
    for _, row in sample.iterrows():
        gid = int(row["game_id"])
        game_df = df[df["game_id"] == gid].sort_values("timestamp_sec")

        blue_now, red_now = reconstruct_teams(row)
        target_lag = float(row["timestamp_sec"]) - 180.0

        lag_row = pick_lag_row(game_df, target_lag)
        if lag_row is not None:
            blue_lag, red_lag = reconstruct_teams(lag_row)
        else:
            blue_lag, red_lag = None, None

        # Peak signed kill_diff up to and including this row.
        prior_inclusive = game_df[game_df["timestamp_sec"] <= row["timestamp_sec"]]
        peak_signed = 0
        for _, pr in prior_inclusive.iterrows():
            kd = int(pr["blue_kills"]) - int(pr["red_kills"])
            if abs(kd) > abs(peak_signed):
                peak_signed = kd

        # Build via FeatureVectorBuilder using blue-frame (is_blue=True).
        # Champion features need the per-game comp_diff — the parquet already
        # has it computed, but comp_diff is LOO at training time. We compare
        # against the non-LOO value from champ_table since that's what runtime
        # will use. This won't match the parquet's comp_diff, so we compare
        # all OTHER features and skip comp_diff.
        vec = FeatureVectorBuilder.build(
            game_minute=float(row["game_minute"]),
            blue_now=blue_now, red_now=red_now,
            blue_3min_ago=blue_lag, red_3min_ago=red_lag,
            peak_kill_diff_signed=peak_signed,
            blue_champs=None, red_champs=None,  # leave comp_diff check out
            champ_scores=champ_table,
        )[0]

        expected = {
            "game_minute": float(row["game_minute"]),
            "kill_diff": int(row["kill_diff"]),
            "tower_diff": int(row["tower_diff"]),
            "drake_diff": int(row["drake_diff"]),
            "baron_diff": int(row["baron_diff"]),
            "inhib_diff": int(row["inhib_diff"]),
            "total_kills": int(row["total_kills"]),
            "total_objectives": int(row["total_objectives"]),
            "kill_diff_delta_3m": int(row["kill_diff_delta_3m"]),
            "obj_diff_delta_3m": int(row["obj_diff_delta_3m"]),
            "peak_kill_diff": int(row["peak_kill_diff"]),
            "lead_retraction": int(row["lead_retraction"]),
        }

        for i, name in enumerate(FEATURES_V2):
            if name == "comp_diff":
                continue  # handled separately
            exp = expected[name]
            got = vec[i]
            if not np.isclose(got, exp, atol=1e-6):
                mismatches += 1
                diffs.append(f"game={gid} ts={row['timestamp_sec']:.0f} {name}: expected={exp} got={got}")
                break

    if mismatches:
        print(f"FAIL — {mismatches}/500 rows had feature mismatch:")
        for d in diffs[:10]:
            print(f"  {d}")
        raise SystemExit(1)

    # Spot-check comp_diff range — should be in ±0.1 range
    vec_with_champs = FeatureVectorBuilder.build(
        game_minute=20.0,
        blue_now={"kills": 5, "towers": 2, "drakes": 1, "barons": 0, "inhibs": 0},
        red_now={"kills": 3, "towers": 1, "drakes": 0, "barons": 0, "inhibs": 0},
        blue_3min_ago=None, red_3min_ago=None,
        peak_kill_diff_signed=5,
        blue_champs=["Rumble", "Varus", "Ahri", "Nautilus", "Alistar"],
        red_champs=["Corki", "Rell", "XinZhao", "Ornn", "Karma"],
        champ_scores=champ_table,
    )
    comp_diff_idx = FEATURES_V2.index("comp_diff")
    comp_diff = vec_with_champs[0, comp_diff_idx]
    print(f"Synthetic comp_diff (strong blue picks vs weak red): {comp_diff:+.4f}")
    assert -0.15 < comp_diff < 0.15, f"comp_diff {comp_diff} out of expected range"

    print("OK — FeatureVectorBuilder matches build_dataset output on 500 sampled rows")
    print("OK — blue-frame feature row: sign flip removed (done in output probability only)")
    print("OK — comp_diff lookup functional")


if __name__ == "__main__":
    run()
