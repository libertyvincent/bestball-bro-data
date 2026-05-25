#!/usr/bin/env python3
"""
build.py — daily refresh of Mike Clay's NFL projections.

Two parallel outputs from a single PDF parse:

1. **Legacy feed** — `projections/nfl_2026.json` — half-PPR projections
   keyed by "Name|TEAM|POS" with VOR/tier. Consumed by the BestBall Bro
   Chrome extension; schema is locked for backwards compat.

2. **Source feeds** — `sources/clay_2026_offense.json`,
   `sources/clay_2026_weekly_team_scoring.json`,
   `sources/clay_2026_unit_grades.json` — three flat files representing
   Clay's full projection (offense projections + per-team-per-week
   projected NFL game scores + per-team unit grades). Consumed by the
   sim engine (`bestball-bro-sim`).

Pipeline:
  1. Download the Clay PDF from ESPN.
  2. Parse the 32 per-team offense tables (pages ~2-33). Same rows
     drive both the legacy and the offense source feeds.
  3. Parse the per-team weekly score-projection tables.
  4. Parse the per-team "Projected Wins" + "SOS Rank" cells.
  5. Parse page 63 — the all-32-teams unit-grades table.
  6. Resolve team names / codes to canonical NFL abbreviations using
     `teams/nfl_2026.json` (the team key) + the ESPN_TO_STANDARD map
     for anywhere the PDF uses Clay's variant codes (ARZ/BLT/CLV/HST/
     JAC/LA/SD/WSH). Normalization is applied to *every* team field
     across *every* output file.
  7. Compute VOR / tier / pos rank for the legacy feed.
  8. Validate (32-team / >=350-player / weekly-sum / mirror-row
     checks) and emit all four files. Build fails loudly if any
     validator trips, so a broken JSON is never committed.

Designed to run unattended in GitHub Actions.
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
SOURCES_DIR = ROOT / "sources"
SOURCE_OFFENSE_FILE = SOURCES_DIR / "clay_2026_offense.json"
SOURCE_WEEKLY_FILE = SOURCES_DIR / "clay_2026_weekly_team_scoring.json"
SOURCE_GRADES_FILE = SOURCES_DIR / "clay_2026_unit_grades.json"
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
    "pass_int": -1.0,
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rec": 0.5,
    "rec_yd": 0.1,
    "rec_td": 6.0,
}
# Underdog full-PPR scoring rules — the source rules Clay's PDF Pts
# column is computed against. The half-PPR projection we publish is
# derived from this by swapping reception 1.0 → 0.5. Interception is
# -1 (Underdog's published rule, not -2 as we previously assumed);
# fumble_lost (-2) and two_pt_conversion (+2) are listed for
# completeness but neither stat is exposed as a per-row column in the
# PDF, so the half-PPR reconstruction in compute_proj_total can't
# apply them — they live implicitly inside Clay's published Pts.
CLAY_NATIVE_SCORING = {
    "passing_yard": 0.04,
    "passing_td": 4,
    "interception": -1,
    "rushing_yard": 0.1,
    "rushing_td": 6,
    "reception": 1.0,
    "receiving_yard": 0.1,
    "receiving_td": 6,
    "fumble_lost": -2,
    "two_pt_conversion": 2,
}

# --- Team abbreviation normalization ------------------------------------
# ESPN's PDF uses a handful of nonstandard team codes (notably on the
# category leaderboard pages -- the per-team pages use full team names in
# titles, which we resolve via TEAMS_FILE). Applied consistently across
# every team field in every output file.
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
EXPECTED_TEAM_COUNT = 32

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
    """Fantasy points from component stats. Underdog half-PPR scoring.

    Applies the INT penalty (-1) — previous omission inflated QB
    projections by 15-20 points each. fumble_lost and two_pt_conversion
    aren't in the per-row stat extract, so they aren't applied here;
    they're already baked into Clay's Pts column on the offense feed."""
    return round(
        c["pass_yd"]  * SCORING_RULES["pass_yd"]
        + c["pass_td"]  * SCORING_RULES["pass_td"]
        + c["pass_int"] * SCORING_RULES["pass_int"]
        + c["rush_yd"]  * SCORING_RULES["rush_yd"]
        + c["rush_td"]  * SCORING_RULES["rush_td"]
        + c["rec"]      * SCORING_RULES["rec"]
        + c["rec_yd"]   * SCORING_RULES["rec_yd"]
        + c["rec_td"]   * SCORING_RULES["rec_td"],
        1,
    )


def normalize_team(raw: str) -> str:
    """Map ESPN-style team codes to canonical NFL abbreviations.

    Used everywhere a team-code string crosses an IO boundary — output
    fields, opponent keys in the weekly scoring file, the unit-grades
    teams map. Unknown codes pass through (the caller's safety floor
    will fail loudly if any reach the output)."""
    code = (raw or "").upper().strip()
    return ESPN_TO_STANDARD.get(code, code)


def load_team_keys():
    """Canonical abbreviations and a 'Team Name -> ABBR' map from
    nfl_2026.json -- our source of truth for the 32 NFL franchises."""
    with open(TEAMS_FILE, encoding="utf-8") as fh:
        data = json.load(fh)
    teams = data["teams"]
    if len(teams) != EXPECTED_TEAM_COUNT:
        sys.exit(f"[build] {TEAMS_FILE} has {len(teams)} teams, "
                 f"expected {EXPECTED_TEAM_COUNT}")
    abbrs = {t["abbr"] for t in teams.values()}
    name_to_abbr = {t["name"]: t["abbr"] for t in teams.values()}
    return abbrs, name_to_abbr


# --- PDF parsing --------------------------------------------------------
# Match a per-team page title (e.g. "2026 Cleveland Browns Projections").
TITLE_RE = re.compile(r"2026\s+([A-Za-z][A-Za-z0-9'\.\s]+?)\s+Projections\b")

# Match an offense row line on a per-team page:
#   <POS> <player name (multi-word)> <16 numeric stats>
# Player names may contain spaces, periods (C.J., Jr.), apostrophes
# (Ja'Marr, De'Von), hyphens (Smith-Njigba), and Roman-numeral suffixes
# (III). The 16 trailing integers are the stat columns in fixed order:
#   Gm | Pass(Att Comp Yds TD INT Sk) | Rush(Att Yds TD)
#      | Rec(Tgt Rec Yd TD) | Pts | Rk
# No end anchor: pdfplumber concatenates the offense, defense, and
# weekly-score rows into a single line per row, so anything after the
# 16th stat is harmlessly ignored.
OFFENSE_ROW_RE = re.compile(
    r"^(?P<pos>QB|RB|WR|TE)\s+"
    r"(?P<name>[A-Za-z][A-Za-z'\.\-\s]+?)"
    r"\s+(?P<stats>\d+(?:\s+\d+){15})(?!\d)"
)

# Weekly-score-projection rows live at the END of each concatenated line
# (offense + defense + weekly trail, all merged by pdfplumber). We use
# re.search with end-of-line anchoring and a leading (?:^|\s) so we
# don't false-positive on numbers buried in the offense/defense stats.
#
# Three line shapes:
#   1. Standard week:  "... 1 LAC V 17.2 29.1 13%"
#   2. Bye week:       "... 14 0.0 0.0"
#   3. Season total:   "... Total 306 465 21%"
WEEKLY_SCORE_RE = re.compile(
    r"(?:^|\s)(?P<wk>\d{1,2})\s+"
    r"@?(?P<opp>[A-Z]{2,3})\s+"
    r"(?P<loc>[VH])\s+"
    r"(?P<tm_score>\d+(?:\.\d+)?)\s+"
    r"(?P<opp_score>\d+(?:\.\d+)?)\s+"
    r"(?P<win_prob>\d{1,3})%\s*$"
)
WEEKLY_BYE_RE = re.compile(
    r"(?:^|\s)(?P<wk>\d{1,2})\s+0(?:\.0+)?\s+0(?:\.0+)?\s*$"
)
WEEKLY_TOTAL_RE = re.compile(
    r"(?:^|\s)Total\s+(?P<tm_total>\d+)\s+(?P<opp_total>\d+)\s+"
    r"(?P<win_pct>\d{1,3})%\s*$",
    re.I,
)

# Per-team season totals + projected wins/SOS, all parsed from free-form
# label/value pairs on the same page. These regexes are lenient — the
# PDF's exact whitespace varies row-to-row.
SEASON_POINTS_FOR_RE = re.compile(
    r"(?:Total\s+)?Points\s*(?:For|Scored)\D+(\d+(?:\.\d+)?)", re.I)
SEASON_POINTS_AGAINST_RE = re.compile(
    r"Points\s*Against\D+(\d+(?:\.\d+)?)", re.I)
PROJECTED_WINS_RE = re.compile(
    r"Projected\s*Wins?\s*[:\s]\s*(\d+(?:\.\d+)?)(?:\s*\(\s*(\d+)\s*\))?",
    re.I)
SOS_RANK_RE = re.compile(
    r"(?:Strength\s*of\s*Schedule|SOS)\s*(?:Rank|Rk)?\D*?(\d+)", re.I)
PDF_UPDATED_RE = re.compile(
    r"Updated:\s*(\d{1,2})/(\d{1,2})/(\d{4})")

# Page-63 unit grades: <team name> + 10 integer position grades +
# 6 aggregate cells (off_gr off_rk def_gr def_rk tot_gr tot_rk).
# Aggregate grades are floats; ranks are 1-32 integers.
UNIT_GRADE_RE = re.compile(
    r"^(?P<team>[A-Za-z][A-Za-z0-9'\.\s]+?)\s+"
    r"(?P<grades>\d+(?:\s+\d+){9})\s+"
    r"(?P<off_gr>\d+(?:\.\d+)?)\s+(?P<off_rk>\d+)\s+"
    r"(?P<def_gr>\d+(?:\.\d+)?)\s+(?P<def_rk>\d+)\s+"
    r"(?P<tot_gr>\d+(?:\.\d+)?)\s+(?P<tot_rk>\d+)\s*$"
)


def parse_pdf_updated(text: str) -> str | None:
    """Extract the 'Updated: M/D/YYYY' string from page 1; ISO-format it."""
    m = PDF_UPDATED_RE.search(text)
    if not m:
        return None
    month, day, year = (int(g) for g in m.groups())
    try:
        return f"{year:04d}-{month:02d}-{day:02d}"
    except ValueError:
        return None


def parse_offense_rows(text: str) -> list:
    """Extract offensive players from one team page.

    Returns a list of dicts with both the legacy-feed fields and the
    full stat panel needed for the offense source feed. Team and rank
    are filled in later by the caller (after we know the team abbr and
    have sorted globally).
    """
    players = []
    for line in text.splitlines():
        line = line.strip()
        m = OFFENSE_ROW_RE.match(line)
        if not m:
            continue
        pos = m.group("pos")
        name = m.group("name").strip()
        # Filter aggregate "Total" rows -- they share the leading position
        # token (e.g. "QB Total 34 549 ...") but the name slot reads "Total".
        if name.lower() == "total":
            continue
        nums = [int(x) for x in m.group("stats").split()]
        gm, p_att, p_comp, p_yds, p_td, p_int, _p_sk, \
            r_att, r_yds, r_td, \
            tgt, rec, rec_yd, rec_td, \
            pts, _rk = nums
        # Legacy-feed components (half-PPR pipeline). pass_int is
        # included so compute_proj_total can apply the -1/INT penalty
        # downstream; previous schema omitted it and that's what
        # inflated QB proj_totals.
        components = {
            "pass_yd": p_yds, "pass_td": p_td, "pass_int": p_int,
            "rush_att": r_att, "rush_yd": r_yds, "rush_td": r_td,
            "targets": tgt, "rec": rec, "rec_yd": rec_yd, "rec_td": rec_td,
        }
        proj_total = compute_proj_total(components)
        proj_ppg = round(proj_total / gm, 2) if gm > 0 else 0.0
        vor = round(proj_total - REPLACEMENT_LEVELS[pos], 1)
        players.append({
            "name": name,
            "pos": pos,
            "games": gm,
            "proj_total": proj_total,
            "proj_ppg": proj_ppg,
            "vor": vor,
            "tier": assign_tier(vor, pos),
            "pos_rk": 0,                 # half-PPR rank, filled after sorting
            "clay_pos_rk": 0,            # Clay's full-PPR rank, filled after sorting
            "clay_ppr_total": pts,
            "components": components,
            # Full stat panel for the offense source feed:
            "passing_attempts":    p_att,
            "passing_completions": p_comp,
            "passing_yards":       p_yds,
            "passing_tds":         p_td,
            "passing_ints":        p_int,
            "rushing_attempts":    r_att,
            "rushing_yards":       r_yds,
            "rushing_tds":         r_td,
            "targets":             tgt,
            "receptions":          rec,
            "receiving_yards":     rec_yd,
            "receiving_tds":       rec_td,
        })
    return players


def parse_weekly_scores(text: str, abbrs: set) -> list:
    """Parse the 18-row Weekly Score Projections table for one team.

    Returns a list of 18 dicts (one per week, including the bye).
    `opponent` is normalized to a canonical abbr; `location` is "V"/"H";
    `win_prob` is a decimal in [0,1] (10% → 0.10); bye rows have null
    opponent/location/win_prob and 0.0 scores."""
    weeks: dict[int, dict] = {}
    for line in text.splitlines():
        line = line.rstrip()
        m = WEEKLY_SCORE_RE.search(line)
        if m:
            wk = int(m.group("wk"))
            if 1 <= wk <= 18 and wk not in weeks:
                opp = normalize_team(m.group("opp"))
                if opp in abbrs:
                    weeks[wk] = {
                        "week":               wk,
                        "opponent":           opp,
                        "location":           m.group("loc"),
                        "team_nfl_score":     float(m.group("tm_score")),
                        "opponent_nfl_score": float(m.group("opp_score")),
                        "win_prob":           int(m.group("win_prob")) / 100.0,
                        "is_bye":             False,
                    }
                    continue
        m = WEEKLY_BYE_RE.search(line)
        if m:
            wk = int(m.group("wk"))
            if 1 <= wk <= 18 and wk not in weeks:
                weeks[wk] = {
                    "week":               wk,
                    "opponent":           None,
                    "location":           None,
                    "team_nfl_score":     0.0,
                    "opponent_nfl_score": 0.0,
                    "win_prob":           None,
                    "is_bye":             True,
                }
    return [weeks[w] for w in sorted(weeks)]


def parse_team_meta(text: str) -> dict:
    """Pull projected_wins/projected_wins_rank/sos_rank + season point
    totals out of a per-team page's text. Lenient — returns Nones for
    fields the PDF doesn't expose in a regex-friendly form."""
    out = {
        "season_projected_nfl_points_for":     None,
        "season_projected_nfl_points_against": None,
        "projected_wins":                      None,
        "projected_wins_rank":                 None,
        "strength_of_schedule_rank":           None,
    }
    m = SEASON_POINTS_FOR_RE.search(text)
    if m:
        out["season_projected_nfl_points_for"] = float(m.group(1))
    m = SEASON_POINTS_AGAINST_RE.search(text)
    if m:
        out["season_projected_nfl_points_against"] = float(m.group(1))
    m = PROJECTED_WINS_RE.search(text)
    if m:
        out["projected_wins"] = float(m.group(1))
        if m.group(2):
            out["projected_wins_rank"] = int(m.group(2))
    m = SOS_RANK_RE.search(text)
    if m:
        out["strength_of_schedule_rank"] = int(m.group(1))
    return out


