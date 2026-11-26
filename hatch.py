#!/usr/bin/env python3
"""Pets Universe hatch tracker.

Opens the Hatches page filtered to your accounts, finds hatches it hasn't seen
before, opens each one for the full details, and posts a Discord embed
(pet icon + player avatar) that pings you.

Env:
  HATCH_QUERY          search text (what you type in the page search box)   [required]
  DISCORD_WEBHOOK_URL  Discord webhook (or HATCH_WEBHOOK_URL to use another) [required]
  DISCORD_USER_ID      your Discord user id, used for the @ping              [optional]
"""
import base64
import json
import os
import uuid
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote, urljoin

BASE = "https://petsuniverse.org"
QUERY = os.environ.get("HATCH_QUERY", "").strip()
WEBHOOK = os.environ.get("HATCH_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL", "")
USER_ID = os.environ.get("DISCORD_USER_ID", "").strip()
SEEN = os.environ.get("HATCH_SEEN", "data/seen_hatches.json")
MAX_SEND = 10
PREVIEW = os.environ.get("PREVIEW", "").strip().lower() == "true"
try:  # preview: how many DIFFERENT pets to send a test alert for
    PREVIEW_COUNT = max(1, min(MAX_SEND, int(os.environ.get("PREVIEW_COUNT") or 1)))
except ValueError:
    PREVIEW_COUNT = 1
KEEP = 500

TITLE_RE = re.compile(r"^(?P<name>.+?)\s+#(?P<serial>[\d,]+)$")
FROM_RE = re.compile(r"^(?P<acct>.+?)\s+from\s+(?P<egg>.+?),\s*(?P<ago>[^,]+)$")
COLORS = {"secret": 0x9B59B6, "exclusive": 0xF1C40F, "mythic": 0xE91E63,
          "legendary": 0xE67E22, "epic": 0x3498DB}

ROWS_JS = r"""() => {
  const re = /^.{1,80}\s#[\d,]+$/;
  const ok = e => re.test((e.textContent || '').trim());
  const hit = [...document.querySelectorAll('body *')].filter(e => ok(e) && ![...e.children].some(ok));
  const used = new Set(); const out = [];
  hit.forEach(t => {
    let p = t;
    while (p.parentElement && hit.filter(h => p.parentElement.contains(h)).length === 1) p = p.parentElement;
    if (used.has(p)) return;
    used.add(p);
    const idx = out.length;
    t.setAttribute('data-htitle', idx);
    p.setAttribute('data-hrow', idx);
    out.push({idx, title: t.textContent.trim(), text: p.innerText,
      imgs: [...p.querySelectorAll('img')].map(i => { const b = i.getBoundingClientRect();
        return {src: i.currentSrc || i.src, w: Math.round(b.width), h: Math.round(b.height)}; })});
  });
  return out;
}"""

MODAL_JS = r"""() => {
  const lab = [...document.querySelectorAll('body *')].find(e =>
    e.children.length === 0 && /^Hatched from$/i.test((e.textContent || '').trim()));
  if (!lab) return null;
  const sz = i => { const b = i.getBoundingClientRect();
    return {src: i.currentSrc || i.src, w: Math.round(b.width), h: Math.round(b.height)}; };
  let box = lab.closest('[role=dialog],dialog,[aria-modal=true]');
  if (!box) {
    box = lab;
    for (let n = 0; n < 12 && box.parentElement; n++) {
      box = box.parentElement;
      const t = box.innerText || '';
      if (/Serial/.test(t) && /Total Eggs/i.test(t) && box.querySelectorAll('img').length >= 2) break;
    }
  }
  document.querySelectorAll('[data-hbox]').forEach(e => e.removeAttribute('data-hbox'));
  box.setAttribute('data-hbox', '1');
  const imgs = [...box.querySelectorAll('img')].map(sz);
  let avatar = null;
  const at = [...box.querySelectorAll('*')].find(e =>
    e.children.length === 0 && /^@\S+$/.test((e.textContent || '').trim()));
  if (at) {
    let p = at;
    while (p && p !== box) {
      const im = p.querySelector('img');
      if (im) { avatar = im.currentSrc || im.src; break; }
      p = p.parentElement;
    }
  }
  return {text: box.innerText, imgs, avatar};
}"""


