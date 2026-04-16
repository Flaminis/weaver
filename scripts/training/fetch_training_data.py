"""
Phase 1 — Fetch historical LoL game data from PandaScore for model training.

Collects ~5000 finished games via:
  1. GET /lol/matches/past  (paginated, 100/page)
  2. GET /lol/games/{id}/frames  (periodic state snapshots)
  3. Fallback: GET /lol/games/{id}/events  (play-by-play, reconstruct state)

Saves raw JSON to data/raw/ with resumable checkpointing.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

PS_KEY = os.getenv("PANDASCORE_API_KEY", "")
PS_BASE = "https://api.pandascore.co"
HEADERS = {"Authorization": f"Bearer {PS_KEY}"}

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
RAW_DIR = DATA_DIR / "raw"
MATCHES_DIR = RAW_DIR / "matches"
FRAMES_DIR = RAW_DIR / "frames"
EVENTS_DIR = RAW_DIR / "events"
PROGRESS_FILE = RAW_DIR / "progress.json"

TARGET_GAMES = 10000
MATCHES_PER_PAGE = 100
FRAMES_PER_PAGE = 100
EVENTS_PER_PAGE = 100
MAX_CONCURRENT = 8
REQ_DELAY_SEC = 0.12  # ~8 req/s to stay within limits

for d in (MATCHES_DIR, FRAMES_DIR, EVENTS_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {
        "matches_pages_done": 0,
        "games_collected": [],
        "frames_done": [],
        "events_done": [],
        "failed_games": [],
    }


def _save_progress(prog: dict):
    PROGRESS_FILE.write_text(json.dumps(prog, indent=2))


async def _api_get(
    client: httpx.AsyncClient,
    path: str,
    params: dict | None = None,
    semaphore: asyncio.Semaphore | None = None,
    retries: int = 4,
) -> list | dict | None:
    url = f"{PS_BASE}{path}"
    sem = semaphore or asyncio.Semaphore(1)
    for attempt in range(retries):
        async with sem:
            await asyncio.sleep(REQ_DELAY_SEC)
            try:
                r = await client.get(url, params=params or {}, headers=HEADERS, timeout=30)
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadError) as e:
                wait = 2 ** attempt
                print(f"  [net-err] {path} attempt {attempt+1}: {e}, retry in {wait}s")
                await asyncio.sleep(wait)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 2 ** (attempt + 1)))
                print(f"  [429] rate-limited on {path}, waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                continue
            if r.status_code in (403, 404):
                return None
            print(f"  [http {r.status_code}] {path} attempt {attempt+1}")
            await asyncio.sleep(2 ** attempt)
    return None


async def fetch_matches(client: httpx.AsyncClient, progress: dict) -> list[dict]:
    """Paginate through /lol/matches/past to collect game metadata."""
    all_games: list[dict] = []
    known_game_ids = set(g["game_id"] for g in progress["games_collected"])
    page = progress["matches_pages_done"] + 1

    while len(all_games) + len(known_game_ids) < TARGET_GAMES:
        print(f"[matches] page {page}  (total games so far: {len(all_games) + len(known_game_ids)})")
        data = await _api_get(
            client,
            "/lol/matches/past",
            params={
                "per_page": MATCHES_PER_PAGE,
                "page": page,
                "sort": "-scheduled_at",
                "filter[detailed_stats]": "true",
                "filter[status]": "finished",
            },
        )
        if not data:
            print(f"[matches] no data on page {page}, stopping")
            break

        for match in data:
            match_id = match.get("id")
            opponents = match.get("opponents", [])
            if len(opponents) < 2:
                continue

            team_map = {}
            for opp in opponents:
                t = opp.get("opponent", {})
                team_map[t.get("id")] = {
                    "name": t.get("name", ""),
                    "acronym": t.get("acronym", ""),
                }

            for game in match.get("games", []):
                gid = game.get("id")
                if not gid or gid in known_game_ids:
                    continue

                winner = game.get("winner", {})
                winner_id = winner.get("id") if winner else None
                if not winner_id:
                    continue
                if game.get("forfeit"):
                    continue
                length = game.get("length")
                if length is not None and length < 300:
                    continue
                if game.get("status") != "finished":
                    continue

                all_games.append({
                    "game_id": gid,
                    "match_id": match_id,
                    "winner_id": winner_id,
                    "length": length,
                    "position": game.get("position"),
                    "teams": team_map,
                })
                known_game_ids.add(gid)

        # Save match page to disk for reference
        match_file = MATCHES_DIR / f"page_{page}.json"
        match_file.write_text(json.dumps(data, indent=2))

        progress["matches_pages_done"] = page
        _save_progress(progress)
        page += 1

        if len(data) < MATCHES_PER_PAGE:
            print("[matches] reached last page")
            break

    return all_games


def _games_needing_data(progress: dict) -> list[dict]:
    """Return games that still need frames or events fetched (deduplicated)."""
    done_set = set(progress["frames_done"]) | set(progress["events_done"])
    failed_set = set(progress["failed_games"])
    seen: set[int] = set()
    result = []
    for g in progress["games_collected"]:
        gid = g["game_id"]
        if gid not in done_set and gid not in failed_set and gid not in seen:
            result.append(g)
            seen.add(gid)
    return result


async def _fetch_frames_for_game(
    client: httpx.AsyncClient,
    game_id: int,
    sem: asyncio.Semaphore,
) -> list[dict] | None:
    """Fetch all frame pages for a single game. Returns None if unavailable."""
    all_frames = []
    page = 1
    while True:
        data = await _api_get(
            client,
            f"/lol/games/{game_id}/frames",
            params={"per_page": FRAMES_PER_PAGE, "page": page},
            semaphore=sem,
        )
        if data is None:
            return None
        if not data:
            break
        all_frames.extend(data)
        if len(data) < FRAMES_PER_PAGE:
            break
        page += 1
    return all_frames if all_frames else None


async def _fetch_events_for_game(
    client: httpx.AsyncClient,
    game_id: int,
    sem: asyncio.Semaphore,
) -> list[dict] | None:
    """Fetch all event pages for a single game."""
    all_events = []
    page = 1
    while True:
        data = await _api_get(
            client,
            f"/lol/games/{game_id}/events",
            params={"per_page": EVENTS_PER_PAGE, "page": page},
            semaphore=sem,
        )
        if data is None:
            return None
        if not data:
            break
        all_events.extend(data)
        if len(data) < EVENTS_PER_PAGE:
            break
        page += 1
    return all_events if all_events else None


async def _fetch_single_game(
    client: httpx.AsyncClient,
    game: dict,
    sem: asyncio.Semaphore,
    progress: dict,
    idx: int,
    total: int,
):
    gid = game["game_id"]
    label = f"[{idx+1}/{total}] game {gid}"

    frames = await _fetch_frames_for_game(client, gid, sem)
    if frames:
        out = FRAMES_DIR / f"{gid}.json"
        out.write_text(json.dumps(frames))
        progress["frames_done"].append(gid)
        print(f"  {label}: {len(frames)} frames saved")
        _save_progress(progress)
        return

    events = await _fetch_events_for_game(client, gid, sem)
    if events:
        out = EVENTS_DIR / f"{gid}.json"
        out.write_text(json.dumps(events))
        progress["events_done"].append(gid)
        print(f"  {label}: {len(events)} events saved (fallback)")
        _save_progress(progress)
        return

    progress["failed_games"].append(gid)
    print(f"  {label}: FAILED (no frames or events)")
    _save_progress(progress)


async def fetch_game_data(client: httpx.AsyncClient, progress: dict):
    """Fetch frames (or events fallback) for all collected games."""
    games = _games_needing_data(progress)
    if not games:
        print("[game-data] all games already fetched")
        return

    total = len(games)
    print(f"[game-data] fetching data for {total} games")
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    batch_size = 50
    for batch_start in range(0, total, batch_size):
        batch = games[batch_start : batch_start + batch_size]
        tasks = [
            _fetch_single_game(client, g, sem, progress, batch_start + i, total)
            for i, g in enumerate(batch)
        ]
        await asyncio.gather(*tasks)
        print(f"  --- batch done ({batch_start + len(batch)}/{total}) ---")


async def main():
    progress = _load_progress()

    already_have = len(progress["frames_done"]) + len(progress["events_done"])
    print(f"[resume] {already_have} games already fetched, "
          f"{len(progress['games_collected'])} game IDs collected, "
          f"{progress['matches_pages_done']} match pages done")

    # Deduplicate on load
    seen_ids: dict[int, dict] = {}
    for g in progress["games_collected"]:
        seen_ids.setdefault(g["game_id"], g)
    progress["games_collected"] = list(seen_ids.values())
    progress["frames_done"] = list(set(progress["frames_done"]))
    progress["events_done"] = list(set(progress["events_done"]))
    progress["failed_games"] = list(set(progress["failed_games"]))
    _save_progress(progress)

    async with httpx.AsyncClient() as client:
        existing_ids = {g["game_id"] for g in progress["games_collected"]}
        if len(existing_ids) < TARGET_GAMES:
            new_games = await fetch_matches(client, progress)
            for g in new_games:
                if g["game_id"] not in existing_ids:
                    progress["games_collected"].append(g)
                    existing_ids.add(g["game_id"])
            _save_progress(progress)
            print(f"[matches] total unique game IDs: {len(existing_ids)}")
        else:
            print(f"[matches] already have {len(existing_ids)} unique game IDs")

        await fetch_game_data(client, progress)

    done = len(progress["frames_done"]) + len(progress["events_done"])
    failed = len(progress["failed_games"])
    print(f"\n[DONE] {done} games with data, {failed} failed, "
          f"{len(progress['games_collected'])} total IDs")


if __name__ == "__main__":
    asyncio.run(main())
