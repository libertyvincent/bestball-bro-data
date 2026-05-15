# CLAUDE.md

Working notes for Claude in the `bestball-bro-data` repo. For human-facing docs, see `README.md`.

## What this repo is

A two-file data feed for the BestBall Bro Chrome extension:
- `teams/nfl_2026.json` — the team key (32 NFL teams from Underdog's `/v1/teams`)
- `projections/nfl_2026.json` — Mike Clay's player projections, refreshed daily

`build.py` downloads Clay's PDF, parses each team's offense table, normalizes team abbreviations, computes VOR/tier, and writes the projections JSON. A GitHub Action at `.github/workflows/update-projections.yml` runs it daily and commits the result.

This repo does NOT contain the Chrome extension itself — that lives in `libertyvincent/bestball-bro`.

## Conventions

**Team abbreviations.** Always use the canonical NFL codes from `teams/nfl_2026.json` — ARI/BAL/CLE/HOU/JAX/LAR/LAC/WAS — never ESPN's `ARZ/BLT/CLV/HST/JAC/LA/SD/WSH`. The `ESPN_TO_STANDARD` map in `build.py` is the single place that translation lives; don't replicate it in the extension or anywhere else.

**Player keys.** `"<Player Name>|<TEAM>|<POS>"` where TEAM is the canonical abbr and POS ∈ {QB, RB, WR, TE}. Example: `"Deshaun Watson|CLE|QB"`. The pipe character is the separator.

**Schema preservation.** When evolving the JSON, keep existing field names — the extension's `BBBRO_MATCH` indexes on them. Add fields rather than rename. The `_meta` block documents what scoring and replacement levels were used; mirror any change to those constants into the output `_meta` in the same commit.

**Two rank fields.** Each player has both `pos_rk` (half-PPR rank from `proj_total` — what the extension uses for Underdog drafts) and `clay_pos_rk` (full-PPR rank from `clay_ppr_total` — Clay's own published rank, preserved for reference). Don't conflate them. The `vor`/`tier` fields are half-PPR and align with `pos_rk`.

## When you need to modify build.py

- **Tier bands** are tuned to the existing JSON's tier assignments and live in `TIER_BANDS`. Bands are per-position VOR thresholds; edit there to retune.
- **Scoring rules and replacement levels** are baked into `build.py` AND echoed into the output `_meta`. If you change one, change the other in the same commit.
- **PDF parser** (`parse_team_page` + `OFFENSE_ROW_RE`) depends on Clay's column order on the per-team pages: Pos | Player | Gm | Pass(Att,Comp,Yds,TD,INT,Sk) | Rush(Att,Yds,TD) | Rec(Tgt,Rec,Yd,TD) | Pts | Rk. If ESPN restructures the PDF, the parser will fail loudly via the `MIN_EXPECTED_PLAYERS` floor and the "must parse 32 teams" check — re-inspect the PDF and retune the regex.
- **PDF URL** is in `SOURCE_URL`. ESPN versions these PDFs by season year, so expect to bump `26` to `27` next offseason.
- **Safety floors** (`MIN_EXPECTED_PLAYERS`, the 32-team check, the canonical-abbr check) exist so a bad parse never produces a broken JSON. Don't loosen them without good reason.

## When you need to modify the workflow

The cron runs at 11:00 UTC daily (`.github/workflows/update-projections.yml`). The workflow needs `permissions: contents: write` for the bot to commit back. The commit step is skipped automatically when the diff is empty, so daily runs that find no new data are no-ops.

To trigger manually: GitHub UI → Actions tab → "Update Clay projections" → "Run workflow."

## Relationship to the extension

The extension repo (`bestball-bro`) consumes `projections/nfl_2026.json` via `BBBRO_PROJECTIONS.get()`, builds a match index via `BBBRO_MATCH.buildIndex(payload)`, and attaches `.projection` to each Underdog appearance in `enrichAppearancesWithProjections()`. From there, VOR feeds the Bro score in `broScore_bestball()`.

The match join in the extension is name-and-team based, which is why the team-abbreviation normalization in this repo matters: when Clay's data and Underdog's data agree on team codes, every player matches.

## What was just fixed

Before this iteration: `projections/nfl_2026.json` used ESPN's nonstandard team codes (`CLV`, `BLT`, `ARZ`, `HST`), which didn't match Underdog's canonical codes (`CLE`, `BAL`, `ARI`, `HOU`). The extension's `BBBRO_MATCH` couldn't reconcile players on those four teams, leaving VOR (and therefore Bro score) blank for several players per draft. Normalizing in `build.py` fixes this without any extension change.

## Extension-side docs that need a small follow-up

`bestball-bro/CLAUDE.md` and `bestball-bro/ARCHITECTURE.md` predate the recommendation engine and don't yet document `BBBRO_PROJECTIONS` or `BBBRO_MATCH`. Next time you're in that repo, add a short section under "Data Sources" describing:

1. The projections feed (URL, schema, refresh cadence — pulled from this repo).
2. `BBBRO_MATCH.buildIndex(payload)` and `BBBRO_MATCH.matchAppearance(appearance, index)` — the join API.
3. `enrichAppearancesWithProjections()` in `content.js` — where the join is applied.
4. `broScore_bestball()` and the `vor → need → scarcity → adpVal → stack` chain.
