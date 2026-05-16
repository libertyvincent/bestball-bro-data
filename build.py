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

# Vertical tolerance (PDF points) for clustering words into the same logical
# row. Adjacent rows in Clay's tables are ~10pt apart, so 3pt is safe.
Y_TOL = 3.0

# Offense table column order (18 columns total):
#   Pos | Player | Gm
#       | Pass(Att Comp Yds TD INT Sk)
#       | Rush(Att Yds TD)
#       | Rec(Tgt Rec Yd TD)
#       | Pts | Rk
# The 16 numeric stat words follow Pos+Player; cells[2:18] are the stats.


def _cluster_into_rows(words):
    """Group extract_words() output into logical rows by y-coordinate."""
    rows = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(w["top"] - rows[-1][-1]["top"]) <= Y_TOL:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w["x0"]) for r in rows]


def _find_offense_header(rows):
    """Locate the Pos|Player|Gm|...|Rk header row of the offense table.
    Returns the list of 18 header word dicts (sorted left-to-right), or None.
    """
    for row in rows:
        for i in range(len(row) - 17):
            texts = [w["text"] for w in row[i:i + 18]]
            if (texts[0] == "Pos" and texts[1] == "Player"
                    and texts[2] == "Gm" and texts[-1] == "Rk"):
                return row[i:i + 18]
    return None


def parse_team_page(page, abbr: str) -> list:
    """Extract QB/RB/WR/TE rows by clustering extract_words() output into
    rows and columns.

    This is robust against PDFs whose tables aren't drawn with line objects
    (Clay's PDF uses background-shaded columns, which pdfplumber's table
    detection can't see). Algorithm:
      1. Pull every word on the page with (x0, top, x1).
      2. Cluster into logical rows by `top` within Y_TOL.
      3. Find the offense header row by signature ("Pos Player Gm ... Rk").
      4. Use the header words' x-positions to define the offense table's
         x-range. The Gm column's x-start separates the player-name words
         (to the left) from the 16 stat words (to the right).
      5. For each subsequent row whose first offense-range word is a skill
         position (QB/RB/WR/TE), assemble name + stats and emit a player.
    """
    try:
        words = page.extract_words(use_text_flow=False)
    except Exception:
        return []
    if not words:
        return []

    rows = _cluster_into_rows(words)
    header = _find_offense_header(rows)
    if not header:
        return []

    # x-boundaries of the offense table (with small tolerance pads).
    x_left = header[0]["x0"] - 5
    x_right = header[-1]["x1"] + 8
    # Words at or right of `gm_x_start` are stat columns (Gm and onward);
    # words to the left of it are the player-name fragments.
    gm_x_start = header[2]["x0"] - 2
    header_top = header[0]["top"]

    players = []
    for row in rows:
        # Skip rows at or above the header line.
        if row[0]["top"] <= header_top + Y_TOL:
            continue
        # Keep only words within the offense x-range.
        offense = [w for w in row if x_left <= w["x0"] <= x_right]
        if not offense:
            continue
        pos = offense[0]["text"]
        if pos not in SKILL_POSITIONS:
            continue
        # Split remaining words into name fragments vs stat numbers.
        name_parts, stat_parts = [], []
        for w in offense[1:]:
            (name_parts if w["x0"] < gm_x_start else stat_parts).append(w["text"])
        name = " ".join(name_parts).strip()
        if not name or name.lower() == "total":
            continue
        if len(stat_parts) < 16:
            continue
        try:
            nums = [int(s) for s in stat_parts[:16]]
        except ValueError:
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
