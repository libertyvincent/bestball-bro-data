#!/usr/bin/env python3
"""Fetch ETR (Establish The Run) Underdog rankings for the four slates
and emit normalized source feeds under build/sources/.

ETR auth is a WordPress session cookie passed via ETR_SESSION_COOKIE.
Each rankings page embeds the dataset as:
    window.PROJECTION_DATASET_<numeric_id> = JSON.parse(`{"rows":[...]}`);

Row entries include `playerId` which is the Underdog UUID, so we join
to the sim repo's slate by UUID directly -- no name+team matching.

Auth-failure detection:
  - 301/302 to a /wp-login.php URL -> exit code 2
  - Response HTML missing PROJECTION_DATASET_ script -> exit code 2
  - Anything else (HTTP error, JSON parse error, etc.) -> exit code 1

The first row's keys are logged to stderr for each slate so the first
real CI run surfaces ETR's actual field names; if any of our candidate
names miss, the value for that field lands as null in the output and
we adjust the candidate lists.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)

ETR_SOURCES = [
    # (short_slug, full_slate_id, url)
    ("season",         "nfl_2026_season",
     "https://establishtherun.com/etrs-top-300-for-underdogfantasy/"),
    ("eliminator",     "nfl_2026_eliminator",
     "https://establishtherun.com/etrs-top-300-for-underdog-fantasy-best-ball-eliminator-rankings-updates-9am-daily/"),
    ("weekly_winners", "nfl_2026_weekly_winners",
     "https://establishtherun.com/underdog-weekly-winners-rankings/"),
    ("superflex",      "nfl_2026_superflex",
     "https://establishtherun.com/etrs-top-300-for-underdog-fantasy-superflex-best-ball-rankings-updates-9am-daily/"),
]

PROJECTION_RE = re.compile(
    r"window\.PROJECTION_DATASET_\d+\s*=\s*JSON\.parse\(`(.+?)`\);",
    re.DOTALL,
)
UPDATED_RE = re.compile(r'data-updated-at="([^"]+)"')
WP_LOGIN_RE = re.compile(r"/wp-login\.php", re.I)

EXIT_AUTH_FAIL = 2
EXIT_GENERIC_FAIL = 1

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "build" / "sources"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pick(row: dict, *candidates: str) -> Any:
    for c in candidates:
        if c in row and row[c] != "":
            return row[c]
    return None


def fetch_slate(short_slug: str, slate_id: str, url: str, cookie: str) -> dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": cookie,
        "Accept": "text/html,application/xhtml+xml",
    }
    resp = requests.get(url, headers=headers, allow_redirects=False, timeout=30)

    if resp.status_code in (301, 302):
        loc = resp.headers.get("Location", "")
        if WP_LOGIN_RE.search(loc):
            print(
                f"[etr] AUTH FAIL: {slate_id} -> redirected to {loc}",
                file=sys.stderr,
            )
            sys.exit(EXIT_AUTH_FAIL)
        print(
            f"[etr] {slate_id}: unexpected redirect to {loc}",
            file=sys.stderr,
        )
        sys.exit(EXIT_GENERIC_FAIL)

    resp.raise_for_status()
    html = resp.text

    m = PROJECTION_RE.search(html)
    if not m:
        print(
            f"[etr] AUTH FAIL: {slate_id} -> no PROJECTION_DATASET in response "
            f"(likely served a login page at HTTP 200)",
            file=sys.stderr,
        )
        sys.exit(EXIT_AUTH_FAIL)

    try:
        payload = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"[etr] {slate_id}: dataset JSON parse error: {e}", file=sys.stderr)
        sys.exit(EXIT_GENERIC_FAIL)

    rows = payload.get("rows") or []
    if not rows:
        print(f"[etr] {slate_id}: dataset has no rows", file=sys.stderr)
        sys.exit(EXIT_GENERIC_FAIL)

    # Surface the actual ETR row schema so the first CI run reveals any
    # field name we haven't anticipated. Cheap (one log line per slate).
    print(
        f"[etr] {slate_id}: first row keys = {sorted(rows[0].keys())}",
        file=sys.stderr,
    )

    updated_match = UPDATED_RE.search(html)
    etr_updated_at = updated_match.group(1) if updated_match else None

    players: list[dict] = []
    skipped = 0
    for r in rows:
        underdog_id = r.get("playerId")
        if not underdog_id:
            skipped += 1
            continue
        players.append({
            "underdog_id":    underdog_id,
            "player_name":    pick(r, "playerName", "name", "displayName",
                                  "fullName"),
            "position":       pick(r, "position", "pos"),
            "team":           pick(r, "team", "teamAbbr", "nflTeam",
                                  "teamAbbreviation"),
            "etr_rank":       pick(r, "rank", "overallRank", "etrRank"),
            "etr_pos_rank":   pick(r, "positionRank", "posRank",
                                  "positionRankFormatted"),
            "adp":            pick(r, "adp", "underdogAdp", "fantasyADP"),
            "adp_diff":       pick(r, "adpDiff", "adpDifference",
                                  "rankAdpDiff"),
            "pos_rank_adp":   pick(r, "positionRankAdp", "posRankAdp",
                                  "adpPositionRank"),
            "pos_rank_diff":  pick(r, "positionRankDiff", "posRankDiff",
                                  "positionRankAdpDiff"),
            "ownership_pct":  pick(r, "ownership", "ownershipPct",
                                  "ownershipPercent", "ownershipPercentage"),
        })

    if skipped:
        print(
            f"[etr] {slate_id}: skipped {skipped} rows with no playerId",
            file=sys.stderr,
        )

    players.sort(
        key=lambda p: (
            p["etr_rank"] is None,
            float(p["etr_rank"]) if p["etr_rank"] is not None else 0,
        )
    )

    return {
        "_meta": {
            "source":         "etr",
            "slate":          slate_id,
            "fetched_at":     now_iso(),
            "etr_updated_at": etr_updated_at,
            "page_url":       url,
            "player_count":   len(players),
        },
        "players": players,
    }


def main() -> None:
    # Ensure ✓ prints don't crash on Windows cp1252 consoles; CI's Linux
    # locale is already utf-8 so this is a no-op there.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    cookie = os.environ.get("ETR_SESSION_COOKIE")
    if not cookie:
        print(
            "ETR_SESSION_COOKIE not set in environment",
            file=sys.stderr,
        )
        sys.exit(EXIT_GENERIC_FAIL)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for short_slug, slate_id, url in ETR_SOURCES:
        out = fetch_slate(short_slug, slate_id, url, cookie)
        out_path = OUT_DIR / f"etr_2026_{short_slug}.json"
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
        print(f"✓ etr_{short_slug}: {out['_meta']['player_count']} players")


if __name__ == "__main__":
    main()
