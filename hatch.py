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
import json
import os
import re
import sys
import time
import urllib.request
from urllib.parse import quote, urljoin

BASE = "https://petsuniverse.org"
QUERY = os.environ.get("HATCH_QUERY", "").strip()
WEBHOOK = os.environ.get("HATCH_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL", "")
USER_ID = os.environ.get("DISCORD_USER_ID", "").strip()
SEEN = os.environ.get("HATCH_SEEN", "data/seen_hatches.json")
MAX_SEND = 10
PREVIEW = os.environ.get("PREVIEW", "").strip().lower() == "true"
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
            fresh = rows[:1] if PREVIEW else [r for r in rows if r["key"] not in seen][:MAX_SEND]
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
                    if len(details) == 1:
                        print("--- first popup text ---")
                        print(got["text"][:600])
                        print("popup images:", json.dumps(got["imgs"])[:300], "avatar:", got.get("avatar"))
                else:
                    print("no popup details for", r["key"])
                try:
                    pg.keyboard.press("Escape")
                    pg.wait_for_timeout(500)
                except Exception:
                    pass
        b.close()
    return rows, details


# ------------------------------------------------------------------ discord
def build_embed(r, m):
    m = m or {}
    who = m.get("display") or r["acct"] or "?"
    user = f" (@{m['username']})" if m.get("username") else ""
    blob = " ".join(r["extras"])
    rarity = next((e for e in r["extras"] if e.lower() in COLORS), "")
    mo = re.search(r"1/[\d,]+", blob)
    odds = mo.group(0) if mo else ""
    ml = re.search(r"\d+(?:\.\d+)?x luck", blob, re.I)
    luck = ml.group(0) if ml else ""
    fields = []

    def add(name, value, inline=True):
        if value:
            fields.append({"name": name, "value": str(value), "inline": inline})

    add("Rarity", rarity or ("Rare" if not odds else ""))
    add("Odds", odds)
    add("Luck", luck)
    add("Egg", m.get("hatched_from") or r["egg"])
    add("Serial", f"#{r['serial']}")
    add("Value", m.get("value"))
    add("Total eggs", m.get("eggs"))
    add("When", (m.get("when") + " (Stockholm)") if m.get("when") else r["ago"])
    emb = {"title": f"\U0001F389 {r['name']} #{r['serial']}",
           "description": f"**{who}** hatched a **{r['name']}**!",
           "color": COLORS.get(rarity.lower(), 0x5865F2), "fields": fields,
           "footer": {"text": "Pets Universe • Hatches"}}
    author = {"name": f"{who}{user}"}
    if m.get("avatar"):
        author["icon_url"] = m["avatar"]
    emb["author"] = author
    icon = m.get("pet_icon") or r["icon"]
    if icon:
        emb["thumbnail"] = {"url": icon}
    return emb


def post(content, embed=None):
    payload = {"content": content}
    if embed:
        payload["embeds"] = [embed]
    if USER_ID:
        payload["allowed_mentions"] = {"users": [USER_ID]}
    req = urllib.request.Request(WEBHOOK, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "User-Agent": "pu-hatches/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


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
        r = rows[0]
        try:
            print("discord status", post(f"{ping()}\U0001F9EA Preview of a hatch alert (your latest hatch):",
                                         build_embed(r, details.get(r["key"]))))
        except Exception as e:
            print("discord failed:", e, file=sys.stderr)
            return 1
        return 0
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
    for r in reversed(fresh[:MAX_SEND]):  # oldest first
        m = details.get(r["key"])
        try:
            st = post(f"{ping()}\U0001F389 New hatch: **{r['name']}**", build_embed(r, m))
            print("sent", r["key"], st)
            sent.add(r["key"])
            time.sleep(1)
        except Exception as e:
            print("discord failed for", r["key"], e, file=sys.stderr)
    skipped = {r["key"] for r in fresh[MAX_SEND:]}  # beyond the cap: mark seen, no flood
    keep = [k for k in current if k in sent or k in skipped or k in seen]
    save_seen(keep + [k for k in seen_list if k not in keep])
    return 0


if __name__ == "__main__":
    sys.exit(main())