def parse_unit_grades_page(text: str, name_to_abbr: dict) -> dict:
    """Parse page 63 — the all-32-teams unit-grades table.

    Returns `{abbr: {offense:{...}, defense:{...}, aggregate:{...}}}`.
    Per-position grades (QB/RB/WR/TE/OL/DI/ED/LB/CB/S) are integers
    1-10. Aggregate grades are floats; ranks are 1-32 integers.
    """
    out: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        m = UNIT_GRADE_RE.match(line)
        if not m:
            continue
        team_name = m.group("team").strip()
        # Discriminate genuine team rows from the header row and the
        # 'Avg/Total' summary footer Clay sometimes appends.
        if team_name not in name_to_abbr:
            continue
        abbr = name_to_abbr[team_name]
        grades = [int(x) for x in m.group("grades").split()]
        if len(grades) != 10:
            continue
        qb, rb, wr, te, ol, di, ed, lb, cb, s = grades
        out[abbr] = {
            "offense": {"qb": qb, "rb": rb, "wr": wr, "te": te, "ol": ol},
            "defense": {"di": di, "ed": ed, "lb": lb, "cb": cb, "s": s},
            "aggregate": {
                "offense_grade": float(m.group("off_gr")),
                "offense_rank":  int(m.group("off_rk")),
                "defense_grade": float(m.group("def_gr")),
                "defense_rank":  int(m.group("def_rk")),
                "overall_grade": float(m.group("tot_gr")),
                "overall_rank":  int(m.group("tot_rk")),
            },
        }
    return out


