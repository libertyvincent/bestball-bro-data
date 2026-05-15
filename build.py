#!/usr/bin/env python3
"""
build.py — daily refresh of Mike Clay's NFL projections.

Pipeline:
  1. Download the Clay PDF from ESPN.
  2. Parse the 32 per-team offense tables (pages 2-33).
  3. Resolve team names to canonical NFL abbreviations using
     nfl_2026.json (the "team key", sourced from Underdog).
  4. Build the components dict, compute proj_total via the
     scoring_rules baked in here, derive proj_ppg, compute VOR
     vs replacement_levels, and assign a tier band.
  5. Rank within each position by Clay's PPR Pts (clay_ppr_total)
     to set clay_pos_rk.
  6. Write projections/nfl_2026.json with the existing schema.
     Keys are "Name|TEAM|POS"; TEAM is canonical (ARI/BAL/CLE/HOU/...).

Designed to run unattended in GitHub Actions. Exits non-zero on any
unrecoverable error so the workflow surfaces failures instead of
committing broken JSON.
"""

from __future__ import annotations

import io
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber
import requests

# --- Paths & sources -----------------------------------------------------
ROOT = Path(__file__).parent
TEAMS_FILE = ROOT / "teams" / "nfl_2026.json"
OUTPUT_FILE = ROOT / "projections" / "nfl_2026.json"
SOURCE_URL = (
    "https://g.espncdn.com/s/ffldraftkit/26/"
    "NFLDK2026_CS_ClayProjections2026.pdf"
)

# --- Scoring & replacement levels ---------------------------------------
# Mirrors what's in the existing JSON's _meta. Single source of truth lives
# here; we also echo these into the output _meta so consumers see exactly
# what was used.
REPLACEMENT_LEVELS = {"QB": 200.0, "RB": 110.0, "WR": 100.0, "TE": 85.0}
SCORING_RULES = {
    "pass_yd": 0.04,
    "pass_td": 4.0,
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rec": 0.5,
    "rec_yd": 0.1,
    "rec_td": 6.0,
}

# --- Team abbreviation normalization ------------------------------------
# ESPN's PDF uses a handful of nonstandard team codes (notably on the
# category leaderboard pages -- the per-team pages use full team names in
# titles, which we resolve via TEAMS_FILE). The map exists for completeness
# and as a safety net if Clay ships an unexpected code.
ESPN_TO_STANDARD = {
    "ARZ": "ARI",  # Arizona
    "BLT": "BAL",  # Baltimore
    "CLV": "CLE",  # Cleveland
    "HST": "HOU",  # Houston
    "JAC": "JAX",  # Jacksonville (some feeds drop the X)
    "WSH": "WAS",  # Washington
    "LA":  "LAR",  # LA Rams (some feeds drop the R)
    "SD":  "LAC",  # Chargers (legacy San Diego code)
}

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")
MIN_EXPECTED_PLAYERS = 350  # safety floor -- Clay's PDF ships ~414 players

# --- Tier bands ---------------------------------------------------------
# Per-position VOR thresholds. Tuned against the existing JSON's tier
# assignments. Bands are deliberately a little wide to keep tier
# membership stable across minor projection updates. Retune in one place.
TIER_BANDS = {
    "QB": [(175, 1), (100, 2), (50, 3), (20, 4), (0, 5),
           (-100, 6), (-150, 7), (-180, 8)],
    "RB": [(190, 1), (130, 2), (80, 3), (30, 4), (-50, 5),
           (-90, 6), (-105, 7), (-110, 8)],
    "WR": [(150, 1), (90, 2), (50, 3), (10, 4), (-50, 5),
           (-85, 6), (-95, 7), (-100, 8)],
    "TE": [(100, 1), (50, 2), (20, 3), (0, 4), (-40, 5),
           (-65, 6), (-75, 7), (-82, 8)],
}


def assign_tier(vor: float, pos: str) -> int:
    """Return tier band (1 = best, 9 = worst) based on VOR."""
    for threshold, tier in TIER_BANDS[pos]:
        if vor >= threshold:
            return tier
    return 9


