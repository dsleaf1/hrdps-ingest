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

REPLY ROUTE: inReach emails carry a reply link (inreachlink.com/<id> -> messenger.garmin.com/web/
reply/<id>). The bot answers through that page, which delivers to the device. Garmin bounces plain
email replies, so an inReach email without a link is logged as an error, never emailed. Requests from an
ordinary email address (handy for testing) are answered by email instead.

Env: GMAIL_USER, GMAIL_APP_PASSWORD, optional R2_PUBLIC (public bucket URL), optional
ALLOWED_SENDERS (comma list; matched against From / Reply-To / body, e.g. your inReach address).

Local checks (no mail needed):
    python inreach_bot.py --point 49.36 -125.72           # print the reply for a point
    python inreach_bot.py --eml sample.eml --dry-run      # parse a saved email, print the reply
"""
import os, re, sys, json, gzip, email, email.header, imaplib, smtplib, argparse
import datetime as dt
import urllib.request, urllib.parse, urllib.error, http.cookiejar
from email.message import EmailMessage
from email.utils import parseaddr
from zoneinfo import ZoneInfo

R2_PUBLIC = os.environ.get("R2_PUBLIC", "https://pub-cb8eb71f75d64a57a0d021318ccd0ae7.r2.dev")
# Searched in order; the first whose box holds the point is used. Together: Port Townsend -> Skagway.
REGIONS = ["coast-overview",              # Puget Sound -> Dixon Entrance incl. Haida Gwaii (to 54.5 N)
           "north-passage"]               # Prince Rupert -> Skagway, Sitka, Glacier Bay (54.4-59.6 N)
AREA_TEXT = "Puget Sd to Skagway, 47-59.6N"
LOCAL_TZ = ZoneInfo("America/Vancouver")
ALASKA_TZ = ZoneInfo("America/Juneau")

def local_tz(lat, lon):
    """Alaska time for the panhandle, Pacific elsewhere. The BC-Alaska border is approximated by a
    line from Dixon Entrance / Portland Canal (56 N, 130.0 W) to White Pass (59.5 N, 135.1 W); it
    is off by tens of km inland but every Inside Passage water lands on the right side."""
    if lat < 54.65:
        return LOCAL_TZ
    border = -130.0 if lat < 56 else -130.0 - (lat - 56) * (5.1 / 3.5)
    return ALASKA_TZ if lon < border else LOCAL_TZ
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

class Forecasts:
    """The latest HRDPS bake: manifest read once, each region's files fetched on first use."""
    def __init__(self, base=R2_PUBLIC, loader=fetch):
        self.base, self.loader, self.cache = base, loader, {}
        self.man = json.loads(loader(f"{base}/hrdps/manifest.json"))

    def for_point(self, lat, lon):
        for k in REGIONS:
            reg = self.man["regions"].get(k)
            if not reg:
                continue
            W, S, E, N = reg["bbox"]
            if W <= lon <= E and S <= lat <= N:
                if k not in self.cache:
                    self.cache[k] = Forecast(self.man, k, self.base, self.loader)
                return self.cache[k]
        return None

class Forecast:
    """One region of the latest HRDPS bake."""
    def __init__(self, man, region, base=R2_PUBLIC, loader=fetch):
        self.man = man
        reg = self.man["regions"][region]
        self.bbox, self.cols, self.rows = reg["bbox"], reg["cols"], reg["rows"]
        self.span = self.cols * self.rows
        self.wind = loader(f"{base}/hrdps/{reg['file']}")
        nh = len(self.man["hours"])
        if len(self.wind) != nh * 2 * self.span:
            raise RuntimeError(f"wind file is {len(self.wind)} B, expected {nh * 2 * self.span}")
        self.gust = None
        gfile = self.man.get("gust", {}).get("regions", {}).get(region)
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

