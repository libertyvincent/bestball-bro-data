# Field-corpus tooling

Scripts that turn a raw Underdog scraper export into the committed, privacy-safe
field corpus (`sources/field/boards_<date>.json`).

| script | role |
|---|---|
| `strip_field_export.py` | privacy strip (raw export → stripped corpus). Canonical; unchanged. |
| `validate_field_corpus.py` | integrity check (recomputes headline stats vs pinned tolerances). Canonical; unchanged. |
| `refresh_corpus.py` | **orchestrator** — wraps the deterministic middle of the refresh chain, then stops. |
| `refresh_corpus.bat` | double-click entry point for `refresh_corpus.py` on Windows. |

## Refresh workflow

1. **Drop** a fresh full export in `C:\Users\vince\Desktop\udbb-scraper\`
   (filename `udbb-scraper-<ISO>.json`).
2. **Run** `refresh_corpus.bat` (or `python scripts/refresh_corpus.py`). It:
   - selects the newest export (older ones are subsets of a superset re-scrape);
   - strips → `sources/field/boards_<date>.json` (never clobbers an existing date);
   - validates and prints **deltas vs the previous snapshot**, classifying any
     snapshot-dependent FAIL (corpus size, board-size mix, slate split, pooled
     R12 σ) as expected drift and **aborting only on a substantive data failure**
     (construction means, TE shares, single-owner, R1/R6/plateau σ);
   - runs a mandatory **privacy grep-gate** (raw-UUID `user_id`, hard PII, owner
     raw prefix → all must be 0);
   - runs a **superset/dedup guard** (aborts if any prior draft is absent from the
     newest — never silently unions a broken re-scrape);
   - re-derives the **sim-side** drafter table and prints its integral;
   - stages the new boards file + provenance on branch `corpus/<date>` and
     **commits locally**.
3. **Review** `git diff main..corpus/<date>`, then push the branch and open a PR.

Validate the automation before trusting it:
`python scripts/refresh_corpus.py --check` reproduces the latest committed
`boards_<date>.json` to a scratch dir, diffs it (byte-identical modulo
`exported_at`), and reports — staging nothing.

## Three human-only steps (the script will NOT do these)

1. **Push / open the PR** — publishing scraped opponent data is a release
   decision. The script commits locally and stops.
2. **Re-derive / install the EXTENSION drafter table** — moving the benchmark
   that validation runs measure against is human-routed (cross-repo). The script
   regenerates only the *sim-side* table and flags the extension table.
3. **Delete the raw export** — irreversible local-PII deletion, human-only.