# --- Validators ---------------------------------------------------------
def _close(a: float, b: float, tol: float = 0.5) -> bool:
    return abs(a - b) <= tol


def validate_weekly(teams_weekly: dict) -> None:
    """Per-team and cross-team consistency checks. Fatal mismatches
    exit; small (<1pt) drift gets a warning."""
    if len(teams_weekly) != EXPECTED_TEAM_COUNT:
        sys.exit(f"[build] weekly: {len(teams_weekly)} teams, "
                 f"expected {EXPECTED_TEAM_COUNT}")

    for abbr, data in teams_weekly.items():
        weeks = data["weeks"]
        if len(weeks) != 18:
            sys.exit(f"[build] weekly: {abbr} has {len(weeks)} week-rows, "
                     f"expected 18")
        bye_count = sum(1 for w in weeks if w["is_bye"])
        if bye_count != 1:
            sys.exit(f"[build] weekly: {abbr} has {bye_count} bye weeks, "
                     f"expected 1")
        # Season-total reconciliation (only if we parsed totals — the
        # season-points regex is guessed from spec, not validated; if
        # it grabs the wrong value we warn rather than fail the build).
        non_bye = [w for w in weeks if not w["is_bye"]]
        tm_sum  = sum(w["team_nfl_score"]     for w in non_bye)
        opp_sum = sum(w["opponent_nfl_score"] for w in non_bye)
        pf = data.get("season_projected_nfl_points_for")
        pa = data.get("season_projected_nfl_points_against")
        if pf is not None and not _close(tm_sum, pf):
            print(f"[build] WARN: {abbr} team-score sum {tm_sum:.1f} "
                  f"!= season for {pf:.1f} (>0.5 drift)")
        if pa is not None and not _close(opp_sum, pa):
            print(f"[build] WARN: {abbr} opp-score sum {opp_sum:.1f} "
                  f"!= season against {pa:.1f} (>0.5 drift)")

    # Cross-team consistency: A's week vs B should mirror B's week vs A.
    # Drift >1pt is logged as a warning (not fatal — PDF rounding can
    # produce sub-point asymmetries).
    for abbr, data in teams_weekly.items():
        for w in data["weeks"]:
            if w["is_bye"] or w["opponent"] not in teams_weekly:
                continue
            opp = w["opponent"]
            mirror = next((x for x in teams_weekly[opp]["weeks"]
                           if x["week"] == w["week"]), None)
            if mirror is None or mirror["is_bye"]:
                continue
            if mirror["opponent"] != abbr:
                print(f"[build] WARN: {abbr} wk{w['week']} opp={opp} but "
                      f"{opp} wk{w['week']} opp={mirror['opponent']}")
                continue
            if abs(w["team_nfl_score"] - mirror["opponent_nfl_score"]) > 1.0:
                print(f"[build] WARN: {abbr} wk{w['week']} team={w['team_nfl_score']} "
                      f"vs {opp} opp={mirror['opponent_nfl_score']} (>1pt drift)")
            if abs(w["opponent_nfl_score"] - mirror["team_nfl_score"]) > 1.0:
                print(f"[build] WARN: {abbr} wk{w['week']} opp={w['opponent_nfl_score']} "
                      f"vs {opp} team={mirror['team_nfl_score']} (>1pt drift)")
            if w["location"] and mirror["location"] and \
                    w["location"] == mirror["location"]:
                print(f"[build] WARN: {abbr} wk{w['week']} loc={w['location']} "
                      f"same as {opp} loc={mirror['location']}")