def compute_proj_total(c: dict) -> float:
    """Fantasy points from component stats. Underdog half-PPR scoring."""
    return round(
        c["pass_yd"] * SCORING_RULES["pass_yd"]
        + c["pass_td"] * SCORING_RULES["pass_td"]
        + c["rush_yd"] * SCORING_RULES["rush_yd"]
        + c["rush_td"] * SCORING_RULES["rush_td"]
        + c["rec"]     * SCORING_RULES["rec"]
        + c["rec_yd"]  * SCORING_RULES["rec_yd"]
        + c["rec_td"]  * SCORING_RULES["rec_td"],
        1,
    )


def normalize_team(raw: str) -> str:
    """Map ESPN-style team codes to canonical NFL abbreviations."""
    code = (raw or "").upper().strip()
    return ESPN_TO_STANDARD.get(code, code)


def load_team_keys():
    """Canonical abbreviations and a 'Team Name -> ABBR' map from
    nfl_2026.json -- our source of truth for the 32 NFL franchises."""
    with open(TEAMS_FILE, encoding="utf-8") as fh:
        data = json.load(fh)
    teams = data["teams"]
    if len(teams) != 32:
        sys.exit(f"[build] {TEAMS_FILE} has {len(teams)} teams, expected 32")
    abbrs = {t["abbr"] for t in teams.values()}
    name_to_abbr = {t["name"]: t["abbr"] for t in teams.values()}
    return abbrs, name_to_abbr


# --- PDF parsing --------------------------------------------------------
# Match a per-team page title (e.g. "2026 Cleveland Browns Projections").
# Character class allows digits so "49ers" matches.
TITLE_RE = re.compile(r"2026\s+([A-Za-z][A-Za-z0-9'\.\s]+?)\s+Projections\b")

# Column order on the offense table (18 columns):
#   Pos | Player | Gm
#       | Pass(Att Comp Yds TD INT Sk)
#       | Rush(Att Yds TD)
#       | Rec(Tgt Rec Yd TD)
#       | Pts | Rk
# Cells[2:18] are the 16 numeric stat columns; cells[0]=Pos, cells[1]=name.
OFFENSE_HEADER_CELLS = ("Pos", "Player", "Gm")


def parse_team_page(page, abbr: str) -> list:
    """Extract QB/RB/WR/TE rows from a team page using table extraction.

    The team pages have three side-by-side tables (offense | defense |
    weekly scores) plus several smaller ones below. extract_text() glues
    rows across all columns, which made line-regex parsing impossible.
    extract_tables() respects the drawn table boundaries.
    """
    players = []
    tables = page.extract_tables() or []
    for table in tables:
        if not table or len(table) < 2:
            continue
        header = [str(c or "").strip() for c in table[0]]
        # The offense table is the only one starting with Pos | Player | Gm.
        if len(header) < 18 or tuple(header[:3]) != OFFENSE_HEADER_CELLS:
            continue
        for row in table[1:]:
            cells = [str(c or "").strip() for c in row]
            if len(cells) < 18:
                continue
            pos = cells[0]
            if pos not in SKILL_POSITIONS:
                continue
            name = cells[1]
            # Skip aggregate "QB Total" / "RB Total" / etc. rows -- they
            # share the leading position token but the player slot reads
            # "Total".
            if not name or name.lower() == "total":
                continue
            try:
                nums = [int(cells[i]) for i in range(2, 18)]
            except (ValueError, TypeError):
                # Non-numeric cell -- malformed row, skip it.
                continue
            gm, _att, _comp, p_yds, p_td, _int, _sk, \
                r_att, r_yds, r_td, \
                tgt, rec, rec_yd, rec_td, \
                pts, _rk = nums
            components = {
                "pass_yd": p_yds,
                "pass_td": p_td,
                "rush_att": r_att,
                "rush_yd": r_yds,
                "rush_td": r_td,
                "targets": tgt,
                "rec": rec,
                "rec_yd": rec_yd,
                "rec_td": rec_td,
            }
            proj_total = compute_proj_total(components)
            proj_ppg = round(proj_total / gm, 2) if gm > 0 else 0.0
            vor = round(proj_total - REPLACEMENT_LEVELS[pos], 1)
            players.append({
                "name": name,
                "team": abbr,
                "pos": pos,
                "games": gm,
                "proj_total": proj_total,
                "proj_ppg": proj_ppg,
                "vor": vor,
                "tier": assign_tier(vor, pos),
                "pos_rk": 0,              # half-PPR rank, filled after sorting
                "clay_pos_rk": 0,         # Clay's full-PPR rank, filled after sorting
                "clay_ppr_total": pts,
                "components": components,
            })
        # Found the offense table -- no need to keep scanning this page.
        break
    return players


