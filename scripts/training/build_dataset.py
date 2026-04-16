"""
Phase 2 — Build training dataset from raw PandaScore frames/events.

Reads data/raw/frames/*.json + data/raw/progress.json (for game metadata),
produces data/processed/training_rows.parquet + champion_scores.parquet.

Each frame from each game becomes one training row with features that match
what the LLF provides at inference time — kill/tower/drake/baron/inhib diffs,
plus momentum features (how the lead has shifted over recent minutes) and
champion composition features (leave-one-out marginal win rate per champion).

Gold/heralds are excluded because LLF does not stream them live.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
RAW_DIR = DATA_DIR / "raw"
FRAMES_DIR = RAW_DIR / "frames"
EVENTS_DIR = RAW_DIR / "events"
PROGRESS_FILE = RAW_DIR / "progress.json"
OUT_DIR = DATA_DIR / "processed"
OUT_DIR.mkdir(parents=True, exist_ok=True)

MIN_GAME_LENGTH_SEC = 300
MIN_FRAMES_PER_GAME = 3

# Momentum look-back windows (seconds). Kept small enough that most frames
# within a match have prior frames to compare to.
LAG_3MIN_SEC = 180
LAG_5MIN_SEC = 300

ROLES = ("top", "jun", "mid", "adc", "sup")


# ── Loaders ─────────────────────────────────────────────────────────────

def _load_game_meta() -> dict[int, dict]:
    prog = json.loads(PROGRESS_FILE.read_text())
    return {g["game_id"]: g for g in prog["games_collected"]}


def _extract_champions(frame: dict) -> tuple[list[str], list[str]]:
    """Return ([blue_champs], [red_champs]) from frame's players struct.
    Missing picks become empty string so downstream code can filter them.
    """
    def side_champs(side_obj: dict) -> list[str]:
        players = side_obj.get("players", {}) or {}
        out = []
        for r in ROLES:
            p = players.get(r) or {}
            ch = (p.get("champion") or {}).get("slug") or ""
            out.append(ch)
        return out
    return side_champs(frame.get("blue", {})), side_champs(frame.get("red", {}))


# ── Frame → per-game row list ───────────────────────────────────────────

def _process_frames_file(path: Path, game_meta: dict) -> tuple[list[dict], dict | None]:
    """Return (rows, game_info) where game_info holds champs/label for
    later champion-score computation. rows are per-frame with stats only;
    momentum/champion features are added in a second pass."""
    game_id = int(path.stem)
    if game_id not in game_meta:
        return [], None

    meta = game_meta[game_id]
    winner_id = meta["winner_id"]

    frames = json.loads(path.read_text())
    if len(frames) < MIN_FRAMES_PER_GAME:
        return [], None

    blue = frames[0].get("blue", {}) or {}
    red = frames[0].get("red", {}) or {}
    blue_id = blue.get("id")
    red_id = red.get("id")
    if not blue_id or not red_id:
        return [], None

    blue_won = 1 if winner_id == blue_id else 0
    blue_champs, red_champs = _extract_champions(frames[0])

    rows = []
    for f in frames:
        b = f.get("blue", {}) or {}
        r = f.get("red", {}) or {}
        ts = f.get("current_timestamp")
        if ts is None:
            continue

        bk = b.get("kills", 0) or 0
        rk = r.get("kills", 0) or 0
        bt = b.get("towers", 0) or 0
        rt = r.get("towers", 0) or 0
        bd = b.get("drakes", 0) or 0
        rd = r.get("drakes", 0) or 0
        bn = b.get("nashors", 0) or 0
        rn = r.get("nashors", 0) or 0
        bi = b.get("inhibitors", 0) or 0
        ri = r.get("inhibitors", 0) or 0
        bh = b.get("heralds", 0) or 0
        rh = r.get("heralds", 0) or 0

        rows.append({
            "game_id": game_id,
            "game_minute": round(ts / 60.0, 2),
            "timestamp_sec": ts,
            "kill_diff": bk - rk,
            "tower_diff": bt - rt,
            "drake_diff": bd - rd,
            "baron_diff": bn - rn,
            "inhib_diff": bi - ri,
            "gold_diff": (b.get("gold", 0) or 0) - (r.get("gold", 0) or 0),
            "herald_diff": bh - rh,
            "total_kills": bk + rk,
            "total_objectives": (bt + rt) + (bd + rd) + (bn + rn),
            "blue_kills": bk, "red_kills": rk,
            "blue_towers": bt, "red_towers": rt,
            "blue_drakes": bd, "red_drakes": rd,
            "blue_barons": bn, "red_barons": rn,
            "blue_inhibs": bi, "red_inhibs": ri,
            "blue_gold": b.get("gold", 0) or 0,
            "red_gold": r.get("gold", 0) or 0,
            "blue_won": blue_won,
        })

    game_info = {
        "game_id": game_id,
        "blue_won": blue_won,
        "blue_champs": blue_champs,
        "red_champs": red_champs,
    }
    return rows, game_info


# ── Events fallback (no champion info available) ────────────────────────

def _process_events_file(path: Path, game_meta: dict) -> list[dict]:
    """Rebuild state from play-by-play events for games missing frame data."""
    game_id = int(path.stem)
    if game_id not in game_meta:
        return []

    events = json.loads(path.read_text())
    if not events:
        return []

    state = {k: 0 for k in (
        "blue_kills", "red_kills",
        "blue_towers", "red_towers",
        "blue_drakes", "red_drakes",
        "blue_barons", "red_barons",
        "blue_inhibs", "red_inhibs",
    )}
    rows = []

    event_type_map = {
        "player_kill": "kills", "tower_kill": "towers",
        "drake_kill": "drakes", "nashor_kill": "barons",
        "inhibitor_kill": "inhibs",
    }

    for ev in sorted(events, key=lambda e: e.get("ingame_timestamp", 0)):
        etype = ev.get("type", "")
        ts = ev.get("ingame_timestamp", 0)
        stat_key = event_type_map.get(etype)
        if not stat_key:
            continue
        payload = ev.get("payload", {})
        killer = payload.get("killer", {})
        side = (killer.get("object", {}) or {}).get("side", "") if killer.get("type") == "player" else ""
        if not side:
            for a in (payload.get("assists") or []):
                if a.get("type") == "player":
                    side = (a.get("object", {}) or {}).get("side", "")
                    if side:
                        break
        if side in ("blue", "red"):
            state[f"{side}_{stat_key}"] += 1

        bk, rk = state["blue_kills"], state["red_kills"]
        bt, rt = state["blue_towers"], state["red_towers"]
        bd, rd = state["blue_drakes"], state["red_drakes"]
        bn, rn = state["blue_barons"], state["red_barons"]
        bi, ri = state["blue_inhibs"], state["red_inhibs"]

        rows.append({
            "game_id": game_id,
            "game_minute": round(ts / 60.0, 2),
            "timestamp_sec": ts,
            "kill_diff": bk - rk,
            "tower_diff": bt - rt,
            "drake_diff": bd - rd,
            "baron_diff": bn - rn,
            "inhib_diff": bi - ri,
            "gold_diff": 0,
            "herald_diff": 0,
            "total_kills": bk + rk,
            "total_objectives": (bt + rt) + (bd + rd) + (bn + rn),
            "blue_kills": bk, "red_kills": rk,
            "blue_towers": bt, "red_towers": rt,
            "blue_drakes": bd, "red_drakes": rd,
            "blue_barons": bn, "red_barons": rn,
            "blue_inhibs": bi, "red_inhibs": ri,
            "blue_gold": 0, "red_gold": 0,
            "blue_won": -1,
        })
    return rows


# ── Champion marginal win-rate table (leave-one-out at row time) ───────

def _build_champion_table(game_infos: list[dict]) -> dict[str, dict[str, int]]:
    """Count wins / games per champion across the full dataset. Lookup is
    LOO-adjusted per row later — we subtract that game's contribution when
    computing the score for training rows of that game.
    """
    table: dict[str, dict[str, int]] = defaultdict(lambda: {"wins": 0, "games": 0})
    for g in game_infos:
        bw = g["blue_won"]
        for ch in g["blue_champs"]:
            if not ch:
                continue
            table[ch]["wins"] += bw
            table[ch]["games"] += 1
        for ch in g["red_champs"]:
            if not ch:
                continue
            table[ch]["wins"] += (1 - bw)
            table[ch]["games"] += 1
    return dict(table)


def _loo_winrate(champion: str, on_blue_side: bool, this_game_blue_won: int,
                  table: dict[str, dict[str, int]], global_mean: float,
                  shrinkage_k: int = 30) -> float:
    """Leave-one-out win rate for a champion, shrunk toward the global mean
    to stabilise rare picks. shrinkage_k chosen so a champ with 30 games
    has half-weight from the prior.
    """
    s = table.get(champion)
    if not s or s["games"] <= 1:
        return global_mean
    this_win = this_game_blue_won if on_blue_side else (1 - this_game_blue_won)
    wins = s["wins"] - this_win
    games = s["games"] - 1
    if games <= 0:
        return global_mean
    raw = wins / games
    # Shrinkage: (games * raw + k * global_mean) / (games + k)
    return (games * raw + shrinkage_k * global_mean) / (games + shrinkage_k)


# ── Momentum features per game ──────────────────────────────────────────

def _add_momentum_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add momentum features computed per-game using the game's own prior frames.

    Features:
      kill_diff_lag_3m   : kill_diff from ~3min ago (0 before that window exists)
      kill_diff_delta_3m : kill_diff - kill_diff_lag_3m
      obj_diff_lag_3m    : total_objective_diff from ~3min ago
      obj_diff_delta_3m  : delta
      peak_kill_diff     : signed value at the peak |kill_diff| so far
      lead_retraction    : peak_kill_diff - kill_diff
                           (positive if the side that led has given up lead)
    """
    df = df.sort_values(["game_id", "timestamp_sec"]).reset_index(drop=True)
    out = df.copy()

    # Per-game rolling computations
    groups = df.groupby("game_id", sort=False)

    # Obj diff = tower + drake + baron + inhib (not kills, not gold — LLF parity)
    obj_diff = (df["tower_diff"] + df["drake_diff"]
                + df["baron_diff"] + df["inhib_diff"])
    out["obj_diff"] = obj_diff

    lag_kd = []
    lag_od = []
    peak_kd_signed = []
    for gid, idx in groups.groups.items():
        idx_list = list(idx)
        ts = df.loc[idx_list, "timestamp_sec"].to_numpy()
        kd = df.loc[idx_list, "kill_diff"].to_numpy()
        od = obj_diff.loc[idx_list].to_numpy()

        # Pointer walk: for each i, find largest j <= i with ts[j] <= ts[i] - LAG
        lag_kd_g = [0] * len(idx_list)
        lag_od_g = [0] * len(idx_list)
        j = 0
        for i in range(len(idx_list)):
            target = ts[i] - LAG_3MIN_SEC
            while j < i and ts[j + 1] <= target:
                j += 1
            if ts[j] <= target:
                lag_kd_g[i] = int(kd[j])
                lag_od_g[i] = int(od[j])
            # else: no prior frame covers target → leave 0
        lag_kd.extend(zip(idx_list, lag_kd_g))
        lag_od.extend(zip(idx_list, lag_od_g))

        # Peak signed kill_diff so far — track the extremum of |kd|
        peak_signed = 0
        peak_signed_g = []
        for v in kd:
            if abs(v) > abs(peak_signed):
                peak_signed = int(v)
            peak_signed_g.append(peak_signed)
        peak_kd_signed.extend(zip(idx_list, peak_signed_g))

    lag_kd_s = pd.Series({i: v for i, v in lag_kd})
    lag_od_s = pd.Series({i: v for i, v in lag_od})
    peak_s = pd.Series({i: v for i, v in peak_kd_signed})

    out["kill_diff_lag_3m"] = lag_kd_s
    out["obj_diff_lag_3m"] = lag_od_s
    out["peak_kill_diff"] = peak_s
    out["kill_diff_delta_3m"] = out["kill_diff"] - out["kill_diff_lag_3m"]
    out["obj_diff_delta_3m"] = out["obj_diff"] - out["obj_diff_lag_3m"]
    out["lead_retraction"] = out["peak_kill_diff"] - out["kill_diff"]
    return out


