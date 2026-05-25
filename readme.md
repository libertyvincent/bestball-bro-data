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

## Source feeds

In addition to Clay's projections, the daily workflow publishes three sources of ranking data to `gh-pages`. All live under `sources/` on the `gh-pages` branch and are served by GitHub Pages.

| File | Source | Notes |
|---|---|---|
| `sources/clay_2026_offense.json` | Mike Clay (ESPN PDF) | Per-player offensive projections, all positions. |
| `sources/clay_2026_weekly_team_scoring.json` | Mike Clay (ESPN PDF) | Per-team weekly score projections + season totals. |
| `sources/clay_2026_unit_grades.json` | Mike Clay (ESPN PDF) | Page-63 unit grades (offense/defense/total). |
| `sources/etr_2026_season.json` | ETR (Establish The Run) | Underdog Season slate rankings, top 300. |
| `sources/etr_2026_eliminator.json` | ETR | Underdog Best Ball Eliminator rankings. |
| `sources/etr_2026_weekly_winners.json` | ETR | Underdog Weekly Winners rankings. |
| `sources/etr_2026_superflex.json` | ETR | Underdog Superflex rankings. |
| `sources/legup_2026_ud.json` | LegUp `ud-ranks` | Underdog Season slate, with Underdog UUIDs. |
| `sources/legup_2026_eliminator.json` | LegUp `eliminator-ranks` | Eliminator slate; includes Week-17 opponent column. |

The blender that consumes these lives in [`bestball-bro-sim`](https://github.com/libertyvincent/bestball-bro-sim); this repo's job ends at publishing.

Base URL: `https://libertyvincent.github.io/bestball-bro-data/sources/<file>.json`

## Daily refresh

`.github/workflows/update-projections.yml` runs every day at **11:00 UTC** (7 AM ET during DST, 6 AM ET in winter) and publishes everything to the `gh-pages` branch via `peaceiris/actions-gh-pages@v4` with `keep_files: true`. Steps:

1. **Build Clay sources** — `build.py` downloads Clay's PDF from ESPN, parses the 32 per-team tables, normalizes ESPN's nonstandard team codes (`CLV→CLE`, `BLT→BAL`, `ARZ→ARI`, `HST→HOU`, `JAC→JAX`, `WSH→WAS`, `LA→LAR`, `SD→LAC`), computes VOR + tier, ranks players, and writes `projections/nfl_2026.json` + the three `sources/clay_*.json` files. Build fails loudly if it parses fewer than 32 teams, fewer than 350 players, or finds any team code that doesn't resolve to one of the canonical 32 abbreviations.
2. **Build ETR sources** — `build_etr.py` fetches the four ETR ranking pages using a WordPress session cookie (`ETR_SESSION_COOKIE` repo secret) and writes `sources/etr_2026_<slate>.json`. Auth failures exit `2`, other failures exit `1`. `continue-on-error: true` keeps the other sources publishing even if ETR breaks.
3. **Build LegUp sources** — `build_legup.py` hits LegUp's public Cloud Functions (no auth) for `ud-ranks`, `eliminator-ranks`, and `main-event`, and writes `sources/legup_2026_<output>.json`. Each slug has a pinned `expected_headers` list; LegUp changing their schema upstream aborts the build loudly.
4. **Publish** — `peaceiris/actions-gh-pages@v4` pushes `./build/` to `gh-pages`. `keep_files: true` means a per-source failure leaves yesterday's copy of that file on `gh-pages` intact; the rest still refresh.

Failures are visible in the [Actions tab][actions].

## ETR authentication

`build_etr.py` needs a WordPress session cookie set as the repo secret **`ETR_SESSION_COOKIE`**. The cookie is good for roughly one year if captured with "Remember Me" checked.

**Cookie refresh procedure** (when the workflow starts failing with exit `2` and "AUTH FAIL" in the logs):

1. Open https://establishtherun.com/login in a Chromium browser. Check **Remember Me**, log in.
2. Navigate to any one of the four ranking pages used in `build_etr.py` (e.g. https://establishtherun.com/etrs-top-300-for-underdogfantasy/).
3. Open DevTools → **Network** tab → hard-reload the page.
4. Click the top document request → **Headers** → **Request Headers** → copy the entire `Cookie:` header value (everything after `Cookie: `, all on one line).
5. In GitHub: **Settings → Secrets and variables → Actions → Repository secrets → `ETR_SESSION_COOKIE`** → **Update secret**, paste the new value.
6. Trigger **Update Clay projections** manually from the Actions tab to confirm the new cookie works.

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
