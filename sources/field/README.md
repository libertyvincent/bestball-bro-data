# Field-calibration corpus

Privacy-stripped completed-draft boards from Underdog best-ball, the empirical
basis for field-side calibration: the harness field-σ refit, the field
construction distribution (the leverage refinement's denominator), and a future
empirical ghost.

Dated files accumulate — **never overwrite**. Future exports add
`boards_<date>.json` alongside this one.

## Files

| file | what |
|---|---|
| `boards_2026-06-10.json` | stripped corpus (51 drafts, 8 reference payloads) |
| `../../scripts/strip_field_export.py` | the privacy strip that produced it |
| `../../scripts/validate_field_corpus.py` | integrity check (recomputes headline stats) |

## Provenance

- **Source:** passive `udbb-scraper` capture (fetch/XHR interception) of Vincent's
  **completed-draft history** walk on Underdog — not an active/live feed.
- **Scraper:** `0.4.0-ww-harvester`, export schema **v3**.
- **Exported:** `2026-06-10T06:21:26Z`. **`draft_at` range:** 2026-04-27 → 2026-06-10.
- **Coverage:** 51 drafts — 50 × 216-pick boards + 1 × 240 (a Superflex board).
  Slate split: **16 Weekly Winners / 33 Season / 1 Frenchie Eliminator / 1 Field
  General (Superflex)**.

## Privacy strip (this repo is PUBLIC / gh-pages)

Applied by `strip_field_export.py` (run it to reproduce; it is deterministic):

- **Dropped all account-scoped envelopes** — every `/v*/user/...` capture
  (entries, balances, active drafts, rankings) plus non-reference captures
  (`/v1/tournaments`, bare `/v1/slates/<id>`, `/v2/slates/.../matches`). Kept
  **only** the slate reference payloads needed for the pick→position join:
  `/v1/slates/<id>/players` and `/v1/slates/<id>/.../appearances` (8 total).
- **Hashed every `user_id`** → `sha256(user_id)[:12]` (raw ids are opaque,
  high-entropy UUIDs; no salt). The owner's hash is tagged `is_owner: true` on
  its draft entries.
- **Dropped `draft_round_index`** (owner-scoped round history). Kept
  `round_tournament_index` (public tournament metadata); the draft→tournament
  join also survives on each envelope's own `tournament_id`.
- **Retained player `first_name`/`last_name`** — public NFL data, present only
  in the `/players` reference payloads, kept so consumers needn't re-join.
- **Scoped defensive sweep** (asserted clean): hard PII
  (`username`/`email`/`balance`/`phone`/…) forbidden **anywhere**; player names
  permitted **only** inside reference payloads, forbidden in `draft_entries` or
  any user-context object.

Everything else (216-pick boards, `projection_adp` per pick, `draft_entries`
with `pick_order`, slate/tournament ids, timestamps) is kept byte-faithful.

## Conventions

- **Opponent-only.** Every field statistic **excludes the owner** (`is_owner`).
  The owner is the scraping account and is not part of "the field."
- **CB→WR remap (stats contract).** Underdog lists Travis-Hunter-class players
  with `position_name: "CB"` (their defensive position); in fantasy they start
  at WR. The corpus's construction stats remap **CB→WR** (26 picks here),
  matching the sim's documented `player_match` fantasy-position convention.
  Consumers computing position counts must apply the same remap.

## Headline numbers (opponent-only; see the validator)

- **Pick-vs-ADP residual σ by round** (216-boards): R1 ≈ 2.33, R6 ≈ 5.18,
  R12 ≈ 10.1, plateau ≈ 10 after R12. **≈ 2–3× chalkier than the parametric
  harness assumption.**
  - **Slate-split structure (important):** the pooled R12 ≈ 10 *blends two
    genuinely different rooms* — **Season ≈ 8.5** vs **Weekly Winners ≈ 12.8**.
    The pooled plateau is a count-weighted average, not a single-room value.
    **Consumers refitting the harness σ should prefer the slate-specific
    values**, not the pooled curve.
- **Season opponent construction** (complete 18-pick rosters, n = 363):
  QB 2.58 / RB 5.40 / WR 7.31 / TE 2.71. **TE≥4 ≈ 3.9%; TE≥4 & RB≤4 ≈ 1.1%.**

## June-snapshot caveat (read before this becomes load-bearing)

This is a **self-selected, off-season snapshot.** Draft speed, stakes, entrant
mix, and April–June timing all modulate chalkiness, and the rooms here are
whatever Vincent happened to enter. The σ and construction figures are directional
and **must be refreshed from a later, larger export before they replace the
harness σ or drive curve regeneration.** Treat the current numbers as a
first-look calibration, not ground truth.

## Reproduce / validate

```sh
python scripts/strip_field_export.py <raw_export.json> sources/field/boards_2026-06-10.json
python scripts/validate_field_corpus.py sources/field/boards_2026-06-10.json   # expect 14/14
```
The validator recomputes the headline stats from the stripped file and asserts
they reproduce the analysis of the raw export (tolerances: means ±0.02, σ ±0.1,
shares ±0.3pp) — proof the strip preserved analytical content.
