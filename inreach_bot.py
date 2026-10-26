#!/usr/bin/env python3
"""
inReach wind forecast bot: text a lat/long from a Garmin inReach, get the HRDPS 2.5 km wind back.

REQUEST (sent from the inReach to the bot's Gmail address):
    wx 49.36 -125.72        decimal degrees; the minus on longitude is optional (BC is all west)
    wx                      no coordinates -> use the inReach's own GPS fix from the Garmin email

REPLY (one message, <= 160 chars):
    49.36,-125.72 18Z
    Sa24 SE12G20 SE15G25 S18G28 SW15G22 W10G15
    Su25 NW8G12 NW10G14 W12G18 W9G13 C

    - first line: the point the forecast is for, then the HRDPS run (UTC) it came from
    - each day: weekday + date, then always five slots, local time 06 09 12 15 18
    - SE15G25 = wind FROM the SE at 15 kt, gusting 25 kt;  C = calm (< 3 kt)
    - "-" = a slot already past (today) or beyond the run's 48 h (last day); positions stay fixed

DATA: reads the same Cloudflare R2 files the Salish marine map reads (hrdps/manifest.json +
coast-overview.bin), plus coast-overview.gust.bin baked by ingest.py. Nearest 2.5 km cell.

REPLY ROUTE: inReach emails carry a "send a reply" link (explore.garmin.com/textmessage/txtmsg?
extId=...). The bot answers through that link, which delivers to the device. Requests from an
ordinary email address (handy for testing) are answered by email instead.

Env: GMAIL_USER, GMAIL_APP_PASSWORD, optional R2_PUBLIC (public bucket URL), optional
ALLOWED_SENDERS (comma list; matched against From / Reply-To / body, e.g. your inReach address).

Local checks (no mail needed):
    python inreach_bot.py --point 49.36 -125.72           # print the reply for a point
    python inreach_bot.py --eml sample.eml --dry-run      # parse a saved email, print the reply
"""
import os, re, sys, json, gzip, email, imaplib, smtplib, random, argparse
import datetime as dt
import urllib.request, urllib.parse, http.cookiejar
from email.message import EmailMessage
from email.utils import parseaddr
from zoneinfo import ZoneInfo

R2_PUBLIC = os.environ.get("R2_PUBLIC", "https://pub-cb8eb71f75d64a57a0d021318ccd0ae7.r2.dev")
REGION = "coast-overview"                 # Puget Sound -> Dixon Entrance incl. Haida Gwaii
LOCAL_TZ = ZoneInfo("America/Vancouver")
SLOTS = (6, 9, 12, 15, 18)                # local hours, in reply order
CALM_KT = 3
MAX_LEN = 160
KMH_PER_KT = 1.852
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
HELP = "Send: wx LAT LON  e.g. wx 49.36 -125.72  (or just wx for your GPS spot)"

def log(*a): print(*a, file=sys.stderr, flush=True)

def note(msg, level="notice"):
    """Key events also become GitHub Actions annotations (visible on the run page)."""
    log(msg)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level}::" + msg.replace("%", "%25").replace("\r", "").replace("\n", "%0A"), flush=True)

# ---------------------------------------------------------------- forecast data
def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "inreach-wx-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
    return gzip.decompress(b) if b[:2] == b"\x1f\x8b" else b