# Collects every picture inside a root element: <img> tags AND CSS background
# images. Each one is tagged with data-hpet so it can be screenshotted later.
PICS_JS = r"""(sel) => {
  const root = document.querySelector(sel);
  if (!root) return [];
  const out = [];
  const visit = e => {
    const b = e.getBoundingClientRect();
    const w = Math.round(b.width), h = Math.round(b.height);
    let src = null, kind = null;
    if (e.tagName === 'IMG') { src = e.currentSrc || e.src; kind = 'img'; }
    else {
      const bg = getComputedStyle(e).backgroundImage || '';
      const m = bg.match(/url\(["']?(.*?)["']?\)/);
      if (m) { src = m[1]; kind = 'bg'; }
    }
    if (src && w >= 20 && h >= 20) {
      const id = 'p' + Math.random().toString(36).slice(2, 9);
      e.setAttribute('data-hpet', id);
      out.push({id, src, kind, w, h});
    }
  };
  visit(root);
  root.querySelectorAll('*').forEach(visit);
  return out;
}"""


# Copies an already-loaded <img> out of the page as a transparent PNG
# (same-origin, so the canvas is not tainted). Works even when the link is 403 for us.
CANVAS_JS = r"""(id) => {
  const i = document.querySelector(`[data-hpet="${id}"]`);
  if (!i || i.tagName !== 'IMG' || !i.complete || !i.naturalWidth) return null;
  const c = document.createElement('canvas');
  c.width = i.naturalWidth; c.height = i.naturalHeight;
  c.getContext('2d').drawImage(i, 0, 0);
  try { return c.toDataURL('image/png'); } catch (e) { return 'ERR ' + e; }
}"""