# ── Main ────────────────────────────────────────────────────────────────

def main():
    print("[build] loading game metadata...")
    game_meta = _load_game_meta()
    print(f"[build] {len(game_meta)} games in metadata")

    all_rows: list[dict] = []
    game_infos: list[dict] = []
    frames_files = sorted(FRAMES_DIR.glob("*.json"))
    events_files = sorted(EVENTS_DIR.glob("*.json"))

    print(f"[build] processing {len(frames_files)} frame files...")
    skipped = 0
    for i, fp in enumerate(frames_files):
        rows, gi = _process_frames_file(fp, game_meta)
        if rows and gi:
            all_rows.extend(rows)
            game_infos.append(gi)
        else:
            skipped += 1
        if (i + 1) % 1000 == 0:
            print(f"  {i+1}/{len(frames_files)} files, {len(all_rows)} rows so far")
    print(f"[build] frames: {len(all_rows)} rows from {len(game_infos)} games ({skipped} skipped)")

    events_count = 0
    print(f"[build] processing {len(events_files)} event files (fallback, no champs)...")
    for fp in events_files:
        rows = _process_events_file(fp, game_meta)
        if rows:
            all_rows.extend(rows)
            events_count += 1
    print(f"[build] events fallback: added {events_count} games (no champion features)")

    if not all_rows:
        print("[build] ERROR: no training rows produced")
        sys.exit(1)

    df = pd.DataFrame(all_rows)
    before = len(df)
    df = df[df["blue_won"] >= 0]
    if len(df) < before:
        print(f"[build] dropped {before - len(df)} rows with unknown blue_won (events fallback)")

    # ── Momentum features ──
    print("[build] computing momentum features...")
    df = _add_momentum_features(df)

    # ── Champion scores (LOO) ──
    print("[build] computing champion LOO win rates...")
    champ_table = _build_champion_table(game_infos)
    # Global mean across all appearances
    total_w = sum(v["wins"] for v in champ_table.values())
    total_g = sum(v["games"] for v in champ_table.values())
    global_mean = total_w / total_g if total_g else 0.5
    print(f"[build] {len(champ_table)} distinct champions, global win rate={global_mean:.3f}")

    gi_by_id = {g["game_id"]: g for g in game_infos}

    blue_comp_scores = {}
    red_comp_scores = {}
    for gid, gi in gi_by_id.items():
        bw = gi["blue_won"]
        b_scores = [_loo_winrate(ch, True, bw, champ_table, global_mean)
                    for ch in gi["blue_champs"] if ch]
        r_scores = [_loo_winrate(ch, False, bw, champ_table, global_mean)
                    for ch in gi["red_champs"] if ch]
        blue_comp_scores[gid] = sum(b_scores) / len(b_scores) if b_scores else global_mean
        red_comp_scores[gid] = sum(r_scores) / len(r_scores) if r_scores else global_mean

    # Rows with no champs (events fallback) get global_mean → comp_diff = 0
    df["blue_comp_score"] = df["game_id"].map(blue_comp_scores).fillna(global_mean)
    df["red_comp_score"] = df["game_id"].map(red_comp_scores).fillna(global_mean)
    df["comp_diff"] = df["blue_comp_score"] - df["red_comp_score"]

    # ── Summary ──
    n_games = df["game_id"].nunique()
    print(f"\n[build] DATASET SUMMARY:")
    print(f"  Total rows:     {len(df):,}")
    print(f"  Unique games:   {n_games:,}")
    print(f"  Rows per game:  {len(df) / n_games:.1f} avg")
    print(f"  Blue win rate:  {df.groupby('game_id')['blue_won'].first().mean():.3f}")
    print(f"  Minute range:   {df['game_minute'].min():.1f} - {df['game_minute'].max():.1f}")
    print(f"  Kill diff:      {df['kill_diff'].min()} to {df['kill_diff'].max()}")
    print(f"  Lead retract:   {df['lead_retraction'].min()} to {df['lead_retraction'].max()}")
    print(f"  comp_diff:      {df['comp_diff'].min():+.3f} to {df['comp_diff'].max():+.3f}")
    print(f"  kd_delta_3m:    {df['kill_diff_delta_3m'].min()} to {df['kill_diff_delta_3m'].max()}")

    # ── Save ──
    out_path = OUT_DIR / "training_rows.parquet"
    df.to_parquet(out_path, index=False)
    print(f"\n[build] saved to {out_path} ({out_path.stat().st_size / 1e6:.1f} MB)")

    # Champion score table for runtime use
    champ_rows = []
    for ch, s in champ_table.items():
        if s["games"] < 5:
            continue
        raw = s["wins"] / s["games"]
        # Use shrinkage-smoothed score (no LOO at inference time — use full-dataset rate)
        shrunk = (s["games"] * raw + 30 * global_mean) / (s["games"] + 30)
        champ_rows.append({
            "champion": ch,
            "games": s["games"],
            "wins": s["wins"],
            "winrate_raw": raw,
            "winrate_shrunk": shrunk,
        })
    champ_df = pd.DataFrame(champ_rows).sort_values("games", ascending=False)
    champ_path = OUT_DIR / "champion_scores.parquet"
    champ_df.to_parquet(champ_path, index=False)
    print(f"[build] champion scores: {len(champ_df)} champions → {champ_path}")
    print(f"        global mean: {global_mean:.3f}")
    print(f"        top 10 by games played:")
    print(champ_df.head(10)[["champion", "games", "winrate_raw", "winrate_shrunk"]].to_string(index=False))

    game_df = df.groupby("game_id").agg(
        blue_won=("blue_won", "first"),
        n_frames=("game_id", "count"),
        max_minute=("game_minute", "max"),
    ).reset_index()
    meta_path = OUT_DIR / "games_meta.parquet"
    game_df.to_parquet(meta_path, index=False)
    print(f"[build] game metadata saved to {meta_path}")


if __name__ == "__main__":
    main()
