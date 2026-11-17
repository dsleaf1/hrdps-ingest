# hrdps-ingest: notes for Claude sessions

This repository bakes ECCC weather-model data to Cloudflare R2 and runs an email bot that answers
Garmin inReach text requests at `hrdpsforecast@gmail.com`. It also feeds a Salish Sea marine map;
that part is not covered here.

Read these before changing anything:

- `docs/INREACH_PIPELINE.md`: user-level walkthrough of the pipeline and reply formats (section 11 is `mt`).
- The docstrings at the top of `inreach_bot.py` and `mountain.py`: request syntax, reply format, file layout.

## The three commands

| Command | Forecast | Bake script | R2 prefix | Units |
|---|---|---|---|---|
| `wx [LAT LON]` | Surface wind and gusts, today onward | `ingest.py` | `hrdps/` | knots |
| `wv [LAT LON]` | Waves | `waves_rdwps.py` | `rdwps/` | m, s |
| `mt [LAT LON] DAY [ELEV ...]` | Wind aloft, freezing level, precipitation for one day | `mountain.py` | `mt/` | km/h, m, mm |

`mt` example and reply:

```
mt 51.23 -123.90 17 2500 3500

51.23,-123.90 18Z
Tu17 FZmax1250
2500/3500m
04 SW20/SSW28 0
08 SW22/SW32 T
...
```

## Working conventions

- Never commit to `main` directly. Work on a branch, push it, open a pull request; the owner merges.
- Run `python test_mt.py` (needs only numpy) and `python test_archive.py` before pushing.
- `inreach_bot.py` must stay standard-library only: its workflow installs no packages.
- Every reply must fit one inReach message: 160 characters (`MAX_LEN`). For `mt`, no line should
  pass 20 characters (`MT_LINE`), because the owner's inReach screen wraps there.
- The repository is public, so Actions logs are public. Never log positions, sender addresses or
  reply links unmasked (see `note()` and `mask()` in the bot).
- Every processed email is marked read even on failure. Keep it that way: a retry loop would bill
  the user for repeated satellite messages.
- GitHub's GraphQL API is blocked in Claude sessions. Use REST, e.g.
  `gh api repos/dsleaf1/hrdps-ingest/pulls -f title=... -f head=... -f base=main -F body=@file`.
- Scheduled and manually dispatched workflows run only from `main`. A push to a branch named
  `mt-*` that touches `mountain.py`, `test_mt.py` or `mountain.yml` runs a no-upload smoke test
  against the live Datamart.
- To see what the bot would answer without sending anything, dispatch `inreach.yml` with
  `dry_run=true`. A real reply appears in the run log as `reply via Garmin link`, minus the
  position line.

## Decisions already made by the owner (do not reopen without asking)

- `mt` requires the day of the month. Only days the 48 h run fully covers are served; partial days
  are refused, except today, which shows the most recent slot and those ahead.
- `mt` slots are local 04 08 12 16 20; precipitation is the 4 hours before each slot; the
  freezing level is reported as the day's maximum, not per slot.
- Up to three elevations, 500 to 4500 m, default 2000 and 3000.
- Three-line `mt` header (point + run / day + FZmax / elevations), chosen after a 22-character
  header wrapped on the device.
- `mt` coverage starts with `coast-overview`; more regions later.

## Status (2026-11-16)

`mt` is live on `main` and verified end to end from the owner's inReach:

- Live reply for Mt Olsen (51.23, -123.90) matched the Avalanche Canada HRDPS meteogram for the
  same run: every 2 km and 3 km wind within 2 km/h and one compass point, freezing level within 50 m.
- A full mountain bake is 12 steps, about 665 MB, and has taken 18 to 26 minutes.
- Requests from the inReach took 2 to 12 minutes to reach the inbox; the bot then replied within seconds.

## Open items

Untested in the field (all pass offline tests):

- `mt` for today's date (remaining slots only).
- Custom and three-elevation requests, including the 8-point compass fallback.
- A wet forecast: `T` and two-digit precipitation.

Known issues and unknowns:

- Slot hours are British Columbia clock time (UTC-7 all year from 2026). In Washington in winter
  every `wx`, `wv` and `mt` slot is one hour early on the local clock. Not yet addressed.
- `wx` and `wv` compute slot times from the runner's time-zone database; `mt` matches on the
  bake's own `steps_local` labels and is immune to stale tables.
- The bot's single-cell read from R2 works, but it has not been confirmed whether R2 honoured the
  HTTP Range request or the full-file fallback was used. Check by logging the response status once.
- With three elevations, the elevations line can pass 20 characters when a `gnd` note is added.
- There is no offline test file for `wx` and `wv` reply building.

Candidate next steps. The owner has said more regions will come later; the other two are
suggestions that have not been requested:

1. More `mt` regions: North Cascades (just east of the current 122.0 W edge) and the Rockies.
   This needs per-region time zones in `mountain.py`.
2. Precipitation type or snow level (`HGTSNLVL` and the HRDPS-WEonG fields are published).
3. Faster bake: concurrent downloads.

## Checking `mt` against ground truth

Avalanche Canada publishes HRDPS point meteograms with printed hourly wind at 2 km and 3 km:
https://meteograms.avalanche.ca/ac/ac_meteogram.html . The image for a point is at
`https://meteograms.avalanche.ca/ac/data/meteograms/HRDPS_mp/LAMc.<HH>Z_<Region>_<Point>.png`
(for example `LAMc.18Z_Southwest_Mt.Olsen.png`), and the point list with coordinates is
`https://meteograms.avalanche.ca/ac/data/ac_forecast_stations_PointMeteograms.geojson`.
Reading the image needs a browser tool. The meteogram labels its hours in PST; the bot's slots are
BC time, one hour ahead.