class Forecast:
    """The latest HRDPS bake for one region, loaded once per bot run."""
    def __init__(self, base=R2_PUBLIC, loader=fetch):
        self.man = json.loads(loader(f"{base}/hrdps/manifest.json"))
        reg = self.man["regions"][REGION]
        self.bbox, self.cols, self.rows = reg["bbox"], reg["cols"], reg["rows"]
        self.span = self.cols * self.rows
        self.wind = loader(f"{base}/hrdps/{reg['file']}")
        nh = len(self.man["hours"])
        if len(self.wind) != nh * 2 * self.span:
            raise RuntimeError(f"wind file is {len(self.wind)} B, expected {nh * 2 * self.span}")
        self.gust = None
        gfile = self.man.get("gust", {}).get("regions", {}).get(REGION)
        if gfile:
            try:
                g = loader(f"{base}/hrdps/{gfile}")
                if len(g) == nh * self.span:
                    self.gust = g
                else:
                    log(f"gust file wrong size ({len(g)} B), ignoring")
            except Exception as e:
                log(f"gust file unavailable: {e}")
        self.hour_index = {dt.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc): i
                           for i, t in enumerate(self.man["hours"])}
        self.run = dt.datetime.strptime(self.man["run"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)

    def cell(self, lat, lon):
        W, S, E, N = self.bbox
        if not (W <= lon <= E and S <= lat <= N):
            return None
        c = round((lon - W) / (E - W) * (self.cols - 1))
        r = round((N - lat) / (N - S) * (self.rows - 1))
        return r * self.cols + c

    def at(self, hour_i, cell):
        """(speed_kt, dir_from_deg, gust_kt or None); speed None if no data."""
        base = hour_i * 2 * self.span
        s = self.wind[base + cell]
        if s == 255:
            return None, None, None
        d = self.wind[base + self.span + cell] * 360 / 255
        g = None
        if self.gust is not None:
            gb = self.gust[hour_i * self.span + cell]
            g = None if gb == 255 else gb / KMH_PER_KT
        return s / KMH_PER_KT, d, g

# ---------------------------------------------------------------- shorthand
def slot_code(kt, deg, gust):
    if kt is None:
        return "-"
    k = round(kt)
    if k < CALM_KT:
        return "C"
    code = f"{COMPASS[round(deg / 22.5) % 16]}{k}"
    if gust is not None:
        code += f"G{max(round(gust), k)}"
    return code

def build_reply(fc, lat, lon, now=None):
    """Return a list of message strings (normally one)."""
    cell = fc.cell(lat, lon)
    if cell is None:
        W, S, E, N = fc.bbox
        return [f"{lat:.2f},{lon:.2f} is outside the forecast area ({S:.0f}-{N:.0f}N {-E:.0f}-{-W:.0f}W)"]
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(LOCAL_TZ)
    # Start today unless today's last slot is already over; slots more than an hour gone show "-".
    day = now.date() if now.hour <= SLOTS[-1] else now.date() + dt.timedelta(days=1)
    cutoff = now - dt.timedelta(hours=1)
    lines = []
    while True:
        idx = []
        for h in SLOTS:
            t = dt.datetime(day.year, day.month, day.day, h, tzinfo=LOCAL_TZ).astimezone(dt.timezone.utc)
            idx.append(fc.hour_index.get(t) if t >= cutoff else None)
        if all(i is None for i in idx):
            break
        # "-" marks a slot already past (today) or beyond the run's 48 h (last day), so the
        # 06/09/12/15/18 positions stay fixed.
        codes = [slot_code(*fc.at(i, cell)) if i is not None else "-" for i in idx]
        lines.append(f"{DAYS[day.weekday()]}{day.day:02d} " + " ".join(codes))
        day += dt.timedelta(days=1)
    header = f"{lat:.2f},{lon:.2f} {fc.run:%H}Z"
    if not lines:
        return [header + " no 06-18 slots in this run, try later"]
    # pack days into <=160-char messages, header on the first
    msgs, cur = [], header
    for ln in lines:
        if len(cur) + 1 + len(ln) <= MAX_LEN:
            cur += "\n" + ln
        else:
            msgs.append(cur); cur = ln
    msgs.append(cur)
    return msgs

# ---------------------------------------------------------------- parsing requests
CMD_RE = re.compile(r"(?i)(?<![a-z])wx\b[\s:]*(?:(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?))?")
GPS_RE = re.compile(r"(?i)\blat(?:itude)?[:\s]+(-?\d{1,2}\.\d+)[^\d-]+lon(?:gitude)?[:\s]+(-?\d{1,3}\.\d+)")
LINK_RE = re.compile(r"https://[^\s\"'<>]*garmin\.com/textmessage/txtmsg\?[^\s\"'<>]*extId=[^\s\"'<>]+", re.I)

def message_text(msg):
    """Plain text of an email (falls back to de-tagged HTML)."""
    parts = msg.walk() if msg.is_multipart() else [msg]
    plain, html = [], []
    for p in parts:
        ct = p.get_content_type()
        if ct not in ("text/plain", "text/html") or p.get_filename():
            continue
        payload = p.get_payload(decode=True) or b""
        txt = payload.decode(p.get_content_charset() or "utf-8", "replace")
        (plain if ct == "text/plain" else html).append(txt)
    if plain:
        return "\n".join(plain)
    return re.sub(r"<[^>]+>", " ", "\n".join(html)).replace("&amp;", "&")

def parse_request(text):
    """-> (lat, lon) | 'gps-missing' | None (not a wx request)."""
    # Only the user's own message: Garmin appends the reply link and GPS line below it.
    head = re.split(r"(?i)view the location|send a reply|sent this message from", text)[0]
    m = CMD_RE.search(head)
    if not m:
        return None
    if m.group(1):
        lat, lon = float(m.group(1)), float(m.group(2))
    else:
        g = GPS_RE.search(text)
        if not g:
            return "gps-missing"
        lat, lon = float(g.group(1)), float(g.group(2))
    if lon > 0:                                   # "wx 49.36 125.72": BC/WA are all west
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return "gps-missing"
    return lat, lon

def reply_link(text):
    m = LINK_RE.search(text)
    return m.group(0).replace("&amp;", "&") if m else None

# ---------------------------------------------------------------- sending
def send_garmin(link, body, reply_addr):
    """Answer through the inReach web reply form, which delivers to the device."""
    u = urllib.parse.urlparse(link)
    ext_id = urllib.parse.parse_qs(u.query)["extId"][0]
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (inreach-wx-bot)")]
    op.open(link, timeout=60).read()                      # session cookie, as a browser would
    data = urllib.parse.urlencode({"ReplyAddress": reply_addr, "ReplyMessage": body,
                                   "MessageId": str(random.randint(10_000_000, 99_999_999)),
                                   "Guid": ext_id}).encode()
    req = urllib.request.Request(f"{u.scheme}://{u.netloc}/TextMessage/TxtMsg", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Referer": link})
    with op.open(req, timeout=60) as r:
        resp = r.read().decode("utf-8", "replace")
    if r.status != 200 or "error" in resp.lower()[:200]:
        raise RuntimeError(f"Garmin reply failed: HTTP {r.status} {resp[:200]}")

def send_email(to, subject, body, user, pw):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = user, to, "Re: " + (subject or "wx")
    m.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw); s.send_message(m)

# ---------------------------------------------------------------- main loop
def handle(msg, fc_loader, allowed):
    """-> (list_of_reply_strings or None, link or None, reply_to address)"""
    text = message_text(msg)
    link = reply_link(text)
    sender = parseaddr(msg.get("Reply-To") or msg.get("From") or "")[1]
    if allowed:
        hay = " ".join([msg.get("From", ""), msg.get("Reply-To", ""), text]).lower()
        if not any(a in hay for a in allowed):
            note(f"ignored: sender not allowed ({sender})")
            return None, link, sender
    req = parse_request(text)
    if req is None:
        # Not a command. Answer with help only if it came from a device (costs them a message).
        return ([HELP] if link else None), link, sender
    if req == "gps-missing":
        return [HELP], link, sender
    return build_reply(fc_loader(), *req), link, sender

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--point", nargs=2, type=float, metavar=("LAT", "LON"))
    ap.add_argument("--eml", help="process one saved .eml file instead of the inbox")
    ap.add_argument("--dry-run", action="store_true", help="print replies, send nothing, mark nothing read")
    args = ap.parse_args()

    fc_cache = {}
    def fc_loader():
        if "fc" not in fc_cache:
            fc_cache["fc"] = Forecast()
        return fc_cache["fc"]

    if args.point:
        print("\n---\n".join(build_reply(fc_loader(), *args.point)))
        return
    allowed = [a.strip().lower() for a in os.environ.get("ALLOWED_SENDERS", "").split(",") if a.strip()]
    if args.eml:
        msg = email.message_from_bytes(open(args.eml, "rb").read())
        replies, link, sender = handle(msg, fc_loader, allowed)
        print(f"via: {'Garmin ' + link if link else 'email ' + sender}")
        print("\n---\n".join(replies or ["(no reply)"]))
        return

    user, pw = os.environ.get("GMAIL_USER", ""), os.environ.get("GMAIL_APP_PASSWORD", "")
    if not (user and pw):
        log("GMAIL_USER / GMAIL_APP_PASSWORD not set: nothing to do (add the repo secrets to enable)")
        return                                    # exit 0: no failure emails before setup
    try:
        im = imaplib.IMAP4_SSL("imap.gmail.com")
        im.login(user, pw.replace(" ", ""))
    except Exception as e:
        u = user.strip().lower()
        note(f"Gmail login failed (address {len(user)} chars, matches hrdpsforecast@gmail.com: "
             f"{u == 'hrdpsforecast@gmail.com'}, stray spaces: {user != user.strip()}; password "
             f"{len(pw.replace(' ', ''))} chars, letters only: {pw.replace(' ', '').isalpha()}): {e}", "error")
        sys.exit(1)
    im.select("INBOX")
    _, data = im.search(None, "UNSEEN")
    ids = data[0].split()
    note(f"Gmail login ok as {user}: {len(ids)} unread")
    failures = 0
    for mid in ids:
        _, d = im.fetch(mid, "(BODY.PEEK[])")
        msg = email.message_from_bytes(d[0][1])
        note(f"from {msg.get('From')} | {msg.get('Subject')}")
        try:
            replies, link, sender = handle(msg, fc_loader, allowed)
            for body in replies or []:
                note(f"{'DRY RUN would send' if args.dry_run else 'reply'} via {'Garmin' if link else 'email'} ({len(body)} ch):\n{body}")
                if args.dry_run:
                    continue
                if link:
                    send_garmin(link, body, user)
                elif sender:
                    send_email(sender, msg.get("Subject"), body, user, pw)
        except Exception as e:
            failures += 1
            note(f"FAILED: {e}", "error")
        if not args.dry_run:
            im.store(mid, "+FLAGS", "\\Seen")     # never retry: a retry loop would bill the user
    im.logout()
    if failures:
        sys.exit(1)                               # surface in GitHub Actions

if __name__ == "__main__":
    main()
