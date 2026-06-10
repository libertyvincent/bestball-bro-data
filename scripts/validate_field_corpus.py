#!/usr/bin/env python3
"""Integrity check for the stripped field corpus.

Recomputes headline statistics from `sources/field/boards_2026-06-10.json`
(stripped) and asserts they reproduce hub's analysis of the RAW export within
tolerance -- proof the privacy strip preserved the analytical content. All
field statistics exclude the owner (is_owner) per the opponent-only convention.

Usage: python scripts/validate_field_corpus.py [boards.json]

Tolerances: means +/-0.02, sigma +/-0.1, shares +/-0.3pp.
"""
from __future__ import annotations
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

MEAN_TOL, SIGMA_TOL, SHARE_TOL = 0.02, 0.1, 0.3  # share tol in pp


def stdev(xs):
    n = len(xs)
    if n < 2:
        return float("nan")
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))  # sample sd


def slate_category(title: str) -> str:
    t = (title or "").lower()
    if "weekly winners" in t:
        return "WW"
    if "eliminator" in t:
        return "Elim"
    if "field general" in t:
        return "SF"
    return "Season"


def draft_obj(env):
    return env["raw_response"]["draft"]


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 \
        else Path("sources/field/boards_2026-06-10.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    drafts = data["drafts"]

    results = []  # (name, ok, detail)

    def check(name, ok, detail):
        results.append((name, ok, detail))

    # --- 1. structural ------------------------------------------------------
    check("draft count == 51", len(drafts) == 51, f"got {len(drafts)}")

    sizes = Counter(len(draft_obj(d)["picks"]) for d in drafts)
    check("boards: 50x216 + 1x240", sizes.get(216) == 50 and sizes.get(240) == 1,
          dict(sizes))

    cats = Counter(slate_category(draft_obj(d)["title"]) for d in drafts)
    want = {"WW": 16, "Season": 33, "Elim": 1, "SF": 1}
    check("slate split 16WW/33Season/1Elim/1SF", dict(cats) == want, dict(cats))

    owners = set()
    for d in drafts:
        for e in draft_obj(d)["draft_entries"]:
            if e.get("is_owner"):
                owners.add(e["user_id"])
    check("exactly one is_owner id", len(owners) == 1,
          f"{len(owners)} distinct: {list(owners)[:3]}")

    # owner entry id per draft (to exclude owner picks)
    def owner_entry_ids(d):
        return {e["id"] for e in draft_obj(d)["draft_entries"] if e.get("is_owner")}

    # --- 2. opponent pick-vs-ADP residual sigma by round (216-boards) -------
    # residual = projection_adp - overall pick number; round = ceil(number/12).
    by_round = defaultdict(list)
    for d in drafts:
        dr = draft_obj(d)
        if len(dr["picks"]) != 216:
            continue
        owner_ids = owner_entry_ids(d)
        for p in dr["picks"]:
            if p.get("draft_entry_id") in owner_ids:
                continue  # owner excluded
            adp, num = p.get("projection_adp"), p.get("number")
            if adp is None or num is None:
                continue
            try:
                adp_f = float(adp)  # some picks carry '-' (no ADP) -> skip
            except (TypeError, ValueError):
                continue
            rnd = math.ceil(int(num) / 12)
            by_round[rnd].append(adp_f - float(num))
    sig = {r: stdev(by_round[r]) for r in sorted(by_round)}

    def near(a, b, tol):
        return abs(a - b) <= tol

    check("residual sigma R1 ~ 2.3", near(sig.get(1, 0), 2.3, SIGMA_TOL),
          f"R1={sig.get(1):.3f}")
    check("residual sigma R6 ~ 5.2", near(sig.get(6, 0), 5.2, SIGMA_TOL),
          f"R6={sig.get(6):.3f}")
    # R12 ~ 10.1 (corrected from a loose 9.6 interpolation in the original
    # spec). The pooled R12 averages two genuinely different rooms -- Season
    # ~8.5 vs Weekly Winners ~12.8 -- so the ~10 figure is a count-weighted
    # blend, not a single-room value. See the slate-split line below and the
    # README; consumers refitting harness sigma should prefer slate-specific.
    check("residual sigma R12 ~ 10.1", near(sig.get(12, 0), 10.1, SIGMA_TOL),
          f"R12={sig.get(12):.3f}")
    plateau = [sig[r] for r in range(13, 19) if r in sig]
    pmean = sum(plateau) / len(plateau) if plateau else float("nan")
    check("plateau ~10 after R12", 9.3 <= pmean <= 10.7,
          f"mean(R13..R18)={pmean:.3f}")
    print("  sigma-by-round (pooled):", {r: round(s, 2) for r, s in sig.items()})

    # Slate-split structure: the pooled plateau ~10 blends two different
    # rooms. Report R12 by slate (216-boards, owner-excluded) so consumers see
    # it. Season is far chalkier than Weekly Winners at the turn.
    split = defaultdict(lambda: defaultdict(list))
    for d in drafts:
        dr = draft_obj(d)
        if len(dr["picks"]) != 216:
            continue
        cat = slate_category(dr["title"])
        owner_ids = owner_entry_ids(d)
        for p in dr["picks"]:
            if p.get("draft_entry_id") in owner_ids:
                continue
            try:
                a = float(p.get("projection_adp"))
            except (TypeError, ValueError):
                continue
            split[cat][math.ceil(int(p["number"]) / 12)].append(a - float(p["number"]))
    r12_split = {c: round(stdev(split[c][12]), 2) for c in ("Season", "WW") if split[c][12]}
    print(f"  R12 sigma by slate: {r12_split}  (pooled R12={sig.get(12):.2f})")

    # --- 3. Season opponent construction (complete 18-pick rosters) ---------
    # position join: pick.appearance_id -> player_id -> position_name, from the
    # kept reference payloads.
    appr_to_player, player_pos = {}, {}
    for env in data["unkeyed"]:
        ep = env.get("api_endpoint", "")
        rr = env.get("raw_response", {})
        if ep.endswith("/players"):
            for pl in rr.get("players", []):
                player_pos[pl["id"]] = pl.get("position_name")
        elif ep.endswith("/appearances"):
            for ap in rr.get("appearances", []):
                appr_to_player[ap["id"]] = ap.get("player_id")

    # Underdog lists Travis-Hunter-class players as position_name "CB"
    # (their defensive position); in fantasy they start at WR. This is the
    # same CB->WR remap the sim's player_match applies. 26 such picks here.
    def pos_of(appearance_id):
        pos = player_pos.get(appr_to_player.get(appearance_id))
        return "WR" if pos == "CB" else pos

    rosters = []  # list of Counter(position -> count) per opponent entry
    unmapped = 0
    for d in drafts:
        dr = draft_obj(d)
        if slate_category(dr["title"]) != "Season" or len(dr["picks"]) != 216:
            continue
        owner_ids = owner_entry_ids(d)
        by_entry = defaultdict(Counter)
        for p in dr["picks"]:
            eid = p.get("draft_entry_id")
            if eid in owner_ids:
                continue
            pos = pos_of(p.get("appearance_id"))
            if pos is None:
                unmapped += 1
                continue
            by_entry[eid][pos] += 1
        for eid, cnt in by_entry.items():
            if sum(cnt.values()) == 18:  # complete roster
                rosters.append(cnt)

    n = len(rosters)
    means = {pos: sum(r.get(pos, 0) for r in rosters) / n for pos in ("QB", "RB", "WR", "TE")}
    te4 = sum(1 for r in rosters if r.get("TE", 0) >= 4) / n * 100
    te4_rb4 = sum(1 for r in rosters if r.get("TE", 0) >= 4 and r.get("RB", 0) <= 4) / n * 100

    print(f"  construction n={n} opponent rosters; unmapped picks={unmapped}")
    want_means = {"QB": 2.58, "RB": 5.40, "WR": 7.32, "TE": 2.71}
    for pos in ("QB", "RB", "WR", "TE"):
        check(f"Season {pos} mean ~ {want_means[pos]}",
              near(means[pos], want_means[pos], MEAN_TOL), f"{means[pos]:.3f}")
    check("Season TE>=4 share ~ 3.9%", near(te4, 3.9, SHARE_TOL), f"{te4:.2f}%")
    check("Season TE>=4 & RB<=4 share ~ 1.1%", near(te4_rb4, 1.1, SHARE_TOL),
          f"{te4_rb4:.2f}%")

    # --- report -------------------------------------------------------------
    print("\n=== VALIDATION ===")
    npass = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
    print(f"\n{npass}/{len(results)} checks passed.")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    main()