def build_reply(fcs, lat, lon, now=None):
    """Return a list of message strings (normally one). fcs: Forecasts (or a single Forecast)."""
    fc = fcs.for_point(lat, lon) if isinstance(fcs, Forecasts) else fcs
    cell = fc.cell(lat, lon) if fc else None
    if cell is None:
        return [f"{lat:.2f},{lon:.2f} is outside the forecast area ({AREA_TEXT})"]
    tz = local_tz(lat, lon)
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    # Start today unless today's last slot is already over; slots more than an hour gone show "-".
    day = now.date() if now.hour <= SLOTS[-1] else now.date() + dt.timedelta(days=1)
    cutoff = now - dt.timedelta(hours=1)
    lines = []
    while True:
        idx = []
        for h in SLOTS:
            t = dt.datetime(day.year, day.month, day.day, h, tzinfo=tz).astimezone(dt.timezone.utc)
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
CMD_RE = re.compile(r"(?i)(?<![a-z])wx\b[\s:]*(?:(-?\d{1,2}(?:\.\d+)?)\s*[ns]?\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*[ew]?\b)?")
GPS_RE = re.compile(r"(?i)\blat(?:itude)?[:\s]+(-?\d{1,2}\.\d+)[^\d-]+lon(?:gitude)?[:\s]+(-?\d{1,3}\.\d+)")
LINK_RE = re.compile(r"https://(?:inreachlink\.com/[A-Za-z0-9_-]+|[^\s\"'<>]*garmin\.com/textmessage/txtmsg\?"
                     r"[^\s\"'<>]*extId=[^\s\"'<>]+)", re.I)

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
    head = head.replace("\u2212", "-").replace("\u2013", "-").replace("\u00b0", "")  # phone/Mac auto-typography
    head = re.sub(r"(\d)\.\s+(\d)", r"\1.\2", head)            # inReach keyboard: "49. 36" -> "49.36"
    m = CMD_RE.search(head)
    if not m:
        return None
    if m.group(1):
        lat, lon = float(m.group(1)), float(m.group(2))
    elif re.search(r"\d", head[m.end():m.end() + 30]):
        return "gps-missing"                      # numbers typed but unreadable: ask, don't guess
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
def open_reply_page(link):
    """Open the reply page like a browser (cookies, redirects). -> (opener, final_url, html)"""
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (inreach-wx-bot)")]
    with op.open(link, timeout=60) as r:
        return op, r.geturl(), r.read().decode("utf-8", "replace")

MESSENGER = "https://messenger.garmin.com"

