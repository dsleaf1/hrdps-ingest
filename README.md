# hrdps-ingest

Scheduled pipeline that turns ECCC's operational **HRDPS Continental 2.5 km** wind
GRIB2 into compact, browser-ready per-region files for the BC-coast kayaker wind map.

Why this exists: ECCC's browser-accessible services (Open-Meteo, GeoMet WCS) only
deliver HRDPS reprojected to **~3.7 km**. The true **2.5 km** field lives only in the
raw GRIB2 (rotated grid `RLatLon0.0225`), which a browser can't decode. This job does
the decoding off-line and publishes small binaries the map loads directly.

## What it does (4×/day)

1. Finds the latest complete HRDPS Continental run on Datamart.
2. Downloads 10 m `WIND` (speed) + `WDIR` (direction) GRIB2 for forecast hours 000–048.
3. Regrids each region to a regular ~2.5 km lat/lon grid (nearest-neighbour).
4. Byte-packs speed (km/h; 255 = nodata) + direction (0–360 → 0–255) per hour.
5. gzips and uploads per-region `.bin` files + `manifest.json` to Cloudflare R2.

The map app reads `hrdps/manifest.json` then `hrdps/{region}.bin` from the R2 public URL.

## Output format

`manifest.json`: `{ model, run, generated, res_deg, nodata, hours[], regions{ key: {name, bbox:[W,S,E,N], cols, rows, file} } }`

`{region}.bin`: for each hour in `hours`, `rows*cols` speed bytes then `rows*cols`
direction bytes (row 0 = north). Hour offset = `h * 2 * rows * cols`.
Decode: `kmh = speed_byte` (255 ⇒ no data); `deg = dir_byte * 360 / 255`.

## One-time setup

### 1. GitHub repo
```
cd hrdps-ingest
git remote add origin git@github.com:<you>/hrdps-ingest.git   # create the empty repo first
git push -u origin main
```
(Use a **public** repo for unlimited free Actions minutes; secrets stay encrypted.)

### 2. Cloudflare R2
- Create a bucket (e.g. `hrdps`).
- **Settings → Public access:** enable the `r2.dev` URL **or** connect a custom domain. Note the public base URL → this is where the app reads from.
- **Settings → CORS policy:** allow `GET` from your app origins (or `*` for testing):
  ```json
  [{"AllowedOrigins":["*"],"AllowedMethods":["GET"],"AllowedHeaders":["*"]}]
  ```
- **Manage R2 API Tokens → Create token** (Object Read & Write). Note the
  **Access Key ID**, **Secret Access Key**, and your account's S3 endpoint
  `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`.

### 3. GitHub Actions secrets
Repo → Settings → Secrets and variables → Actions → New repository secret:
- `R2_ENDPOINT` = `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`
- `R2_BUCKET` = `hrdps`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`

### 4. First run
Actions → **HRDPS 2.5 km ingest** → **Run workflow**. Then check the R2 bucket for
`hrdps/manifest.json` and the region `.bin` files.

## Local smoke test (needs eccodes installed)
```
pip install -r requirements.txt        # plus a system eccodes (e.g. brew install eccodes)
python ingest.py --no-upload --regions haida-gwaii --max-hours 3
# -> ./out/manifest.json + ./out/haida-gwaii.bin
```