def validate_unit_grades(grades: dict) -> None:
    if len(grades) != EXPECTED_TEAM_COUNT:
        sys.exit(f"[build] unit grades: {len(grades)} teams, "
                 f"expected {EXPECTED_TEAM_COUNT}")
    for abbr, g in grades.items():
        for unit, vals in (("offense", g["offense"]), ("defense", g["defense"])):
            for k, v in vals.items():
                if not isinstance(v, int):
                    sys.exit(f"[build] unit grades: {abbr}.{unit}.{k} "
                             f"is {type(v).__name__}, expected int")
        agg = g["aggregate"]
        for k in ("offense_grade", "defense_grade", "overall_grade"):
            if not isinstance(agg[k], float):
                sys.exit(f"[build] unit grades: {abbr}.aggregate.{k} "
                         f"is {type(agg[k]).__name__}, expected float")


def validate_offense_vs_legacy(offense_players: list,
                                legacy_players: dict) -> None:
    """Cross-feed reconciliation: legacy proj_total vs offense
    projected_points_half_ppr for the same player.

    These won't be exactly equal. compute_proj_total applies the
    INT penalty (-1) but can't apply fumble_lost (-2) or
    two_pt_conversion (+2) because neither stat is exposed as a
    per-row column in the PDF — both are implicit in Clay's Pts.
    Expected residual drift is small (a few points; fumble counts
    are low). Anything past 25pt indicates a real methodology bug
    — fail loudly so we don't ship corrupted output.
    """
    legacy_by_key = {f"{p['name']}|{p['team']}|{p['pos']}": p
                     for p in legacy_players.values()}
    drift = []      # small/expected
    suspect = []    # too large to be int/fumble delta alone
    for op in offense_players:
        key = f"{op['name']}|{op['team']}|{op['position']}"
        lp = legacy_by_key.get(key)
        if lp is None:
            continue
        delta = lp["proj_total"] - op["projected_points_half_ppr"]
        if abs(delta) > 25.0:
            suspect.append((key, op["projected_points_half_ppr"],
                            lp["proj_total"], delta))
        elif abs(delta) > 0.5:
            drift.append(delta)
    if suspect:
        for k, a, b, d in suspect[:10]:
            print(f"[build] ERROR half-PPR drift >25pt: {k} "
                  f"offense={a} legacy={b} delta={d:+.1f}")
        if len(suspect) > 10:
            print(f"[build]   ... and {len(suspect) - 10} more")
        sys.exit(f"[build] {len(suspect)} player(s) exceeded the "
                 f"25pt cross-feed drift threshold")
    if drift:
        print(f"[build] half-PPR cross-feed: {len(drift)} players drifted "
              f"0.5-25pt vs legacy (expected — fumble residual)")


