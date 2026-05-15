# bestball-bro-data

NFL fantasy data feeding the [BestBall Bro] Chrome extension. Two JSON files; one refreshed daily by a GitHub Action.

## Files

- **`teams/nfl_2026.json`** — the 32 NFL teams (canonical abbreviations, full names, brand colors). Source: Underdog Fantasy `/v1/teams`. Updated manually as needed (typically once a season). This is the **team key** — the source of truth for the 32 abbreviations every consumer of this repo uses.
- **`projections/nfl_2026.json`** — Mike Clay's 2026 season-long projections with VOR and tier added. Refreshed daily by `build.py`.

## URLs for the extension

The extension should fetch `projections/nfl_2026.json` from a CDN, **not** from a `/blob/` URL (those serve HTML, not JSON):

- **jsDelivr** (preferred — globally cached, no rate limits on users):
  ```
  https://cdn.jsdelivr.net/gh/libertyvincent/bestball-bro-data@main/projections/nfl_2026.json
  ```
- **GitHub raw** (uncached, hits GitHub directly):
  ```
  https://raw.githubusercontent.com/libertyvincent/bestball-bro-data/main/projections/nfl_2026.json
  ```

For the team key, swap `projections` → `teams` in either URL:
```
https://cdn.jsdelivr.net/gh/libertyvincent/bestball-bro-data@main/teams/nfl_2026.json
https://raw.githubusercontent.com/libertyvincent/bestball-bro-data/main/teams/nfl_2026.json
```

## Daily refresh

`.github/workflows/update-projections.yml` runs every day at **11:00 UTC** (7 AM ET during DST, 6 AM ET in winter). On each run, `build.py`:

1. Downloads Clay's PDF from ESPN at the URL in `_meta.source_url`.
2. Parses each of the 32 per-team offense tables.
3. Normalizes ESPN's nonstandard team codes (`CLV→CLE`, `BLT→BAL`, `ARZ→ARI`, `HST→HOU`, `JAC→JAX`, `WSH→WAS`, `LA→LAR`, `SD→LAC`) so projection keys join cleanly against Underdog player data.
4. Computes VOR (proj_total minus the position's replacement level) and assigns a tier band per position.
5. Ranks players within each position twice: `pos_rk` by Underdog half-PPR `proj_total`, and `clay_pos_rk` by Clay's full-PPR `clay_ppr_total` (the PDF's Pts column).
6. Writes `projections/nfl_2026.json`.
7. Commits and pushes to `main` — **but only if the file actually changed.** No-op runs leave the repo untouched.

Failures are visible in the [Actions tab][actions]. The build fails loudly (non-zero exit) if it parses fewer than 32 teams, fewer than 350 players, or finds any team code that doesn't resolve to one of the canonical 32 abbreviations. No broken JSON is ever committed.

## Running build.py locally

```sh
pip install -r requirements.txt
python build.py
```

Requires internet (downloads the PDF from ESPN). Writes `projections/nfl_2026.json` in place.

## Schema

`projections/nfl_2026.json`:

```json
{
  "_meta": {
    "version": "2026-05-15",
    "generated_at": "2026-05-15T11:00:00.000000+00:00",
    "season": 2026,
    "sport": "NFL",
    "scoring": "underdog_half_ppr",
    "source": "mike_clay_espn",
    "source_url": "https://g.espncdn.com/s/ffldraftkit/26/NFLDK2026_CS_ClayProjections2026.pdf",
    "replacement_levels": { "QB": 200, "RB": 110, "WR": 100, "TE": 85 },
    "scoring_rules":      { "pass_yd": 0.04, "pass_td": 4, ... },
    "player_count": 414
  },
  "players": {
    "Josh Allen|BUF|QB": {
      "name": "Josh Allen", "team": "BUF", "pos": "QB",
      "games": 17, "proj_total": 391.7, "proj_ppg": 23.04,
      "vor": 191.7, "tier": 1,
      "pos_rk": 1, "clay_pos_rk": 1, "clay_ppr_total": 369,
      "components": { "pass_yd": 3945, "pass_td": 26, ... }
    },
    ...
  }
}
```

Keys are `"<Player Name>|<TEAM>|<POS>"` where TEAM is the canonical abbreviation (always matches one in `teams/nfl_2026.json`) and POS is one of `QB`, `RB`, `WR`, `TE`.

Two rank fields per player:
- `pos_rk` — half-PPR rank by `proj_total` (matches `vor`/`tier` ordering; this is what the extension cares about).
- `clay_pos_rk` — full-PPR rank by Clay's published `clay_ppr_total` (matches the PDF's Rk column).

## Why two JSON files?

`teams/nfl_2026.json` is the **canonical source of truth** for the 32 NFL franchises. `projections/nfl_2026.json` is **derived data** that references those same abbreviations. If Clay's PDF ever ships a team code that can't be mapped to one of the 32, `build.py` fails the build instead of silently corrupting downstream joins.

[actions]: https://github.com/libertyvincent/bestball-bro-data/actions
[BestBall Bro]: https://github.com/libertyvincent/bestball-bro