def main() -> None:
    print("[build] starting Clay projections build")
    abbrs, name_to_abbr = load_team_keys()
    print(f"[build] loaded {len(abbrs)} canonical team abbreviations")

    response = requests.get(SOURCE_URL, timeout=60)
    response.raise_for_status()
    pdf_bytes = response.content
    print(f"[build] fetched {len(pdf_bytes):,} bytes from ESPN")

    all_players = []
    teams_seen = set()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            title_match = TITLE_RE.search(text)
            if not title_match:
                continue
            team_name = title_match.group(1).strip()
            if team_name not in name_to_abbr:
                # Title patterns also appear in TOC/header lines; only real
                # team pages resolve. Quiet-skip the rest.
                continue
            if team_name in teams_seen:
                continue
            teams_seen.add(team_name)
            abbr = name_to_abbr[team_name]
            page_players = parse_team_page(page, abbr)
            print(f"[build]   p.{page_num:>3} {team_name:<24} ({abbr}) "
                  f"-> {len(page_players)} players")
            all_players.extend(page_players)

    if len(teams_seen) != 32:
        missing = set(name_to_abbr) - teams_seen
        sys.exit(f"[build] only parsed {len(teams_seen)}/32 teams; "
                 f"missing: {sorted(missing)}")

    if len(all_players) < MIN_EXPECTED_PLAYERS:
        sys.exit(f"[build] parsed only {len(all_players)} players "
                 f"(expected >= {MIN_EXPECTED_PLAYERS}) -- aborting")

    bad_teams = {p["team"] for p in all_players if p["team"] not in abbrs}
    if bad_teams:
        sys.exit(f"[build] uncanonical team codes in output: {bad_teams}")

    # Two positional ranks per player:
    #   pos_rk      — by Underdog half-PPR proj_total (what the extension
    #                 cares about; matches vor/tier ordering).
    #   clay_pos_rk — by Clay's published full-PPR Pts (clay_ppr_total),
    #                 preserved for reference / matches the PDF's Rk column.
    # High-volume receivers will rank a notch lower in half PPR than in
    # full PPR; touchdown-heavy players a notch higher.
    by_pos = defaultdict(list)
    for p in all_players:
        by_pos[p["pos"]].append(p)
    for players in by_pos.values():
        players.sort(key=lambda r: r["proj_total"], reverse=True)
        for idx, p in enumerate(players, start=1):
            p["pos_rk"] = idx
        players.sort(key=lambda r: r["clay_ppr_total"], reverse=True)
        for idx, p in enumerate(players, start=1):
            p["clay_pos_rk"] = idx

    # Build output keyed by "Name|TEAM|POS", in QB/RB/WR/TE order.
    output_players = {}
    for pos in SKILL_POSITIONS:
        for p in by_pos[pos]:
            key = f"{p['name']}|{p['team']}|{p['pos']}"
            output_players[key] = p

    now = datetime.now(timezone.utc)
    payload = {
        "_meta": {
            "version": now.date().isoformat(),
            "generated_at": now.isoformat(timespec="microseconds"),
            "season": 2026,
            "sport": "NFL",
            "scoring": "underdog_half_ppr",
            "source": "mike_clay_espn",
            "source_url": SOURCE_URL,
            "replacement_levels": REPLACEMENT_LEVELS,
            "scoring_rules": SCORING_RULES,
            "player_count": len(output_players),
        },
        "players": output_players,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    print(f"[build] wrote {OUTPUT_FILE} -- {len(output_players)} players")


if __name__ == "__main__":
    main()