# --- Feed assembly ------------------------------------------------------
def build_offense_feed(offense_players: list, pdf_updated: str | None,
                       fetched_at: str) -> dict:
    """Order: rank_overall ascending."""
    offense_players = sorted(offense_players, key=lambda p: p["rank_overall"])
    return {
        "_meta": {
            "source_id": "mike_clay_offense",
            "source_name": "Mike Clay - ESPN 2026 NFL Projection Guide",
            "source_url": SOURCE_URL,
            "pdf_updated": pdf_updated,
            "fetched_at": fetched_at,
            "format": "ranking_with_projection_full_stats",
            "scoring": "native_ppr",
            "scoring_components": CLAY_NATIVE_SCORING,
            "player_count": len(offense_players),
        },
        "players": offense_players,
    }


def build_weekly_feed(teams_weekly: dict, pdf_updated: str | None,
                      fetched_at: str) -> dict:
    return {
        "_meta": {
            "source_id": "mike_clay_weekly_team_scoring",
            "source_name": "Mike Clay - ESPN 2026 NFL Projection Guide "
                           "(weekly team scoring)",
            "source_url": SOURCE_URL,
            "pdf_updated": pdf_updated,
            "fetched_at": fetched_at,
            "format": "per_team_weekly_score_projections",
            "scoring_unit": "nfl_game_points",
            "scoring_description":
                "These are projected NFL game scoring (touchdowns + "
                "field goals + safeties + 2-pt conversions), not "
                "fantasy points. Use as a relative per-week multiplier "
                "vs the team's season average to derive opponent-"
                "adjustment factors for fantasy projections.",
            "team_count": len(teams_weekly),
        },
        "teams": {abbr: teams_weekly[abbr] for abbr in sorted(teams_weekly)},
    }


