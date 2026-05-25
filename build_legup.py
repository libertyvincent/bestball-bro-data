#!/usr/bin/env python3
"""Fetch LegUp rankings for the three v1.5d slugs and emit normalized
source feeds under build/sources/.

LegUp's Cloud Functions are public (no auth). For each slug we hit
getRankLinks to discover the current table URL + version, fetch the
table JSON, then normalize to the schema the sim repo's blender expects.

The table JSON is shaped {headers: [...], rows: [[cell, ...], ...]}
where each cell is {"v": <value>, "bg": ..., "c": ..., "b": bool, "a": ...}.
We extract `.v` from every cell and ignore styling.

Column layout is verified strictly via header equality; if LegUp ever
adds/reorders/renames a column we abort loudly so the sim side never
silently consumes mis-aligned data.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

GET_LINKS_URL = (
    "https://us-central1-legendary-upside.cloudfunctions.net/"
    "getRankLinks?slug={slug}"
)

LEGUP_SOURCES = [
    # (slug, slate, output_name)
    ("ud-ranks",         "nfl_2026_season",     "legup_2026_ud"),
    ("eliminator-ranks", "nfl_2026_eliminator", "legup_2026_eliminator"),
    ("main-event",       "nfl_2026_season",     "legup_2026_mainevent"),
]

# Each LegUp slug has its own column layout. We pin expected_headers per
# slug so we abort loudly on any upstream schema change, and use
# field_map to translate output fields -> header names (positions vary).
# A field_map value of None means "this slug doesn't expose that field"
# (e.g. main-event has no Underdog UUID column; the sim blender keys
# those rows by name+pos+team).
SLUG_SCHEMA: dict[str, dict] = {
    "ud-ranks": {
        "expected_headers": [
            "Name", "Team", "Pos", "P Rk", "Rank", "ADP", "Rookie", "+/-", "id",
        ],
        "field_map": {
            "underdog_id":    "id",
            "player_name":    "Name",
            "position":       "Pos",
            "team":           "Team",
            "legup_rank":     "Rank",
            "legup_pos_rank": "P Rk",
        },
    },
    "eliminator-ranks": {
        # Extra "Wk17" column = Week-17 opponent (Eliminator's
        # championship week). We don't consume it here.
        "expected_headers": [
            "Name", "Team", "Wk17", "Pos", "P Rk", "Rank", "ADP", "Rookie", "+/-", "id",
        ],
        "field_map": {
            "underdog_id":    "id",
            "player_name":    "Name",
            "position":       "Pos",
            "team":           "Team",
            "legup_rank":     "Rank",
            "legup_pos_rank": "P Rk",
        },
    },
    "main-event": {
        # No "id" column; the blender resolves name+pos+team. Note the
        # position-rank header is "Pos Rk" here, not "P Rk".
        "expected_headers": [
            "Name", "Team", "Pos", "Pos Rk", "Rank", "Rookie",
        ],
        "field_map": {
            "underdog_id":    None,
            "player_name":    "Name",
            "position":       "Pos",
            "team":           "Team",
            "legup_rank":     "Rank",
            "legup_pos_rank": "Pos Rk",
        },
    },
}

POS_RANK_PREFIX_RE = re.compile(r"^\D+")

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "build" / "sources"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cell_value(cell: Any) -> Any:
    if isinstance(cell, dict):
        return cell.get("v")
    return cell


def parse_pos_rank(raw: Any) -> int | None:
    # "RB1" -> 1, "WR12" -> 12. None / unparseable -> None (don't crash
    # the whole slate over one bad row, e.g. a "DST" or stray label).
    if raw is None:
        return None
    stripped = POS_RANK_PREFIX_RE.sub("", str(raw))
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        return None


def parse_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(str(raw))
    except ValueError:
        return None


def fetch_slug(slug: str, slate: str) -> dict:
    schema = SLUG_SCHEMA.get(slug)
    if schema is None:
        print(f"[legup] {slug}: no SLUG_SCHEMA entry; refusing to parse",
              file=sys.stderr)
        sys.exit(1)
    expected_headers: list[str] = schema["expected_headers"]
    field_map: dict[str, str | None] = schema["field_map"]

    links_resp = requests.get(GET_LINKS_URL.format(slug=slug), timeout=30)
    links_resp.raise_for_status()
    links = links_resp.json()

    table_url = links.get("table")
    version = links.get("version")
    if not table_url:
        print(f"[legup] {slug}: getRankLinks response missing 'table' URL",
              file=sys.stderr)
        sys.exit(1)

    table_resp = requests.get(table_url, timeout=30)
    table_resp.raise_for_status()
    table = table_resp.json()

    headers = [cell_value(h) for h in table.get("headers", [])]
    if headers != expected_headers:
        print(f"ERROR: legup {slug} header mismatch", file=sys.stderr)
        print(f"  expected: {expected_headers}", file=sys.stderr)
        print(f"  got:      {headers}", file=sys.stderr)
        sys.exit(1)
    header_idx = {h: i for i, h in enumerate(headers)}

    rows = table.get("rows", []) or []
    players: list[dict] = []
    skipped: list[tuple[str, str, str]] = []
    has_uuid_column = field_map["underdog_id"] is not None

    for row in rows:
        cells = [cell_value(c) for c in row]
        if len(cells) < len(expected_headers):
            skipped.append(("<short row>", "", ""))
            continue

        def get(field: str) -> Any:
            header = field_map[field]
            return cells[header_idx[header]] if header is not None else None

        ud_id = get("underdog_id")
        name = get("player_name")
        team = get("team")
        pos  = get("position")

        # Slugs with an "id" column require it to be populated; rows
        # without one can't be joined downstream. Slugs without an "id"
        # column (main-event) emit underdog_id=null for every player and
        # the sim-side blender keys them by name+pos+team.
        if has_uuid_column and not ud_id:
            skipped.append((name or "<no name>", team or "", pos or ""))
            continue

        players.append({
            "underdog_id":    ud_id,
            "player_name":    name,
            "position":       pos,
            "team":           team,
            "legup_rank":     parse_int(get("legup_rank")),
            "legup_pos_rank": parse_pos_rank(get("legup_pos_rank")),
        })

    for entry in skipped:
        print(
            f"WARN: legup {slug}: skipped row \"{entry[0]}\" "
            f"({entry[1]}/{entry[2]}) -- no underdog_id",
            file=sys.stderr,
        )
    if len(skipped) >= 5:
        print(
            f"!!! legup {slug}: {len(skipped)} rows skipped (>=5) -- "
            f"investigate, may indicate LegUp renamed the id field",
            file=sys.stderr,
        )

    players.sort(key=lambda p: (p["legup_rank"] is None, p["legup_rank"] or 0))

    return {
        "_meta": {
            "source":       "legup",
            "slug":         slug,
            "slate":        slate,
            "version":      version,
            "fetched_at":   now_iso(),
            "player_count": len(players),
        },
        "players": players,
    }


def main() -> None:
    # Ensure ✓ prints don't crash on Windows cp1252 consoles; CI's Linux
    # locale is already utf-8 so this is a no-op there.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for slug, slate, output_name in LEGUP_SOURCES:
        out = fetch_slug(slug, slate)
        out_path = OUT_DIR / f"{output_name}.json"
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
        print(f"✓ {output_name}: {out['_meta']['player_count']} players")


if __name__ == "__main__":
    main()