def check_image(url):
    """Can Discord show this picture? (fetched from the runner itself)"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            body = r.read(200000)
            ok = (r.status == 200 and ctype.startswith("image/") and "svg" not in ctype
                  and "avif" not in ctype and len(body) > 800)
            print(f"    check {url[:90]} -> {r.status} {ctype} {len(body)}B {'OK' if ok else 'no'}")
            return ok
    except Exception as e:
        print(f"    check {url[:90]} -> failed: {str(e)[:80]}")
        return False


def data_uri_bytes(src):
    m = re.match(r"data:image/(png|jpe?g|webp|gif);base64,(.+)", src, re.S)
    if not m:
        return None
    try:
        raw = base64.b64decode(m.group(2))
        return raw if len(raw) > 800 else None
    except Exception:
        return None


def resolve_pet(pg, key, row_idx, avatar):
    """Find the pet picture: a URL Discord can load, or image bytes to attach.
    Looks in the open popup first, then in the list row. Returns (url, png_bytes)."""
    cands = []
    for where, sel in (("popup", '[data-hbox="1"]'), ("row", f'[data-hrow="{row_idx}"]')):
        try:
            for c in pg.evaluate(PICS_JS, sel):
                c["where"] = where
                cands.append(c)
        except Exception as e:
            print("  pic scan failed in", where, str(e)[:80])
    cands = [c for c in cands if c["src"] and c["src"] != avatar and "headshot" not in c["src"].lower()
             and "/assets/game/ui/" not in c["src"]]  # ui/ = rarity stars & badges, never the pet
    cands.sort(key=lambda c: (c["where"] != "popup", -c["w"] * c["h"]))
    print(f"pet candidates for {key}: " + json.dumps(
        [{k: (v[:100] if isinstance(v, str) else v) for k, v in c.items()} for c in cands])[:1500])
    for c in cands:
        raw = data_uri_bytes(c["src"])
        if raw:
            print("  using embedded data picture", c["id"])
            return None, raw
        if c["src"].startswith(("data:", "blob:")):
            continue
        url = urljoin(BASE, c["src"])
        if check_image(url):
            return url, None
        # link blocked for outsiders (e.g. img.php?a=... -> 403): copy THIS picture out of
        # the page (transparent PNG), else screenshot it - never fall through to a wrong image
        try:
            got = pg.evaluate(CANVAS_JS, c["id"])
            raw = data_uri_bytes(got or "")
            if raw:
                print(f"  link blocked, copied picture from page {c['id']} ({len(raw)}B)")
                return None, raw
            print("  canvas copy failed:", (got or "not loaded")[:80])
        except Exception as e:
            print("  canvas copy failed:", str(e)[:80])
        try:
            png = pg.locator(f'[data-hpet="{c["id"]}"]').first.screenshot(timeout=4000)
            if png and len(png) > 800:
                print("  link blocked, using screenshot of", c["where"], c["kind"], c["id"])
                return None, png
        except Exception as e:
            print("  screenshot failed", c["id"], str(e)[:80])
    for c in cands:  # last resort: screenshot of the picture element
        try:
            png = pg.locator(f'[data-hpet="{c["id"]}"]').first.screenshot(timeout=4000)
            if png and len(png) > 800:
                print("  using screenshot of", c["where"], c["kind"], c["id"])
                return None, png
        except Exception as e:
            print("  screenshot failed", c["id"], str(e)[:80])
    print("  no usable pet picture for", key)
    return None, None


# ------------------------------------------------------------------ parsing
def parse_row(d):
    m = TITLE_RE.match(d["title"])
    if not m:
        return None
    lines = [l.strip() for l in d["text"].splitlines() if l.strip()]
    acct = egg = ago = ""
    extras = []
    for l in lines:
        if l == d["title"] or TITLE_RE.match(l):
            continue
        f = FROM_RE.match(l)
        if f and not acct:
            acct, egg, ago = f.group("acct"), f.group("egg"), f.group("ago")
            continue
        extras.append(l)
    imgs = sorted(d["imgs"], key=lambda i: i["w"] * i["h"], reverse=True)
    return {"key": f"{m['name']}#{m['serial']}", "name": m["name"], "serial": m["serial"],
            "acct": acct, "egg": egg, "ago": ago, "extras": extras, "idx": d["idx"],
            "icon": urljoin(BASE, imgs[0]["src"]) if imgs and imgs[0]["src"] else None}


def parse_modal(m):
    lines = [l.strip() for l in m["text"].splitlines() if l.strip()]

    def after(label):
        for i, l in enumerate(lines):
            if l.lower() == label.lower() and i + 1 < len(lines):
                return lines[i + 1]
        return ""

    out = {"hatched_from": after("Hatched from"), "when": after("When"),
           "eggs": after("Total Eggs Opened"), "value": after("Value"),
           "username": "", "display": ""}
    for i, l in enumerate(lines):
        if l.startswith("@") and len(l) > 1:
            out["username"] = l[1:]
            out["display"] = lines[i - 1] if i > 0 else ""
            break
    imgs = sorted(m["imgs"], key=lambda i: i["w"] * i["h"], reverse=True)
    out["pet_icon"] = urljoin(BASE, imgs[0]["src"]) if imgs and imgs[0]["src"] else None
    out["avatar"] = urljoin(BASE, m["avatar"]) if m.get("avatar") else None
    return out


# ------------------------------------------------------------------ scraping
def preview_rows(rows):
    """Newest hatch of each different pet name, up to PREVIEW_COUNT."""
    out, names = [], set()
    for r in rows:
        if r["name"] not in names:
            names.add(r["name"])
            out.append(r)
        if len(out) >= PREVIEW_COUNT:
            break
    return out

def scrape(seen):
    """Returns (all_rows, details_for_new) where details is {key: modal_dict}."""
    from playwright.sync_api import sync_playwright
    os.makedirs("data", exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 430, "height": 1800}, timezone_id="Europe/Stockholm")
        pg = ctx.new_page()
        pg.goto(f"{BASE}/hatches?q={quote(QUERY)}", wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(3000)
        raw = pg.evaluate(ROWS_JS)
        rows = [r for r in (parse_row(d) for d in raw) if r]
        print(f"rows parsed: {len(rows)}")
        for r in rows[:3]:
            print("  sample:", json.dumps(r, ensure_ascii=False))
        if not rows:
            print("--- page text (first 40 lines) ---")
            print("\n".join(pg.inner_text("body").splitlines()[:40]))
        pg.screenshot(path="data/last_hatches.png")
        details = {}
        if seen is not None or PREVIEW:
            fresh = preview_rows(rows) if PREVIEW else [r for r in rows if r["key"] not in seen][:MAX_SEND]
            for r in reversed(fresh):  # oldest first
                got = None
                for how in ("dom", "force"):
                    try:
                        loc = pg.locator(f'[data-htitle="{r["idx"]}"]').first
                        if how == "dom":
                            loc.evaluate("e => e.click()")
                        else:
                            loc.click(timeout=3000, force=True)
                        pg.wait_for_selector("text=Hatched from", timeout=4000)
                        got = pg.evaluate(MODAL_JS)
                    except Exception as e:
                        print(how, "popup attempt failed:", str(e).splitlines()[0][:100])
                    if got:
                        print("popup opened via", how)
                        break
                    pg.keyboard.press("Escape")
                    pg.wait_for_timeout(500)
                if got:
                    details[r["key"]] = parse_modal(got)
                    url, png = resolve_pet(pg, r["key"], r["idx"], got.get("avatar"))
                    details[r["key"]]["pet_url"], details[r["key"]]["pet_png"] = url, png
                    if len(details) == 1:
                        print("--- first popup text ---")
                        print(got["text"][:600])
                        print("popup images:", json.dumps(got["imgs"])[:300], "avatar:", got.get("avatar"))
                else:
                    print("no popup details for", r["key"])
                    url, png = resolve_pet(pg, r["key"], r["idx"], None)
                    details[r["key"]] = {"pet_url": url, "pet_png": png}
                try:
                    pg.keyboard.press("Escape")
                    pg.wait_for_timeout(500)
                except Exception:
                    pass
        b.close()
    return rows, details


# ------------------------------------------------------------------ discord
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
UA = "Mozilla/5.0 (compatible; pu-hatches/1.0)"
_HEADSHOTS = {}


def hatch_time(when):
    """'22 Nov 2026, 19:43' (Stockholm) -> ISO UTC string, or None."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})\D+(\d{1,2}):(\d{2})", when or "")
    if not m or m.group(2).lower() not in MONTHS:
        return None
    d, mon, y, h, mi = m.groups()
    dt = datetime(int(y), MONTHS[mon.lower()], int(d), int(h), int(mi), tzinfo=ZoneInfo("Europe/Stockholm"))
    return dt.astimezone(timezone.utc).isoformat()