def send_garmin(link, body, reply_addr=None):
    """Reply through Garmin Messenger's web reply page (messenger.garmin.com/web/reply/<tinyUrlId>),
    exactly as its Send button does: the Next.js server action sendReplyAction(tinyUrlId, text).
    The action ID is read from Garmin's current page bundle each time, so a redeploy doesn't break it."""
    op, final, page = open_reply_page(link)            # inreachlink.com -> messenger.garmin.com
    m = re.search(r'tinyUrlId\\?"\s*:\s*\\?"([A-Za-z0-9_-]{6,})', page)
    tok = m.group(1) if m else (link.rstrip("/").rsplit("/", 1)[-1] if "inreachlink.com" in link else None)
    if not tok:
        raise RuntimeError("reply page has no tinyUrlId")
    page_url = f"{MESSENGER}/web/reply/{tok}"
    if f"/web/reply/{tok}" not in final:
        with op.open(page_url, timeout=60) as r:
            page = r.read().decode("utf-8", "replace")
    cm = re.search(r'static/chunks/app/\(reply\)/reply/%5BtinyUrlId%5D/page-[0-9a-f]+\.js', page)
    if not cm:
        raise RuntimeError("reply page bundle not found (Garmin changed the page?)")
    with op.open(f"{MESSENGER}/web/_next/{cm.group(0)}", timeout=60) as r:
        js = r.read().decode("utf-8", "replace")
    am = re.search(r'createServerReference\)\("([0-9a-f]{20,})"[^;]{0,200}?"sendReplyAction"', js)
    if not am:
        raise RuntimeError("sendReplyAction not found in reply bundle (Garmin changed the page?)")
    tree = ["", {"children": ["(reply)", {"children": ["reply", {"children": [["tinyUrlId", tok, "d"],
            {"children": ["__PAGE__", {}, None, None]}, None, None]}, None, None]}, None, None]}, None, None, True]
    req = urllib.request.Request(page_url, data=json.dumps([tok, body]).encode(), method="POST", headers={
        "Next-Action": am.group(1),
        "Next-Router-State-Tree": urllib.parse.quote(json.dumps(tree, separators=(",", ":"))),
        "Accept": "text/x-component",
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": MESSENGER, "Referer": page_url})
    try:
        with op.open(req, timeout=60) as r:
            status, resp = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Garmin reply failed: HTTP {e.code} {e.read()[:200]!r}")
    if status != 200 or re.search(r'(^|\n)\w+:E\{', resp):
        raise RuntimeError(f"Garmin reply failed: HTTP {status} {resp[:200]!r}")
    note(f"Garmin accepted reply: HTTP {status}, response {resp.replace(tok, '<tok>')[:300]!r}")

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
    if req is None:                               # the command may be in the subject instead
        subj = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
        req = parse_request(subj + "\n" + text)
    if req is None and "google.com" not in sender:
        note(f"not a wx request; body starts: {text.strip()[:80]!r}")
    if req is None:
        # Not a command. Answer with help only if it came from a device (costs them a message).
        return ([HELP] if link else None), link, sender
    if req == "gps-missing":
        return [HELP], link, sender
    return build_reply(fc_loader(), *req), link, sender

def mask(t):
    """Hide location digits and private IDs/tokens so the dump is safe in a public run log."""
    t = re.sub(r"(?i)(lat(?:itude)?|lon(?:gitude)?)([:\s]+)-?\d+(?:\.\d+)?", r"\1\2<num>", t)
    t = re.sub(r"(https?://[^/\s]+/)[^\s\"'<>]*", lambda m: m.group(1) + "<path:" + str(len(m.group(0))) + ">", t)
    t = re.sub(r"[A-Za-z0-9._%+-]+@", "<user>@", t)
    t = re.sub(r"-?\d{1,3}\.\d{3,}", "<num>", t)                  # bare high-precision coords
    return t

def inspect_latest(im):
    _, data = im.search(None, '(FROM "inreach" SUBJECT "inReach message from")')
    ids = data[0].split()
    if not ids:
        note("inspect: no inReach email found"); return
    _, d = im.fetch(ids[-1], "(BODY.PEEK[])")
    msg = email.message_from_bytes(d[0][1])
    hdr = {h: mask(str(msg.get(h, ""))) for h in ("From", "Reply-To", "Sender", "To", "Subject")}
    note("inspect headers: " + json.dumps(hdr))
    for p in (msg.walk() if msg.is_multipart() else [msg]):
        ct = p.get_content_type()
        if ct.startswith("multipart/"):
            continue
        raw = (p.get_payload(decode=True) or b"").decode(p.get_content_charset() or "utf-8", "replace")
        if ct == "text/html":
            raw = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
            links = re.findall(r'href="([^"]+)"', raw)
            note("inspect html links: " + json.dumps([mask(l) for l in links])[:900])
            raw = re.sub(r"<[^>]+>", " ", raw)
            raw = re.sub(r"[ \t]+", " ", raw)
        note(f"inspect part {ct}: " + repr(mask(raw.strip()))[:1500])
    link = reply_link(message_text(msg))
    if not link:
        note("inspect: no reply link found", "warning"); return
    try:
        op, final, page = open_reply_page(link)
        fu = urllib.parse.urlparse(final)
        note(f"inspect link -> {fu.scheme}://{fu.netloc}{fu.path} query keys {sorted(urllib.parse.parse_qs(fu.query))}")
        forms = re.findall(r"<form[^>]*>", page, re.I)
        fields = sorted(set(re.findall(r'<(?:input|textarea)[^>]*name="([^"]+)"', page, re.I)))
        posts = sorted(set(re.findall(r"(?i)(?:url|action)\s*[:=]\s*['\"]([^'\"]*TxtMsg[^'\"]*)", page)))
        note(f"inspect reply page: {len(page)} B, forms {[mask(f) for f in forms][:5]}, fields {fields[:20]}, "
             f"post targets {[mask(x) for x in posts][:5]}, mentions ReplyMessage: {'ReplyMessage' in page}")
        tok = link.rstrip("/").rsplit("/", 1)[-1] if "inreachlink.com" in link else ""
        def m2(x):                                           # mask the private tokens by length
            for t_ in filter(None, [tok] + urllib.parse.parse_qs(fu.query).get("extId", [])):
                x = x.replace(t_, f"<tok{len(t_)}>")
            return x
        note("inspect /reply mentions: " + json.dumps([m2(c) for c in re.findall(r".{0,80}/reply/.{0,120}", page)][:6])[:1500])
        note("inspect tinyUrlId ctx: " + json.dumps([m2(c) for c in re.findall(r".{0,100}tinyUrlId.{0,100}", page)][:6])[:1500])
        note(f"inspect token in page: {tok and tok in page}; token length {len(tok)}")
        for cand in ([f"https://messenger.garmin.com/web/reply/{tok}", f"https://messenger.garmin.com/reply/{tok}"] if tok else []):
            try:
                with op.open(cand, timeout=60) as r2:
                    body2 = r2.read().decode("utf-8", "replace")
                    note(f"inspect GET {m2(cand)} -> {r2.status} {m2(r2.geturl())} {len(body2)} B, "
                         f"has sendReply page chunk: {'reply/%5BtinyUrlId%5D' in body2 or '(reply)' in body2}")
            except Exception as e:
                note(f"inspect GET {m2(cand)} -> {e}")
    except Exception as e:
        note(f"inspect: opening reply link failed: {e}", "warning")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--point", nargs=2, type=float, metavar=("LAT", "LON"))
    ap.add_argument("--eml", help="process one saved .eml file instead of the inbox")
    ap.add_argument("--dry-run", action="store_true", help="print replies, send nothing, mark nothing read")
    ap.add_argument("--inspect", action="store_true",
                    help="print the structure of the latest inReach email (location/IDs masked), then stop")
    ap.add_argument("--answer-latest", action="store_true",
                    help="(re)answer the most recent inReach request even if already read")
    args = ap.parse_args()

    fc_cache = {}
    def fc_loader():
        if "fc" not in fc_cache:
            fc_cache["fc"] = Forecasts()
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
        missing = [n for n, v in (("INREACH_GMAIL_USER", user), ("INREACH_GMAIL_APP_PASSWORD", pw)) if not v]
        note("secret(s) empty or missing: " + ", ".join(missing) + " -- nothing to do", "warning")
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
    if args.inspect:
        inspect_latest(im); im.logout(); return
    if args.answer_latest:
        _, data = im.search(None, '(FROM "inreach" SUBJECT "inReach message from")')
        ids = data[0].split()[-1:]
    else:
        _, data = im.search(None, "UNSEEN")
        ids = data[0].split()
    note(f"Gmail login ok as {user}: {len(ids)} unread")
    failures = 0
    for mid in ids:
        _, d = im.fetch(mid, "(BODY.PEEK[])")
        msg = email.message_from_bytes(d[0][1])
        note(f"request from {'inReach' if 'inreach' in (msg.get('From') or '').lower() else 'email'}")
        try:
            replies, link, sender = handle(msg, fc_loader, allowed)
            for body in replies or []:
                note(f"{'DRY RUN would send' if args.dry_run else 'reply'} via {'Garmin link' if link else 'email'} "
                     f"({len(body)} ch):\n" + body.split("\n", 1)[-1])     # forecast only, no position
                if args.dry_run:
                    continue
                if link:
                    send_garmin(link, body, user)
                elif "garmin.com" in sender.lower() or "inreach" in sender.lower():
                    note("inReach email without a reply link: cannot reply", "error")
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
