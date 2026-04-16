"""
Phase 2 — Build training dataset from raw PandaScore frames/events.

Reads data/raw/frames/*.json + data/raw/progress.json (for game metadata),
produces data/processed/training_rows.parquet.

Each frame from each game becomes one training row with features that match
what the LLF provides at inference time.
"""
from __future__ import annotations

import json
import sys
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


def _load_game_meta() -> dict[int, dict]:
    """Load game_id -> {winner_id, match_id, ...} from progress file."""
    prog = json.loads(PROGRESS_FILE.read_text())
    meta = {}
    for g in prog["games_collected"]:
        gid = g["game_id"]
        meta[gid] = g
    return meta


def _process_frames_file(path: Path, game_meta: dict) -> list[dict]:
    """Convert a single game's frames JSON into training rows."""
    game_id = int(path.stem)
    if game_id not in game_meta:
        return []

    meta = game_meta[game_id]
    winner_id = meta["winner_id"]

    frames = json.loads(path.read_text())
    if len(frames) < MIN_FRAMES_PER_GAME:
        return []

    blue = frames[0].get("blue", {})
    red = frames[0].get("red", {})
    blue_id = blue.get("id")
    red_id = red.get("id")
    if not blue_id or not red_id:
        return []

    blue_won = 1 if winner_id == blue_id else 0

    rows = []
    for f in frames:
        b = f["blue"]
        r = f["red"]
        ts = f.get("current_timestamp")
        if ts is None:
            continue

        game_minute = ts / 60.0

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
        bg = b.get("gold", 0) or 0
        rg = r.get("gold", 0) or 0
        bh = b.get("heralds", 0) or 0
        rh = r.get("heralds", 0) or 0

        rows.append({
            "game_id": game_id,
            "game_minute": round(game_minute, 2),
            "timestamp_sec": ts,
            "kill_diff": bk - rk,
            "tower_diff": bt - rt,
            "drake_diff": bd - rd,
            "baron_diff": bn - rn,
            "inhib_diff": bi - ri,
            "gold_diff": bg - rg,
            "herald_diff": bh - rh,
            "total_kills": bk + rk,
            "total_objectives": (bt + rt) + (bd + rd) + (bn + rn),
            "blue_kills": bk,
            "red_kills": rk,
            "blue_towers": bt,
            "red_towers": rt,
            "blue_drakes": bd,
            "red_drakes": rd,
            "blue_barons": bn,
            "red_barons": rn,
            "blue_inhibs": bi,
            "red_inhibs": ri,
            "blue_gold": bg,
            "red_gold": rg,
            "blue_won": blue_won,
        })
    return rows


def _process_events_file(path: Path, game_meta: dict) -> list[dict]:
    """Fallback: reconstruct state from play-by-play events."""
    game_id = int(path.stem)
    if game_id not in game_meta:
        return []

    meta = game_meta[game_id]
    winner_id = meta["winner_id"]

    events = json.loads(path.read_text())
    if not events:
        return []

    state = {
        "blue_kills": 0, "red_kills": 0,
        "blue_towers": 0, "red_towers": 0,
        "blue_drakes": 0, "red_drakes": 0,
        "blue_barons": 0, "red_barons": 0,
        "blue_inhibs": 0, "red_inhibs": 0,
    }
    blue_won = None
    rows = []

    event_type_map = {
        "player_kill": "kills",
        "tower_kill": "towers",
        "drake_kill": "drakes",
        "nashor_kill": "barons",
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
        killer_type = killer.get("type", "")

        if killer_type == "player":
            side = killer.get("object", {}).get("side", "")
        else:
            side = ""

        if not side:
            assists = payload.get("assists", [])
            for a in assists:
                if a.get("type") == "player":
                    side = a.get("object", {}).get("side", "")
                    if side:
                        break

        if side in ("blue", "red"):
            state[f"{side}_{stat_key}"] += 1

        if blue_won is None and winner_id:
            # Determine blue_won from first event that has side info
            # We need to map winner_id to blue/red, which requires team IDs
            # from the frame data. For events fallback, we'll set this later.
            pass

        game_minute = ts / 60.0
        bk, rk = state["blue_kills"], state["red_kills"]
        bt, rt = state["blue_towers"], state["red_towers"]
        bd, rd = state["blue_drakes"], state["red_drakes"]
        bn, rn = state["blue_barons"], state["red_barons"]
        bi, ri = state["blue_inhibs"], state["red_inhibs"]

        rows.append({
            "game_id": game_id,
            "game_minute": round(game_minute, 2),
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
            "blue_kills": bk,
            "red_kills": rk,
            "blue_towers": bt,
            "red_towers": rt,
            "blue_drakes": bd,
            "red_drakes": rd,
            "blue_barons": bn,
            "red_barons": rn,
            "blue_inhibs": bi,
            "red_inhibs": ri,
            "blue_gold": 0,
            "red_gold": 0,
            "blue_won": -1,  # placeholder — need side mapping
        })

    return rows


def main():
    print("[build] loading game metadata...")
    game_meta = _load_game_meta()
    print(f"[build] {len(game_meta)} games in metadata")

    all_rows = []
    frames_files = sorted(FRAMES_DIR.glob("*.json"))
    events_files = sorted(EVENTS_DIR.glob("*.json"))

    print(f"[build] processing {len(frames_files)} frame files...")
    skipped = 0
    for i, fp in enumerate(frames_files):
        rows = _process_frames_file(fp, game_meta)
        if rows:
            all_rows.extend(rows)
        else:
            skipped += 1
        if (i + 1) % 500 == 0:
            print(f"  {i+1}/{len(frames_files)} files, {len(all_rows)} rows so far")

    print(f"[build] frames: {len(all_rows)} rows from {len(frames_files) - skipped} games ({skipped} skipped)")

    events_count = 0
    print(f"[build] processing {len(events_files)} event files (fallback)...")
    for fp in events_files:
        rows = _process_events_file(fp, game_meta)
        if rows:
            all_rows.extend(rows)
            events_count += 1

    print(f"[build] events fallback: added {events_count} games")

    if not all_rows:
        print("[build] ERROR: no training rows produced")
        sys.exit(1)

    df = pd.DataFrame(all_rows)

    # Drop events-fallback rows where blue_won couldn't be determined
    before = len(df)
    df = df[df["blue_won"] >= 0]
    if len(df) < before:
        print(f"[build] dropped {before - len(df)} rows with unknown blue_won (events fallback)")

    # Basic stats
    n_games = df["game_id"].nunique()
    print(f"\n[build] DATASET SUMMARY:")
    print(f"  Total rows:    {len(df):,}")
    print(f"  Unique games:  {n_games:,}")
    print(f"  Rows per game: {len(df) / n_games:.1f} avg")
    print(f"  Blue win rate: {df.groupby('game_id')['blue_won'].first().mean():.3f}")
    print(f"  Game minute range: {df['game_minute'].min():.1f} - {df['game_minute'].max():.1f}")
    print(f"  Kill diff range: {df['kill_diff'].min()} to {df['kill_diff'].max()}")
    print(f"  Gold diff range: {df['gold_diff'].min():,} to {df['gold_diff'].max():,}")

    # Save
    out_path = OUT_DIR / "training_rows.parquet"
    df.to_parquet(out_path, index=False)
    print(f"\n[build] saved to {out_path} ({out_path.stat().st_size / 1e6:.1f} MB)")

    # Also save game-level metadata
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
