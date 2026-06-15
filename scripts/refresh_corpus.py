#!/usr/bin/env python3
"""refresh_corpus.py -- prepare a field-corpus refresh, then STOP.

Wraps the deterministic middle of the corpus-refresh chain into one command:
select newest export -> privacy-strip -> validate -> privacy grep-gate ->
re-derive the sim-side drafter table -> stage on a branch -> print a report.

It deliberately does NOT do the three human-judgment steps:
  1. git push / open a PR        (publishing scraped opponent data is a release
                                  decision a human makes after reviewing the diff)
  2. re-derive / install the EXTENSION drafter table (moving the benchmark that
                                  validation runs measure against is human-only;
                                  this script regenerates only the SIM-side table
                                  and FLAGS the extension table for the human)
  3. delete the raw export       (irreversible local-PII deletion -- human-only)

Usage:
    python scripts/refresh_corpus.py            # real run: strip+stage on a branch
    python scripts/refresh_corpus.py --check     # reproduction/gate: strip to a
                                                 # scratch dir, diff against the
                                                 # committed boards_<date>.json,
                                                 # validate+derive+report, stage
                                                 # NOTHING. Use to trust the
                                                 # automation against a known-good
                                                 # manual run before relying on it.

Explicit invocation only -- there is no watch-folder daemon (a background process
re-deriving the benchmark on file-drop is exactly the silent state change we
avoid). Double-click `refresh_corpus.bat` to run on Windows.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# --- locations (resolved from this file; cwd-independent) --------------------
DATA_REPO = Path(__file__).resolve().parent.parent          # bestball-bro-data
SIM_REPO = DATA_REPO.parent / "bestball-bro-sim"            # sibling sim repo
FIELD_DIR = DATA_REPO / "sources" / "field"
STRIP = DATA_REPO / "scripts" / "strip_field_export.py"
VALIDATE = DATA_REPO / "scripts" / "validate_field_corpus.py"
DERIVE = SIM_REPO / "inst" / "scripts" / "derive_drafter_table.R"
EXPORT_DIR = Path(r"C:\Users\vince\Desktop\udbb-scraper")

# --- validator check classification (STRUCTURAL, not a per-snapshot allowlist)
# Snapshot-dependent checks drift with the corpus's size / slate mix and are
# REPORTED, never fatal. Everything else is substantive: a single substantive
# FAIL aborts before any staging. Matching on the check's semantic category
# (not its pinned number) means a real data regression next snapshot can't hide
# among the expected pin drifts.
SNAPSHOT_DEPENDENT = [
    re.compile(r"^draft count"),     # corpus size pin
    re.compile(r"^boards:"),         # board-size mix pin
    re.compile(r"^slate split"),     # slate-split pin
    re.compile(r"sigma R12"),        # POOLED R12 sigma: a Season+WW blend, so it
                                     # moves with the slate mix (the validator's
                                     # own comment flags this); slate-specific
                                     # R12 is stable and reported separately.
]


def is_snapshot_dependent(check_name: str) -> bool:
    return any(p.search(check_name) for p in SNAPSHOT_DEPENDENT)


# --- small helpers ----------------------------------------------------------
class Abort(Exception):
    """Clean, message-carrying abort. exit(1) unless `benign`."""

    def __init__(self, msg: str, benign: bool = False):
        super().__init__(msg)
        self.benign = benign


def run(cmd, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(c) for c in cmd], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8",
    )


def find_rscript() -> str:
    import os
    cands = [
        os.environ.get("RSCRIPT"),
        shutil.which("Rscript"), shutil.which("Rscript.exe"),
        r"C:\Program Files\R\R-4.6.0\bin\Rscript.exe",
    ]
    for c in cands:
        if c and Path(c).exists():
            return c
    raise Abort("Rscript not found. Set $RSCRIPT or install R 4.x "
                "(looked on PATH and at the R-4.6.0 default).")


DATE_RE = re.compile(r"udbb-scraper-(\d{4}-\d{2}-\d{2})T")
BOARDS_RE = re.compile(r"boards_(\d{4}-\d{2}-\d{2})\.json$")


def newest_export() -> tuple[Path, str, list[Path]]:
    """Newest udbb-scraper-*.json by ISO stamp; older ones are subsets."""
    if not EXPORT_DIR.is_dir():
        raise Abort(f"Export folder not found: {EXPORT_DIR}")
    exports = sorted(EXPORT_DIR.glob("udbb-scraper-*.json"))  # ISO sorts in time
    exports = [e for e in exports if DATE_RE.search(e.name)]
    if not exports:
        raise Abort(f"No udbb-scraper-*.json in {EXPORT_DIR}.")
    newest = exports[-1]
    date = DATE_RE.search(newest.name).group(1)
    skipped = exports[:-1]
    return newest, date, skipped


def newest_committed_boards() -> tuple[Path | None, str | None]:
    boards = sorted(p for p in FIELD_DIR.glob("boards_*.json") if BOARDS_RE.search(p.name))
    if not boards:
        return None, None
    newest = boards[-1]
    return newest, BOARDS_RE.search(newest.name).group(1)


def previous_boards(exclude_date: str) -> Path | None:
    boards = sorted(p for p in FIELD_DIR.glob("boards_*.json")
                    if BOARDS_RE.search(p.name) and BOARDS_RE.search(p.name).group(1) != exclude_date)
    return boards[-1] if boards else None


# --- draft-id extraction for the superset/dedup guard -----------------------
def draft_ids(boards_path: Path) -> set[str]:
    data = json.loads(boards_path.read_text(encoding="utf-8"))
    ids = set()
    for env in data.get("drafts", []):
        d = env.get("raw_response", {}).get("draft", {})
        did = d.get("id")
        if did is not None:
            ids.add(str(did))
    return ids


def superset_guard(new_boards: Path) -> str:
    """Newest must subsume every older boards file. A draft present in an older
    file but absent from the newest means the re-scrape is NOT a clean superset
    -- something upstream is wrong; abort so a human looks (never silent-union)."""
    new_ids = draft_ids(new_boards)
    lines = [f"newest: {len(new_ids)} distinct draft_id(s)"]
    for older in sorted(FIELD_DIR.glob("boards_*.json")):
        if older.resolve() == new_boards.resolve() or not BOARDS_RE.search(older.name):
            continue
        old_ids = draft_ids(older)
        missing = old_ids - new_ids
        lines.append(f"  vs {older.name}: {len(old_ids)} drafts, "
                     f"{len(missing)} absent from newest")
        if missing:
            raise Abort(
                "NON-SUPERSET re-scrape: "
                f"{len(missing)} draft_id(s) in {older.name} are absent from "
                f"{new_boards.name} (e.g. {sorted(missing)[:3]}). The newest "
                "export must subsume all prior ones. Investigate upstream "
                "(scraper coverage) before refreshing -- not auto-unioning.")
    return "\n".join(lines)


# --- validator parse + classify ---------------------------------------------
CHECK_RE = re.compile(r"^\s*\[(PASS|FAIL)\]\s+(.*?)\s+\((.*)\)\s*$")


def run_validate(boards_path: Path):
    cp = run([sys.executable, VALIDATE, boards_path], cwd=DATA_REPO)
    checks = []
    for line in cp.stdout.splitlines():
        m = CHECK_RE.match(line)
        if m:
            checks.append((m.group(1), m.group(2), m.group(3)))
    return cp.stdout, checks


def metrics_from_checks(checks) -> dict:
    """Pull the delta-table metrics out of the validator's printed details
    (parsing its output, NOT recomputing -- no duplicate validate logic)."""
    m = {}
    for _, name, detail in checks:
        if name.startswith("draft count"):
            g = re.search(r"got (\d+)", detail)
            if g:
                m["drafts"] = int(g.group(1))
        elif "sigma R6" in name:
            g = re.search(r"R6=([\d.]+)", detail)
            if g:
                m["sigR6"] = float(g.group(1))
        elif "sigma R12" in name:
            g = re.search(r"R12=([\d.]+)", detail)
            if g:
                m["sigR12"] = float(g.group(1))
        elif name.startswith("Season QB mean"):
            m["QB"] = _num(detail)
        elif name.startswith("Season RB mean"):
            m["RB"] = _num(detail)
        elif name.startswith("Season WR mean"):
            m["WR"] = _num(detail)
        elif name.startswith("Season TE mean"):
            m["TE"] = _num(detail)
        elif name.startswith("Season TE>=4 share"):
            m["TE4"] = _num(detail.replace("%", ""))
    return m


def _num(s: str):
    g = re.search(r"-?[\d.]+", s)
    return float(g.group(0)) if g else None


# --- privacy grep-gate ------------------------------------------------------
UUID_USERID_RE = re.compile(r'"user_id"\s*:\s*"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-')
HARD_PII_RE = re.compile(r'"(username|user_name|email|full_name|display_name|balance|phone|address)"\s*:')


def privacy_gate(boards_path: Path, owner_raw_prefix: str | None) -> list[tuple[str, int, str]]:
    """Mandatory. Returns [(label, count, command)]; any count>0 aborts."""
    text = boards_path.read_text(encoding="utf-8")
    rel = boards_path.name
    results = [
        ("raw-UUID user_id (must be hashed)", len(UUID_USERID_RE.findall(text)),
         f'grep -cE \'"user_id" *: *"[0-9a-f]{{8}}-[0-9a-f]{{4}}-\' {rel}'),
        ("hard PII keys (username/email/balance/phone/...)",
         len(HARD_PII_RE.findall(text)),
         f'grep -cE \'"(username|email|balance|phone|address|full_name|display_name)" *:\' {rel}'),
    ]
    if owner_raw_prefix:
        results.append(
            (f"owner raw user_id prefix '{owner_raw_prefix}'",
             text.count(owner_raw_prefix),
             f"grep -c '{owner_raw_prefix}' {rel}"))
    return results


# --- sim-side drafter table -------------------------------------------------
INTEGRAL_RE = re.compile(
    r"INTEGRAL QB=([\d.]+) RB=([\d.]+) WR=([\d.]+) TE=([\d.]+) SUM=([\d.]+)")
ROUND_DEV_RE = re.compile(r"ROUND_SUM_MAXDEV ([\d.eE+-]+)")


def derive_table(corpus_path: Path):
    """Run the SIM-side derivation on `corpus_path`; read its exact INTEGRAL /
    ROUND_SUM lines. Returns (integral dict, round_sum_ok, total, raw stdout)."""
    rscript = find_rscript()
    cp = run([rscript, DERIVE, corpus_path.resolve()], cwd=SIM_REPO)
    gi = INTEGRAL_RE.search(cp.stdout)
    if not gi:
        raise Abort("Sim-side derivation produced no INTEGRAL line.\n"
                    f"--- Rscript stdout (tail) ---\n{cp.stdout[-800:]}\n"
                    f"--- Rscript stderr (tail) ---\n{cp.stderr[-1200:]}")
    integral = {"QB": float(gi.group(1)), "RB": float(gi.group(2)),
                "WR": float(gi.group(3)), "TE": float(gi.group(4))}
    total = float(gi.group(5))
    gd = ROUND_DEV_RE.search(cp.stdout)
    round_sum_ok = bool(gd) and float(gd.group(1)) < 1e-6
    return integral, round_sum_ok, total, cp.stdout


# --- README provenance auto-update (real runs only) -------------------------
def update_field_readme(date: str, new_count: int, prev_count: int | None) -> bool:
    """Prepend a newest-first provenance bullet + flip the Files 'current' row.
    Best-effort: aborts staging (returns False) if anchors are missing rather
    than corrupting the README."""
    readme = FIELD_DIR / "README.md"
    text = readme.read_text(encoding="utf-8")
    anchor = "## Provenance (snapshots accumulate; newest first)\n"
    if anchor not in text:
        return False
    delta = f"(+{new_count - prev_count} over prior)" if prev_count is not None else ""
    bullet = (
        f"\n**`boards_{date}.json` — current (auto-refreshed).**\n"
        f"- Stripped from the newest `udbb-scraper-*.json` export via "
        f"`scripts/refresh_corpus.py`. **Coverage:** {new_count} drafts {delta}.\n"
        f"- Superset of all prior snapshots (dedup-by-`draft_id` guard passed).\n")
    text = text.replace(anchor, anchor + bullet, 1)
    readme.write_text(text, encoding="utf-8")
    return True


# --- report -----------------------------------------------------------------
def fmt_delta(new, old, places=3):
    if new is None:
        return "n/a"
    if old is None:
        return f"{new:.{places}f}  (new)"
    return f"{old:.{places}f} -> {new:.{places}f}  ({new - old:+.{places}f})"


def main() -> int:
    ap = argparse.ArgumentParser(description="Prepare a field-corpus refresh; stop before push.")
    ap.add_argument("--check", action="store_true",
                    help="Reproduction/gate mode: strip to a scratch dir, diff "
                         "against the committed boards_<date>.json, report; "
                         "stage nothing.")
    args = ap.parse_args()
    check = args.check

    print("=" * 72)
    print(f"  field-corpus refresh  [{'CHECK / reproduction' if check else 'REAL run'}]")
    print("=" * 72)

    # 1. select -------------------------------------------------------------
    newest, date, skipped = newest_export()
    print(f"\n[1] selected export : {newest.name}")
    print(f"    output date      : {date}  -> boards_{date}.json")
    for s in skipped:
        print(f"    skipped (subset) : {s.name}")

    _, committed_date = newest_committed_boards()
    target = FIELD_DIR / f"boards_{date}.json"
    if not check:
        if committed_date and date < committed_date:
            raise Abort(f"Newest export ({date}) is older than the committed "
                        f"corpus (boards_{committed_date}.json). Nothing new.",
                        benign=True)
        if target.exists():
            raise Abort(f"boards_{date}.json already exists -- nothing new "
                        f"(no clobber; re-run is idempotent).", benign=True)
    else:
        if not target.exists():
            raise Abort(f"--check needs the committed boards_{date}.json to "
                        f"diff against; not found.")

    # 2. strip --------------------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        out_path = (Path(td) / f"boards_{date}.json") if check else target
        cp = run([sys.executable, STRIP, newest, out_path], cwd=DATA_REPO)
        if cp.returncode != 0:
            raise Abort(f"strip_field_export.py failed:\n{cp.stdout}\n{cp.stderr}")
        owner_prefix = None
        g = re.search(r"owner raw->hash:\s*([0-9a-fA-F]+)", cp.stdout)
        if g:
            owner_prefix = g.group(1)
        print(f"\n[2] stripped -> {out_path.name}")
        for ln in cp.stdout.splitlines():
            print(f"    {ln}")

        repro_match = None
        if check:
            a = out_path.read_bytes()
            b = target.read_bytes()
            repro_match = (a == b)
            if not repro_match:
                # distinguish a timestamp-only diff from a real content diff
                ja, jb = json.loads(a), json.loads(b)
                ja2 = {k: v for k, v in ja.items() if k != "exported_at"}
                jb2 = {k: v for k, v in jb.items() if k != "exported_at"}
                repro_match = ja2 == jb2
                kind = "identical modulo exported_at" if repro_match else "CONTENT DIFFERS"
                print(f"    reproduction vs committed: {kind}")
            else:
                print(f"    reproduction vs committed: BYTE-IDENTICAL")

        # 3. validate -------------------------------------------------------
        vout, checks = run_validate(out_path)
        fails = [(n, d) for st, n, d in checks if st == "FAIL"]
        snap_fails = [(n, d) for n, d in fails if is_snapshot_dependent(n)]
        subst_fails = [(n, d) for n, d in fails if not is_snapshot_dependent(n)]
        npass = sum(1 for st, _, _ in checks if st == "PASS")
        print(f"\n[3] validator: {npass}/{len(checks)} passed")
        for line in vout.splitlines():
            if line.strip().startswith(("sigma-by-round", "R12 sigma", "construction")):
                print(f"    {line.strip()}")
        if snap_fails:
            print("    snapshot-dependent FAILs (expected drift, NOT fatal):")
            for n, d in snap_fails:
                print(f"      - {n}  ({d})")
        if subst_fails:
            print("    SUBSTANTIVE FAILs (data error):")
            for n, d in subst_fails:
                print(f"      - {n}  ({d})")

        # deltas vs previous snapshot
        prev = previous_boards(date)
        new_m = metrics_from_checks(checks)
        prev_m = {}
        if prev:
            _, pchecks = run_validate(prev)
            prev_m = metrics_from_checks(pchecks)
        print(f"\n    deltas vs {prev.name if prev else '(no prior)'}:")
        print(f"      draft count  : {new_m.get('drafts')}"
              + (f"  ({new_m.get('drafts',0) - prev_m.get('drafts',0):+d})" if prev_m.get('drafts') else ""))
        print(f"      Season sigma R6  : {fmt_delta(new_m.get('sigR6'), prev_m.get('sigR6'))}")
        print(f"      Season sigma R12 : {fmt_delta(new_m.get('sigR12'), prev_m.get('sigR12'))}")
        for pos in ("QB", "RB", "WR", "TE"):
            print(f"      construct {pos} mean : {fmt_delta(new_m.get(pos), prev_m.get(pos))}")
        print(f"      TE>=4 share %    : {fmt_delta(new_m.get('TE4'), prev_m.get('TE4'), 2)}")

        if subst_fails:
            raise Abort(f"{len(subst_fails)} SUBSTANTIVE validator failure(s) -- "
                        "aborting before any staging. (Snapshot-pin drifts above "
                        "are expected; re-pinning the validator is a human "
                        "follow-up.)")

        # 4. privacy grep-gate ---------------------------------------------
        print(f"\n[4] privacy grep-gate (must all be 0):")
        pg = privacy_gate(out_path, owner_prefix)
        for label, count, cmd in pg:
            print(f"    [{ 'OK' if count == 0 else 'HIT' }] {label}: {count}")
            print(f"          $ {cmd}")
        if any(c for _, c, _ in pg):
            raise Abort("PRIVACY GATE HIT -- forbidden content in the stripped "
                        "output. Aborting; nothing staged.")

        # 5. superset guard + sim-side derive ------------------------------
        # guard runs against the file in its final location (committed in check
        # mode; the just-written file in real mode), so it sees all prior boards.
        guard_target = target if check else out_path
        print(f"\n[5] superset/dedup guard:")
        for ln in superset_guard(guard_target).splitlines():
            print(f"    {ln}")

        corpus_for_derive = target if check else out_path
        integral, rs_ok, total, dout = derive_table(corpus_for_derive)
        print(f"\n    sim-side drafter table (corpus: {corpus_for_derive.name}):")
        print(f"      integral: QB {integral['QB']:.3f} / RB {integral['RB']:.3f} "
              f"/ WR {integral['WR']:.3f} / TE {integral['TE']:.3f}  (sum {total:.3f})")
        print(f"      round-sum check (each round P sums to 1): "
              f"{'OK' if rs_ok else 'FAIL'};  total ~ 18: "
              f"{'OK' if abs(total - 18) < 0.05 else 'CHECK'}")
        print(f"      NOTE: extension drafter table NOT touched -- flag for human "
              f"re-derivation (cross-repo, benchmark move).")

        # 6. stage (real runs only) ----------------------------------------
        if check:
            print(f"\n[6] CHECK mode -- nothing staged, nothing committed.")
        else:
            branch = f"corpus/{date}"
            print(f"\n[6] staging on branch {branch} (local commit, NO push):")
            readme_ok = update_field_readme(date, new_m.get("drafts", 0),
                                            prev_m.get("drafts"))
            if not readme_ok:
                raise Abort("README provenance anchor not found -- refusing to "
                            "stage a corpus commit without updated provenance.")
            co = run(["git", "checkout", "-b", branch], cwd=DATA_REPO)
            if co.returncode != 0:
                raise Abort(f"git checkout -b {branch} failed:\n{co.stderr}")
            run(["git", "add", str(target.relative_to(DATA_REPO)),
                 str((FIELD_DIR / 'README.md').relative_to(DATA_REPO))], cwd=DATA_REPO)
            msg = (f"corpus: refresh field corpus to {date}\n\n"
                   f"Source: {newest.name}\n"
                   f"Drafts: {new_m.get('drafts')} "
                   f"(+{new_m.get('drafts',0) - (prev_m.get('drafts') or 0)} over prior)\n"
                   f"Superset/dedup guard: passed. Validator: substantive checks "
                   f"pass; snapshot pins drifted (re-pin is a human follow-up).\n"
                   f"Sim-side drafter integral: QB {integral['QB']:.3f} / "
                   f"RB {integral['RB']:.3f} / WR {integral['WR']:.3f} / "
                   f"TE {integral['TE']:.3f}.\n")
            cm = run(["git", "commit", "-m", msg], cwd=DATA_REPO)
            if cm.returncode != 0:
                raise Abort(f"git commit failed:\n{cm.stdout}\n{cm.stderr}")
            print(f"    committed locally on {branch}.")

    # 7. report tail --------------------------------------------------------
    print("\n" + "=" * 72)
    print("  NEXT STEPS (human-only):")
    if not check:
        print(f"   - Review `git diff main..corpus/{date}` in bestball-bro-data, "
              "then publish the branch and open a PR.")
    print("   - Route the EXTENSION-table re-derivation (cross-repo benchmark).")
    print("   - Raw export deletion is manual (local PII).")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Abort as e:
        print(f"\n{'(nothing to do)' if e.benign else 'ABORT'}: {e}")
        sys.exit(0 if e.benign else 1)
