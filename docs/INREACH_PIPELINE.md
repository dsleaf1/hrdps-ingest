# inReach Wind & Wave Forecasts: How the Data Gets to Your Device

This document follows a forecast from Environment Canada's computers to the text on your inReach screen. It covers where the data is stored, every step the system takes to answer a `wx` (wind) or `wv` (waves) request, and how to read the reply.

All code lives in the [`dsleaf1/hrdps-ingest`](https://github.com/dsleaf1/hrdps-ingest) repository. File names in this document refer to that repo.

---

## 1. The big picture

Two separate jobs run independently. The first prepares the data on a schedule, whether or not anyone asks for a forecast. The second answers requests as they arrive.

```
 PREPARE (scheduled, 4x/day each)                    ANSWER (on demand, ~1 min)
 ─────────────────────────────────                   ──────────────────────────
 ECCC Datamart                                       inReach / email
   HRDPS wind GRIB2 ──► ingest.py ──┐                     │ "wx 49.15 -125.91"
   RDWPS wave GRIB2 ──► waves_rdwps.py ─┐                 ▼
                                    │   │            Gmail inbox (hrdpsforecast@gmail.com)
                                    ▼   ▼                 │
                         Cloudflare R2 bucket             │ Apps Script sees new mail (every minute)
                         (manifests + binary grids)       ▼
                                    │              GitHub Actions: inreach_bot.py
                                    └────────────────►  reads the grids, builds the reply
                                                          │
                                                          ▼
                                       Garmin Messenger reply page ──► your inReach
                                       (or an email reply, for email requests)
```

The design keeps the slow, heavy work (downloading and decoding hundreds of model files) out of the request path. When a request arrives, the bot only downloads one small manifest and one or two pre-cut binary files, then reads a handful of bytes from them.

---

## 2. The "database": Cloudflare R2

There's no conventional database. The forecast store is a **Cloudflare R2 bucket**: S3-style object storage with a public read URL. It holds a set of files that are overwritten with each new model run. Each dataset has two kinds of file:

- a **manifest** (JSON): which model run this is, which forecast hours exist, and the grid geometry of each region;
- one **binary grid per region**: every value for every cell and every hour, packed as single bytes.

All files are gzip-compressed on upload.

| Object | Written by | Contents |
|---|---|---|
| `hrdps/manifest.json` | `ingest.py` | Wind run time, list of 49 forecast hours, each region's bounding box, columns and rows, gust labels |
| `hrdps/coast-overview.bin` | `ingest.py` | Wind speed + direction, Puget Sound → Dixon Entrance (46.9–54.5°N, 122.0–133.6°W) |
| `hrdps/north-passage.bin` | `ingest.py` | Wind speed + direction, Prince Rupert → Skagway (54.4–59.6°N, 129.5–137.0°W) |
| `hrdps/coast-overview.gust.bin` | `ingest.py` | Gusts for the southern region |
| `hrdps/north-passage.gust.bin` | `ingest.py` | Gusts for the northern region |
| `rdwps/manifest.json` | `waves_rdwps.py` | Wave run time, forecast hours, region geometry, encoding notes |
| `rdwps/coast-overview.bin` | `waves_rdwps.py` | Wave height, period, direction, southern region |
| `rdwps/north-passage.bin` | `waves_rdwps.py` | Wave height, period, direction, northern region |

The same bucket also holds the Salish marine map's other data (currents, map-only wind regions, archives). The bot reads only the files above.

**Why bytes?** A whole region for 49 hours fits in 1–4 MB compressed, small enough for the bot to fetch in a second or two. A byte (0–255) is precise enough for paddling purposes: 1 km/h for wind, 0.1 m for waves, 1.4° for direction.

---

## 3. Preparing the wind data (`ingest.py`)

**When:** GitHub Actions runs it four times a day at 05:05, 11:05, 17:05 and 23:05 UTC (workflow `ingest.yml`). Each time it picks up the newest complete HRDPS run, normally the one from about five hours earlier.

**Source:** ECCC's **HRDPS Continental 2.5 km** (High Resolution Deterministic Prediction System), published as GRIB2 files on the MSC Datamart at `dd.weather.gc.ca/<date>/WXO-DD/model_hrdps/continental/2.5km/<run>/<hour>/`.

### Steps

1. **Find the latest complete run.** The script lists today's and yesterday's run folders (00, 06, 12, 18 UTC), newest first. It takes the first one whose hour-48 folder already contains the wind file, so it never publishes a half-finished run.
2. **Download three fields for each of 49 forecast hours (000–048):**
   - `WIND_AGL-10m`: wind speed 10 m above the surface (m/s)
   - `WDIR_AGL-10m`: direction the wind blows **from** (degrees)
   - `GUST_AGL-10m`: wind gust (m/s). The gust file name is found automatically from the run's file listing, so a renaming at ECCC doesn't break it.
3. **Decode the GRIB2.** Each file is read with `cfgrib`/`eccodes`, which return the values plus each grid point's latitude and longitude. HRDPS uses a *rotated* lat/lon grid (`RLatLon0.0225`), so its points don't line up with ordinary latitude/longitude lines.
4. **Regrid onto a plain lat/lon grid.** For each region, the script builds a regular grid at 0.0225° spacing (about 2.5 km north–south). Row 0 is the northern edge and column 0 the western edge. Each target cell takes the value of the **nearest** HRDPS point, found with a k-d tree; values are copied, never interpolated or blended. Target cells with no model point within 1.6 grid spacings are marked as no data. The region sizes are:
   - `coast-overview`: 517 columns × 339 rows = 175,263 cells
   - `north-passage`: 334 columns × 232 rows = 77,488 cells
5. **Convert and pack into bytes:**
   - speed: m/s × 3.6 → km/h, rounded, clipped to 0–254 (255 = no data)
   - direction: degrees × 255/360, rounded (0–255)
   - gust: m/s × 3.6 → km/h, same as speed
6. **Upload to R2:** the region `.bin` files, the gust `.gust.bin` files, and `manifest.json`, all gzipped.

### Wind file layout

For each forecast hour *h* (0–48, matching the manifest's `hours` list), the wind file holds two blocks of `rows × cols` bytes:

```
offset = h × 2 × rows × cols
[ speed bytes for every cell ][ direction bytes for every cell ]
```

The gust file holds one block per hour (`offset = h × rows × cols`).

To decode:
- `km/h = speed_byte`, where 255 means no data
- `degrees_from = direction_byte × 360 / 255`
- `knots = km/h / 1.852`

---

## 4. Preparing the wave data (`waves_rdwps.py`)

**When:** four times a day at 04:40, 10:40, 16:40 and 22:40 UTC (workflow `waves_rdwps.yml`).

**Source:** ECCC's **RDWPS national 2.5 km** (Regional Deterministic Wave Prediction System) at `dd.weather.gc.ca/<date>/WXO-DD/model_rdwps/national/2.5km/<run>/`. It's a spectral wave model, so it carries ocean swell as well as local wind waves. It runs on the same rotated 2.5 km grid as HRDPS, hourly to 48 hours.

### Steps

1. **Find the latest complete run:** the newest run folder that already has the hour-48 wave-height file.
2. **Download three fields for each of 49 hours:**
   - `HTSGW`: significant height of combined wind waves and swell (m)
   - `PWPER`: primary wave mean period (s)
   - `PWAVEDIR`: primary wave direction, degrees the waves come **from**
3. **Decode and regrid** exactly as for wind, onto the **same** two region grids. A cell number therefore means the same place in the wind and wave files.
4. **Mark land.** RDWPS has no values over land or in water too narrow for its grid. Those cells become 255.
5. **Pack into bytes:**
   - height: metres × 10 (decimetres), 0–254
   - period: whole seconds
   - direction: degrees × 255/360
6. **Upload** both region files, then the manifest last, so the bot never sees a manifest pointing at files that aren't there yet.

### Wave file layout

Each hour holds three blocks:

```
offset = h × 3 × rows × cols
[ height bytes ][ period bytes ][ direction bytes ]
```

To decode:
- `metres = height_byte / 10`
- `seconds = period_byte`
- `degrees_from = direction_byte × 360 / 255`
- 255 in any block means no data

---

## 5. A request arrives

### 5.1 Sending

From an inReach, you send a message to **hrdpsforecast@gmail.com**. Garmin delivers it as an email from `no.reply.inreach@garmin.com`. The email contains:

- your message text, such as `Wx 49. 36 -125. 72` (the inReach keyboard often adds a space after decimal points);
- a reply link, `https://inreachlink.com/<code>`, which is the only way to send a message back to your device;
- your GPS position: `… sent this message from: Lat 49.36 Lon -125.72`.

From a phone or computer, you send an ordinary email with `wx …` or `wv …` in the body or the subject.

### 5.2 Waking the bot

Two mechanisms start the bot:

1. **Fast path: Google Apps Script.** A small script (`apps_script/dispatch.gs`) runs inside the bot's Gmail account every minute. If it finds an unread email newer than the last one it handled, it calls GitHub's API to start the **inReach wind bot** workflow (`inreach.yml`). The script authenticates with a fine-grained token limited to that one repository's Actions. In practice this takes about a minute from email arrival to the bot starting.
2. **Backup: GitHub's own schedule.** The workflow is also scheduled every 10 minutes. GitHub runs frequent schedules late on free accounts, often hours apart, so this only matters if the Apps Script ever stops.

### 5.3 Reading the inbox

`inreach_bot.py` logs in to Gmail over IMAP with an app password. It searches for **unread** messages and handles each one in turn. After handling a message, successful or not, it marks it read, so a fault can never make it reply twice and bill the paddler again.

---

## 6. Inside the bot: from request to reply

### Step 1: Parse the command

The bot takes the text **above** Garmin's footer (cut at "View the location", "send a reply" or "sent this message from") so it never mistakes the footer's GPS numbers for a typed request. It then cleans up the text:

- typographic minus signs and dashes become `-`, and degree signs are dropped;
- `49. 36` is joined back into `49.36`.

Next it looks for `wx` or `wv`, optionally followed by two numbers. N/S and E/W letters, commas and spaces are all accepted. Then:

- **Two numbers found:** they're latitude and longitude. A positive longitude is flipped to west, since the whole coverage area is west longitude.
- **No numbers:** the GPS position from Garmin's footer is used.
- **Numbers present but unreadable, or no GPS:** the bot replies with a one-line help message rather than guessing a position.
- **Neither `wx` nor `wv`:** the email is ignored. An inReach sender gets the help message; an ordinary email sender gets nothing.

If the command isn't in the body, the subject line is checked as well.

### Step 2: Load the right data

The bot fetches the manifest (`hrdps/manifest.json` for `wx`, `rdwps/manifest.json` for `wv`). It checks the regions in order (`coast-overview`, then `north-passage`) and picks the first whose bounding box contains the point. Only that region's file is downloaded. A point outside both boxes gets "outside the forecast area".

### Step 3: Find the grid cell

The region's box and size convert the point to a column and row:

```
col  = round( (lon − West)  / (East − West)  × (cols − 1) )
row  = round( (North − lat) / (North − South) × (rows − 1) )
cell = row × cols + col
```

**Worked example**, Tofino offshore, 49.15°N 125.91°W, in `coast-overview` (box 133.6–122.0°W, 46.9–54.5°N, 517 × 339):

```
col  = round( (−125.91 + 133.6) / 11.6 × 516 ) = round(342.1) = 342
row  = round( (54.5 − 49.15)    /  7.6 × 338 ) = round(237.9) = 238
cell = 238 × 517 + 342 = 123,388
```

**For waves only:** if that cell is land or unresolved water, the bot searches outward for the nearest cell with wave data **within 5 km**. If it finds one at 1.5 km or more, the reply header shows the distance (`@3km`). If none is found, the reply explains that the point is in a channel or inlet too narrow for the wave model.

### Step 4: Work out the time slots

Slots are fixed at **06, 09, 12, 15, 18 local time**.

- **Time zone:** Pacific time, or Alaska time in the Alaska panhandle. The panhandle is approximated by a line from Dixon Entrance / Portland Canal (56°N, 130°W) to White Pass (59.5°N, 135.1°W). Daylight saving is handled automatically from the date.
- **Which days:** today, unless 18:00 has already passed, then the following days as far as the 48-hour run reaches.
- **Past slots:** a slot whose time was more than an hour ago shows `-`.
- **Beyond the run:** a slot after the last forecast hour shows `-`.

Each local slot time is converted to UTC and looked up in the manifest's list of forecast hours.

**Worked example:** a request on Saturday 31 Oct at 08:40 PDT, against the 12Z run. The `09` slot is 09:00 PDT = 16:00 UTC, which is hour index 4 of the run (12Z + 4 h).

### Step 5: Summarise each slot over 3 hours

Each slot covers the **3 hours centred on it**: index 4 plus its neighbours 3 and 5, i.e. 08:00–10:00 local. So the five slots together cover 05:00–19:00 without gaps. If the run doesn't have a neighbouring hour (at the very start or end of the run), the hours it does have are used.

| Value | How the 3 hours are combined | Why |
|---|---|---|
| Wind speed | **average** | Typical conditions you'll paddle in |
| Wind direction | from the **windiest** of the 3 hours | Direction of the wind that matters most |
| Gust | **highest** | Worst case to expect |
| Wave height | **highest** | Worst case to expect |
| Wave period and direction | from the hour with the **highest** waves | Describes those waves |

The bytes are read straight from the binary file:

```
wind speed byte  = wind.bin[ h × 2 × span + cell ]          (span = rows × cols)
wind dir byte    = wind.bin[ h × 2 × span + span + cell ]
gust byte        = gust.bin[ h × span + cell ]
wave height byte = wave.bin[ h × 3 × span + cell ]
wave period byte = wave.bin[ h × 3 × span + span + cell ]
wave dir byte    = wave.bin[ h × 3 × span + 2 × span + cell ]
```

### Step 6: Turn values into codes

**Wind** (`wx`):
- direction → nearest of 16 compass points (N, NNE, NE, … NNW)
- speed in knots, rounded
- `G` + gust in knots, never shown lower than the speed
- under 3 knots → `C` (calm)

**Waves** (`wv`):
- direction → 16 compass points
- height to 0.1 m
- `/` + period in seconds
- under 0.2 m → `F` (flat)

### Step 7: Assemble and send

The reply is a header line plus one line per day. Lines are packed into messages of at most **160 characters**, splitting only between days. For `wv`, if a late-evening reply would otherwise need two messages, a "today" line with only one or two slots left is dropped.

Sending depends on how the request came in:

- **From an inReach:** Garmin no longer accepts email replies to inReach messages; it bounces them. The bot instead opens the message's `inreachlink.com` link, which leads to Garmin's Messenger web reply page (`messenger.garmin.com/web/reply/<id>`), and posts the forecast the same way that page's Send button does. It reads the page's current reply action from Garmin's own JavaScript each time, so a Garmin update doesn't break it. Garmin then queues the message for the device.
- **From an ordinary email:** the bot replies by email from hrdpsforecast@gmail.com.

The bot records each reply's length and Garmin's response in the GitHub run log. To protect paddlers' privacy, coordinates are left out of that log, because the repository is public.

### Step 8: The device receives it

The inReach only picks up incoming messages when it checks in with the satellites: on its automatic message-check interval, or when you use **Check Messages**. It needs a clear view of the sky. This is usually the longest part of the round trip.

---

## 7. How to read the reply

### 7.1 Wind (`wx`)

```
50.12,-127.65 18Z
Sa24 SE12G20 SE15G25 S18G28 SW15G22 W10G15
Su25 NW8G12 NW10G14 - - -
```

**Header:** the point the forecast is for, then the HRDPS run it came from, in UTC (`18Z` = the 18:00 UTC run).

**Day lines:** weekday and date (`Sa24` = Saturday the 24th), then five slots, always in this order:

```
        06:00    09:00    12:00    15:00    18:00
Sa24   SE12G20  SE15G25  S18G28   SW15G22  W10G15
```

| Code | Meaning |
|---|---|
| `SE15G25` | Wind **from** the southeast, averaging 15 knots over the 3 hours, gusts up to 25 knots |
| `C` | Calm: average under 3 knots |
| `-` | No forecast: the slot is already past, or beyond the end of the 48-hour run |
| `SE15` (no `G`) | Gust data wasn't available for that run |

**Reading the example:** Saturday starts with a southeast 12 knots gusting 20 around dawn. It builds to a south 18 gusting 28 around midday, then veers southwest and west and eases toward evening. Sunday morning is a light northwesterly. Sunday's last three slots are beyond the run.

### 7.2 Waves (`wv`)

```
Waves 49.15,-125.91 12Z @2km
Fr30 SW0.6/6 SW1.2/9 SW1.8/9 SW2.0/9 W1.6/8
Sa31 W1.4/9 W1.2/9 F F -
```

**Header:** `Waves`, the point, the RDWPS run (UTC), and `@2km` if the forecast is for the nearest model water that far away.

| Code | Meaning |
|---|---|
| `SW1.2/9` | Waves **from** the southwest, 1.2 m significant height (highest in the 3 hours), 9-second period |
| `F` | Flat: under 0.2 m |
| `-` | No forecast for that slot |

Things to keep in mind when reading wave heights:

- **Significant height** is the average of the highest third of the waves. Individual waves well above it are normal.
- The height **includes ocean swell**, so on the outer coast it can be large even in light wind.
- **Period** is the time between main crests:
  - 10 s or more usually means swell from far away;
  - under about 5 s means short, steep local chop, which is often harder going than its height suggests.

### 7.3 Other replies you may see

| Reply | What happened |
|---|---|
| `Send: wx LAT LON for wind, wv LAT LON for waves …` | The request couldn't be read, or plain `wx`/`wv` arrived with no GPS position |
| `… is outside the forecast area (Puget Sd to Skagway, 47-59.6N)` | The point isn't in either region; check the numbers, latitude first |
| `… waves: no wave model water within 5 km …` | A `wv` point in a channel or inlet too narrow for the wave model; try the nearest open water |
| `… no 06-18 slots in this run, try later` | Rare: the newest run doesn't cover any remaining daytime slot |

---

## 8. Timing, end to end

| Stage | Typical time |
|---|---|
| Model run time → data published on R2 | about 5 hours (HRDPS and RDWPS processing at ECCC, then our scheduled bake) |
| inReach sends → email in the bot's inbox | seconds to a few minutes, given open sky |
| Email arrives → bot starts | up to 1 minute (Apps Script check interval) |
| Bot starts → reply sent | 20–40 seconds (start-up, one manifest, one region file) |
| Reply sent → on your inReach | until its next message check |

---

## 9. When something goes wrong: where to look

| Symptom | Where to check |
|---|---|
| No reply at all | GitHub → Actions → **inReach wind bot**: did a run start after the request, and did it log `reply via Garmin link`? If no run started, check the Apps Script's **Executions** page in the bot's Google account. |
| Reply says Garmin failed | The run log shows Garmin's HTTP response. Garmin may have changed its reply page. |
| Old run time in the header | GitHub → Actions → **HRDPS ingest** or **RDWPS waves**: is the latest scheduled run green? |
| Gusts missing | The HRDPS ingest log line `gust label:` shows whether the gust file was found for that run. |
| Bot can't log in to Gmail | The run shows a "Gmail login failed" error with checks on the address and app password. |

---

## 10. File reference

| File | Role |
|---|---|
| `ingest.py` | HRDPS wind + gust bake (also feeds the Salish marine map) |
| `waves_rdwps.py` | RDWPS wave bake for `wv` |
| `inreach_bot.py` | Reads the inbox, parses requests, builds and sends replies |
| `apps_script/dispatch.gs` | Runs in the bot's Gmail; starts the bot within a minute of new mail |
| `.github/workflows/ingest.yml` | Wind bake schedule (05:05, 11:05, 17:05, 23:05 UTC) |
| `.github/workflows/waves_rdwps.yml` | Wave bake schedule (04:40, 10:40, 16:40, 22:40 UTC) |
| `.github/workflows/inreach.yml` | The bot (started by Apps Script; 10-minute backup schedule) |

**Secrets** (repository settings → Actions):
- `INREACH_GMAIL_USER`, `INREACH_GMAIL_APP_PASSWORD`: the bot's Gmail login
- `R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`: write access to the bucket
- `INREACH_ALLOWED_SENDERS` (optional): restrict who gets answers

The Apps Script stores its GitHub token in its own **Script Properties** as `GITHUB_TOKEN`.