def build_grades_feed(grades: dict, team_meta: dict,
                      pdf_updated: str | None, fetched_at: str) -> dict:
    teams_out = {}
    for abbr in sorted(grades):
        entry = dict(grades[abbr])
        m = team_meta.get(abbr, {})
        entry["projected_wins"]            = m.get("projected_wins")
        entry["projected_wins_rank"]       = m.get("projected_wins_rank")
        entry["strength_of_schedule_rank"] = m.get("strength_of_schedule_rank")
        teams_out[abbr] = entry
    return {
        "_meta": {
            "source_id": "mike_clay_unit_grades",
            "source_name": "Mike Clay - ESPN 2026 NFL Projection Guide "
                           "(unit grades)",
            "source_url": SOURCE_URL,
            "pdf_updated": pdf_updated,
            "fetched_at": fetched_at,
            "format": "per_team_unit_grades",
            "scale": "1_to_10_integer_higher_is_better",
            "defensive_group_labels": {
                "di": "interior defensive line",
                "ed": "edge rusher",
                "lb": "off-ball linebacker",
                "cb": "cornerback",
                "s":  "safety",
            },
            "team_count": len(teams_out),
        },
        "teams": teams_out,
    }


def main() -> None:
    print("[build] starting Clay projections build")
    abbrs, name_to_abbr = load_team_keys()
    print(f"[build] loaded {len(abbrs)} canonical team abbreviations")

    response = requests.get(SOURCE_URL, timeout=60)
    response.raise_for_status()
    pdf_bytes = response.content
    print(f"[build] fetched {len(pdf_bytes):,} bytes from ESPN")

    all_players = []                # legacy + offense source rows
    teams_weekly: dict[str, dict] = {}
    team_meta: dict[str, dict] = {}
    teams_seen = set()
    pdf_updated: str | None = None
    grades_parsed: dict = {}        # filled when we find the page

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if pdf_updated is None:
                pdf_updated = parse_pdf_updated(text)

            title_match = TITLE_RE.search(text)

            if title_match:
                team_name = title_match.group(1).strip()
                if team_name in name_to_abbr and team_name not in teams_seen:
                    teams_seen.add(team_name)
                    abbr = name_to_abbr[team_name]

                    page_players = parse_offense_rows(text)
                    for p in page_players:
                        p["team"] = abbr
                    all_players.extend(page_players)

                    weeks = parse_weekly_scores(text, abbrs)
                    meta  = parse_team_meta(text)
                    team_meta[abbr] = meta
                    if weeks:
                        teams_weekly[abbr] = {
                            "season_projected_nfl_points_for":
                                meta.get("season_projected_nfl_points_for"),
                            "season_projected_nfl_points_against":
                                meta.get("season_projected_nfl_points_against"),
                            "weeks": weeks,
                        }
                    print(f"[build]   p.{page_num:>3} {team_name:<24} ({abbr}) "
                          f"-> {len(page_players)} players, "
                          f"{len(weeks)} weeks")

            # Unit-grades scan: run UNIT_GRADE_RE against every page;
            # the page whose rows resolve to the most canonical NFL
            # abbrs wins. No header-substring pre-filter — a sniff on
            # "Off Gr"/"Def Gr" used to live here and silently locked
            # us out of page 63 because extract_text() doesn't render
            # those labels as contiguous substrings.
            candidate = parse_unit_grades_page(text, name_to_abbr)
            if len(candidate) > len(grades_parsed):
                grades_parsed = candidate

    if len(teams_seen) != EXPECTED_TEAM_COUNT:
        missing = set(name_to_abbr) - teams_seen
        sys.exit(f"[build] only parsed {len(teams_seen)}/{EXPECTED_TEAM_COUNT} "
                 f"teams; missing: {sorted(missing)}")

    if len(all_players) < MIN_EXPECTED_PLAYERS:
        sys.exit(f"[build] parsed only {len(all_players)} players "
                 f"(expected >= {MIN_EXPECTED_PLAYERS}) -- aborting")

    bad_teams = {p["team"] for p in all_players if p["team"] not in abbrs}
    if bad_teams:
        sys.exit(f"[build] uncanonical team codes in output: {bad_teams}")

    # Two positional ranks per player (legacy feed):
    #   pos_rk      — by Underdog half-PPR proj_total (what the extension
    #                 cares about; matches vor/tier ordering).
    #   clay_pos_rk — by Clay's published full-PPR Pts (clay_ppr_total),
    #                 preserved for reference / matches the PDF's Rk column.
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

    # --- Legacy feed (projections/nfl_2026.json) -------------------------
    output_players = {}
    for pos in SKILL_POSITIONS:
        for p in by_pos[pos]:
            key = f"{p['name']}|{p['team']}|{p['pos']}"
            output_players[key] = {
                "name": p["name"], "team": p["team"], "pos": p["pos"],
                "games": p["games"], "proj_total": p["proj_total"],
                "proj_ppg": p["proj_ppg"], "vor": p["vor"],
                "tier": p["tier"], "pos_rk": p["pos_rk"],
                "clay_pos_rk": p["clay_pos_rk"],
                "clay_ppr_total": p["clay_ppr_total"],
                "components": p["components"],
            }

    now = datetime.now(timezone.utc)
    fetched_at_iso = now.isoformat(timespec="microseconds")
    fetched_at_z   = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    legacy_payload = {
        "_meta": {
            "version": now.date().isoformat(),
            "generated_at": fetched_at_iso,
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
        json.dump(legacy_payload, fh, indent=2, ensure_ascii=False)
    print(f"[build] wrote {OUTPUT_FILE} -- {len(output_players)} players")

    # --- Offense source feed (sources/clay_2026_offense.json) ----------
    # rank_overall is across-all-positions by full-PPR; rank_position is
    # within-position by full-PPR. Half-PPR is full-PPR minus 0.5/rec.
    offense_players = []
    for p in all_players:
        offense_players.append({
            "name":     p["name"],
            "team":     p["team"],
            "position": p["pos"],
            "rank_overall":  0,   # filled below
            "rank_position": p["clay_pos_rk"],
            "projected_points_full_ppr":
                round(float(p["clay_ppr_total"]), 1),
            "projected_points_half_ppr":
                round(float(p["clay_ppr_total"]) - 0.5 * p["receptions"], 1),
            "games":                p["games"],
            "passing_attempts":     p["passing_attempts"],
            "passing_completions":  p["passing_completions"],
            "passing_yards":        p["passing_yards"],
            "passing_tds":          p["passing_tds"],
            "passing_ints":         p["passing_ints"],
            "rushing_attempts":     p["rushing_attempts"],
            "rushing_yards":        p["rushing_yards"],
            "rushing_tds":          p["rushing_tds"],
            "targets":              p["targets"],
            "receptions":           p["receptions"],
            "receiving_yards":      p["receiving_yards"],
            "receiving_tds":        p["receiving_tds"],
        })
    offense_players.sort(
        key=lambda r: r["projected_points_full_ppr"], reverse=True)
    for idx, op in enumerate(offense_players, start=1):
        op["rank_overall"] = idx

    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    offense_feed = build_offense_feed(
        offense_players, pdf_updated, fetched_at_z)
    with open(SOURCE_OFFENSE_FILE, "w", encoding="utf-8") as fh:
        json.dump(offense_feed, fh, indent=2, ensure_ascii=False)
    print(f"[build] wrote {SOURCE_OFFENSE_FILE} -- "
          f"{len(offense_players)} players")

    # --- Weekly team scoring source feed -------------------------------
    validate_weekly(teams_weekly)
    weekly_feed = build_weekly_feed(teams_weekly, pdf_updated, fetched_at_z)
    with open(SOURCE_WEEKLY_FILE, "w", encoding="utf-8") as fh:
        json.dump(weekly_feed, fh, indent=2, ensure_ascii=False)
    print(f"[build] wrote {SOURCE_WEEKLY_FILE} -- "
          f"{len(teams_weekly)} teams x 18 weeks")

    # --- Unit grades source feed ---------------------------------------
    if not grades_parsed:
        sys.exit("[build] unit grades: no page parsed cleanly to 32-team table")
    validate_unit_grades(grades_parsed)
    grades_feed = build_grades_feed(
        grades_parsed, team_meta, pdf_updated, fetched_at_z)
    with open(SOURCE_GRADES_FILE, "w", encoding="utf-8") as fh:
        json.dump(grades_feed, fh, indent=2, ensure_ascii=False)
    print(f"[build] wrote {SOURCE_GRADES_FILE} -- "
          f"{len(grades_parsed)} teams")

    # --- Cross-feed sanity check (half-PPR consistency) ----------------
    validate_offense_vs_legacy(offense_players, output_players)

    print("[build] done — 4 files emitted (1 legacy + 3 sources)")


if __name__ == "__main__":
    main()