def roblox_headshot(username):
    """Public Roblox API: username -> user id -> headshot image URL."""
    if not username:
        return None
    if username in _HEADSHOTS:
        return _HEADSHOTS[username]
    url = None
    try:
        req = urllib.request.Request(
            "https://users.roblox.com/v1/usernames/users",
            data=json.dumps({"usernames": [username], "excludeBannedUsers": False}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            uid = json.load(r)["data"][0]["id"]
        req = urllib.request.Request(
            f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={uid}"
            "&size=150x150&format=Png&isCircular=false", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            url = json.load(r)["data"][0].get("imageUrl")
    except Exception as e:
        print("roblox headshot failed for", username, str(e)[:100])
    _HEADSHOTS[username] = url
    return url


def abbr(text):
    """'40,217,145' -> '40.2M' (non-numeric text is returned unchanged)."""
    t = (text or "").replace(",", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", t):
        return text
    v = float(t)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            x = v / div
            return (f"{x:.1f}".rstrip("0").rstrip(".") if x < 100 else f"{x:.0f}") + suf
    return t


def build_embed(r, m):
    m = m or {}
    who = m.get("display") or r["acct"] or "?"
    user = m.get("username", "")
    blob = " ".join(r["extras"])
    rarity = next((e for e in r["extras"] if e.lower() in COLORS), "")
    mo = re.search(r"1/[\d,]+", blob)
    odds = mo.group(0) if mo else ""
    ml = re.search(r"\d+(?:\.\d+)?x luck", blob, re.I)
    luck = ml.group(0) if ml else ""
    egg = m.get("hatched_from") or r["egg"]
    head = f"{rarity} " if rarity else ""
    lines = [f"**{who} hatched a {head}{r['name']}!**", ""]
    if egg:
        lines.append(f"\U0001F95A **Egg:** `{egg}`")
    lines.append(f"\u2B50 **Serial:** `#{r['serial']}`")
    if m.get("value"):
        lines.append(f"\U0001F48E **Value:** `{abbr(m['value'])}`")
    if odds:
        lines.append(f"\U0001F3B2 **Odds:** `1/{abbr(odds[2:])}`" + (f"  \u00B7  **Luck:** `{luck.split('x')[0]}x`" if luck else ""))
    elif luck:
        lines.append(f"\U0001F340 **Luck:** `{luck.split('x')[0]}x`")
    if m.get("eggs"):
        lines += ["", "\U0001F4D8 **Player's Stats:**", "Total Eggs Opened:", f"`{abbr(m['eggs'])}`"]
    emb = {"description": "\n".join(lines),
           "color": COLORS.get(rarity.lower(), 0x5865F2),
           "footer": {"text": "Pets Universe \u2022 Hatches"},
           "timestamp": hatch_time(m.get("when")) or datetime.now(timezone.utc).isoformat()}
    author = {"name": f"{who} (@{user})" if user else who}
    icon = roblox_headshot(user) or m.get("avatar")
    if icon:
        author["icon_url"] = icon
    emb["author"] = author
    if m.get("pet_png"):
        emb["thumbnail"] = {"url": "attachment://pet.png"}
    elif m.get("pet_url"):
        emb["thumbnail"] = {"url": m["pet_url"]}
    elif "pet_url" not in m and (m.get("pet_icon") or r["icon"]):  # old fallback
        emb["thumbnail"] = {"url": m.get("pet_icon") or r["icon"]}
    return emb


def card_data(r, m):
    """Everything the picture card shows, from the row, the popup and the site's value list."""
    import card
    m = m or {}
    blob = " ".join(r["extras"])
    rarity = next((e for e in r["extras"] if e.lower() in COLORS), "")
    mo = re.search(r"1/[\d,]+", blob)
    ml = re.search(r"(\d+(?:\.\d+)?)x luck", blob, re.I)
    it = card.lookup(r["name"])
    user = m.get("username", "")
    return {
        "name": r["name"], "serial": r["serial"], "rarity": rarity,
        "when": (m.get("when") or "").replace(", ", " · "),
        # the popup's Value first (what the site shows), the value list's RAP if the popup had none
        "value": m.get("value") or card.short(it.get("rap")),
        "exists": card.short(it.get("ex")),
        "odds": card.odds_text(mo.group(0) if mo else "", it.get("od")),
        "luck": f"{ml.group(1)}x" if ml else None,
        "display": m.get("display") or r["acct"], "username": user,
        "avatar": roblox_headshot(user) or m.get("avatar"),
        "egg": m.get("hatched_from") or r["egg"],
        "eggs_opened": abbr(m["eggs"]) if m.get("eggs") else None,
        "pet_src": card.to_data_uri(m.get("pet_png"), m.get("pet_url") or m.get("pet_icon") or r.get("icon")),
    }


def make_cards(rs, details):
    """PNG card per row (None where it failed - that alert falls back to the text embed)."""
    try:
        import card
        pngs = card.render([card_data(r, details.get(r["key"])) for r in rs])
        print("cards rendered:", sum(1 for p in pngs if p), "/", len(rs))
        return pngs
    except Exception as e:
        print("cards failed, using text alerts:", str(e)[:150])
        return [None] * len(rs)


def send_alert(r, m, card_png, content):
    """Picture card if we have one, else the classic text embed."""
    if card_png:
        rarity = next((e for e in r["extras"] if e.lower() in COLORS), "")
        emb = {"color": COLORS.get(rarity.lower(), 0x5865F2), "image": {"url": "attachment://card.png"},
               "timestamp": hatch_time((m or {}).get("when")) or datetime.now(timezone.utc).isoformat()}
        return post(content, emb, card_png, "card.png")
    return post(content, build_embed(r, m), (m or {}).get("pet_png"))


def post(content, embed=None, png=None, fname="pet.png"):
    payload = {"content": content}
    if embed:
        payload["embeds"] = [embed]
    if USER_ID:
        payload["allowed_mentions"] = {"users": [USER_ID]}
    if png:  # embed picture sent as an attachment (attachment://pet.png)
        bnd = uuid.uuid4().hex
        head = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
                f"Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n"
                f"--{bnd}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"{fname}\"\r\n"
                f"Content-Type: image/png\r\n\r\n")
        body = head.encode() + png + f"\r\n--{bnd}--\r\n".encode()
        req = urllib.request.Request(WEBHOOK, data=body, headers={
            "Content-Type": f"multipart/form-data; boundary={bnd}", "User-Agent": "pu-hatches/1.0"})
    else:
        req = urllib.request.Request(WEBHOOK, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "User-Agent": "pu-hatches/1.0"})
    for attempt in range(4):  # bursts: Discord may answer 429 "slow down" - wait as told, then retry
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as e:
            if attempt == 3 or not (e.code == 429 or e.code >= 500):
                raise
            wait = 2.0
            try:
                wait = float(json.loads(e.read() or b"{}").get("retry_after", wait))
            except Exception:
                pass
            print(f"discord {e.code}, retrying in {wait:.1f}s")
            time.sleep(min(wait, 30) + 0.5)


def ping():
    return f"<@{USER_ID}> " if USER_ID else ""


# ------------------------------------------------------------------ state
def load_seen():
    try:
        with open(SEEN, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_seen(keys):
    os.makedirs(os.path.dirname(SEEN) or ".", exist_ok=True)
    with open(SEEN, "w", encoding="utf-8") as f:
        json.dump(keys[:KEEP], f, indent=0)


# ------------------------------------------------------------------ main
def main():
    if not QUERY or not WEBHOOK:
        print("HATCH_QUERY and DISCORD_WEBHOOK_URL secrets are required", file=sys.stderr)
        return 2
    seen_list = load_seen()
    seen = set(seen_list) if seen_list is not None else None
    rows, details = scrape(seen)
    if not rows:
        print("no hatch rows found - nothing changed", file=sys.stderr)
        return 1
    if PREVIEW:  # show how an alert looks using the newest hatch; changes no state
        picks = preview_rows(rows)
        print("preview pets:", [r["key"] for r in picks])
        failed = 0
        cards = make_cards(picks, details)
        for i, (r, cpng) in enumerate(zip(picks, cards), 1):
            m = details.get(r["key"]) or {}
            print(f"preview {r['key']}: pet_url={m.get('pet_url')} png={'yes' if m.get('pet_png') else 'no'}"
                  f" card={'yes' if cpng else 'no'}")
            try:
                print("discord status", send_alert(
                    r, details.get(r["key"]), cpng,
                    f"{ping()}\U0001F9EA Test alert {i}/{len(picks)}: **{r['name']}**"))
            except Exception as e:
                failed += 1
                print("discord failed:", e, file=sys.stderr)
            time.sleep(1.5)  # stay under Discord's webhook rate limit
        return 1 if failed == len(picks) else 0
    current = [r["key"] for r in rows]

    if seen is None:  # first run: remember everything, no flood of pings
        save_seen(current)
        print("first run: stored", len(current), "existing hatches")
        try:
            print("discord status", post(
                f"{ping()}✅ Hatch tracker is running. Watching {len(current)} recent hatches; "
                "new ones will ping you here."))
        except Exception as e:
            print("discord failed:", e, file=sys.stderr)
        return 0

    fresh = [r for r in rows if r["key"] not in seen]
    print("new hatches:", len(fresh))
    sent = set()
    batch = list(reversed(fresh[:MAX_SEND]))  # oldest first
    cards = make_cards(batch, details) if batch else []
    for r, cpng in zip(batch, cards):
        m = details.get(r["key"])
        try:
            st = send_alert(r, m, cpng, f"{ping()}\U0001F389 New hatch: **{r['name']}**")
            print("sent", r["key"], st)
            sent.add(r["key"])
            time.sleep(1.2)  # spacing between alerts in a burst
        except Exception as e:
            print("discord failed for", r["key"], e, file=sys.stderr)
    skipped = {r["key"] for r in fresh[MAX_SEND:]}  # beyond the cap: mark seen, no flood
    keep = [k for k in current if k in sent or k in skipped or k in seen]
    save_seen(keep + [k for k in seen_list if k not in keep])
    return 0


if __name__ == "__main__":
    sys.exit(main())
