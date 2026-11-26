#!/usr/bin/env python3
"""Hatch card: a picture version of the hatch alert.

The card is a small HTML page rendered by headless Chromium (already installed for the
scraper) and screenshotted to PNG. Value / Exists / catalogue odds come from the site's
public value list (same data as the hatch popup).
"""
import base64
import html
import json
import re
import time
import urllib.request

API = "https://petsuniverse.org/api/public.php"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
THEMES = {  # rarity -> (accent, glow)
    "secret": ("#c084fc", "rgba(168,85,247,.55)"),
    "exclusive": ("#facc15", "rgba(250,204,21,.45)"),
    "mythical": ("#f87171", "rgba(239,68,68,.5)"),
    "legendary": ("#fb923c", "rgba(249,115,22,.5)"),
}
DEFAULT_THEME = ("#60a5fa", "rgba(59,130,246,.45)")
_VALUES = {"at": 0.0, "items": []}


# ------------------------------------------------------------------ data
def values(max_age=600):
    """The site's value list, cached for 10 minutes."""
    if time.time() - _VALUES["at"] > max_age or not _VALUES["items"]:
        req = urllib.request.Request(f"{API}?r=values", headers={
            "User-Agent": BROWSER_UA, "Referer": "https://petsuniverse.org/values", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        if d.get("ok"):
            _VALUES.update(at=time.time(), items=d.get("items") or [])
    return _VALUES["items"]


def lookup(name):
    """Value-list entry for a hatched pet name (e.g. 'Rainbow Bee Emperor'), or {}."""
    try:
        items = values()
    except Exception as e:
        print("value list failed:", str(e)[:100])
        return {}
    return next((i for i in items if i.get("name") == name and i.get("k", "").startswith("P:")), None) \
        or next((i for i in items if i.get("name") == name), {}) or {}


def short(v):
    """2181 -> '2,181'; 163.3 -> '163'; 2334.6 -> '2.33K'; 133386693 -> '133.4M'."""
    if v is None:
        return None
    v = float(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div * (10 if suf == "K" else 1):
            x = v / div
            return (f"{x:.2f}" if x < 10 else f"{x:.1f}" if x < 100 else f"{x:.0f}").rstrip("0").rstrip(".") + suf
    return f"{v:,.0f}"


def odds_text(row_odds, catalog):
    """'1/133,386,693' -> '1 in 133.4M'; falls back to the catalogue odds ('1 in 333')."""
    m = re.search(r"1/([\d,]+)", row_odds or "")
    if m:
        return f"1 in {short(m.group(1).replace(',', ''))}"
    c = (catalog or "").strip()
    m = re.fullmatch(r"1 in ([\d,]+)", c)
    if m:
        return f"1 in {short(m.group(1).replace(',', ''))}"
    return c or None


def to_data_uri(png=None, url=None):
    if png:
        return "data:image/png;base64," + base64.b64encode(png).decode()
    if url:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Referer": "https://petsuniverse.org/"})
            with urllib.request.urlopen(req, timeout=15) as r:
                ctype = (r.headers.get("Content-Type") or "image/png").split(";")[0]
                return f"data:{ctype};base64," + base64.b64encode(r.read()).decode()
        except Exception as e:
            print("card: pet image download failed:", str(e)[:100])
    return None


# ------------------------------------------------------------------ html
CSS = """
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;800&display=block');
*{box-sizing:border-box;margin:0;padding:0}
body{background:transparent;font-family:'Outfit','Noto Sans','DejaVu Sans',sans-serif;color:#f4f4f7}
.card{width:600px;border-radius:30px;padding:26px 26px 22px;position:relative;overflow:hidden;
  background:radial-gradient(120% 70% at 50% 0%,var(--glow) 0%,rgba(0,0,0,0) 60%),
             linear-gradient(180deg,#17151f 0%,#0d0c12 100%);
  border:1.5px solid color-mix(in srgb,var(--accent) 45%,transparent)}
.card:before{content:"";position:absolute;inset:0;pointer-events:none;
  background-image:radial-gradient(rgba(255,255,255,.55) 1px,transparent 1.5px);
  background-size:46px 46px;opacity:.10}
.top{display:flex;justify-content:space-between;align-items:center;position:relative}
.tag{font-size:13px;font-weight:800;letter-spacing:.18em;color:var(--accent);
  padding:7px 14px;border-radius:99px;background:color-mix(in srgb,var(--accent) 14%,transparent);
  border:1px solid color-mix(in srgb,var(--accent) 40%,transparent)}
.when{font-size:14px;color:#a1a1b5;font-weight:600}
.hero{height:300px;display:flex;align-items:center;justify-content:center;position:relative;margin:6px 0 2px}
.halo{position:absolute;width:290px;height:290px;border-radius:50%;
  background:radial-gradient(circle,var(--glow) 0%,rgba(0,0,0,0) 68%)}
.ring{position:absolute;width:250px;height:250px;border-radius:50%;
  border:2px dashed color-mix(in srgb,var(--accent) 35%,transparent)}
.pet{position:relative;max-width:270px;max-height:270px;width:270px;object-fit:contain;
  filter:drop-shadow(0 18px 26px rgba(0,0,0,.6))}
.nopet{position:relative;font-size:150px}
.spark{position:absolute;color:var(--accent);opacity:.9}
h1{font-size:40px;font-weight:800;text-align:center;line-height:1.05;letter-spacing:-.01em;position:relative}
.sub{display:flex;justify-content:center;gap:10px;margin:12px 0 20px;position:relative}
.chip{font-size:16px;font-weight:600;padding:6px 14px;border-radius:99px;background:#ffffff10;color:#d7d7e3}
.chip.r{color:#111;background:var(--accent);font-weight:800}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;position:relative}
.tile{background:#ffffff0b;border:1px solid #ffffff12;border-radius:18px;padding:13px 16px}
.tile .k{font-size:13px;color:#9a9ab0;font-weight:600;letter-spacing:.06em;text-transform:uppercase}
.tile .v{font-size:25px;font-weight:800;margin-top:3px;white-space:nowrap}
.tile.main{background:color-mix(in srgb,var(--accent) 13%,transparent);
  border-color:color-mix(in srgb,var(--accent) 38%,transparent)}
.tile.main .v{color:var(--accent)}
.who{display:flex;align-items:center;gap:14px;margin-top:14px;padding:12px 16px;border-radius:18px;
  background:#ffffff0b;border:1px solid #ffffff12;position:relative}
.av{width:52px;height:52px;border-radius:50%;background:#2a2838;object-fit:cover;flex:none}
.who .n{font-size:20px;font-weight:800}
.who .u{font-size:14px;color:#9a9ab0}
.who .egg{margin-left:auto;text-align:right}
.who .egg .k{font-size:12px;color:#9a9ab0;letter-spacing:.06em;text-transform:uppercase;font-weight:600}
.who .egg .v{font-size:18px;font-weight:800}
.foot{display:flex;justify-content:space-between;margin-top:14px;font-size:13px;color:#7c7c92;position:relative}
"""


def build_html(c):
    """c: dict with name, serial, rarity, when, value, exists, odds, luck, display, username,
    avatar, egg, eggs_opened, pet_src."""
    accent, glow = THEMES.get((c.get("rarity") or "").lower(), DEFAULT_THEME)
    e = lambda x: html.escape(str(x)) if x not in (None, "") else ""
    tiles = [("\U0001F3AB Value", c.get("value"), True), ("\U0001F30D Exists", c.get("exists"), False),
             ("\U0001F3B2 Odds", c.get("odds"), False), ("\U0001F340 Luck", c.get("luck"), False)]
    tiles = [t for t in tiles if t[1]]
    eggs_tile = len(tiles) % 2 == 1  # keep the grid even
    if eggs_tile:
        tiles.append(("\U0001F95A Eggs opened", c.get("eggs_opened") or "-", False))
    grid = "".join(f'<div class="tile{" main" if m else ""}"><div class="k">{e(k)}</div><div class="v">{e(v)}</div></div>'
                   for k, v, m in tiles)
    pet = (f'<img class="pet" src="{c["pet_src"]}">' if c.get("pet_src") else '<div class="nopet">\U0001F95A</div>')
    av = f'<img class="av" src="{e(c["avatar"])}">' if c.get("avatar") else '<div class="av"></div>'
    sparks = "".join(f'<div class="spark" style="left:{x}px;top:{y}px;font-size:{s}px">✦</div>'
                     for x, y, s in ((110, 40, 30), (440, 70, 22), (470, 210, 16), (95, 220, 18)))
    user = f"@{e(c['username'])}" if c.get("username") else ""
    foot_l = f"\U0001F95A {e(c['eggs_opened'])} eggs opened" if c.get("eggs_opened") and not eggs_tile else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head>
<body><div class="card" style="--accent:{accent};--glow:{glow}">
  <div class="top"><div class="tag">NEW HATCH</div><div class="when">{e(c.get('when'))}</div></div>
  <div class="hero"><div class="halo"></div><div class="ring"></div>{sparks}{pet}</div>
  <h1>{e(c.get('name'))}</h1>
  <div class="sub">{f'<span class="chip r">{e(c["rarity"])}</span>' if c.get("rarity") else ""}
    <span class="chip">#{e(c.get('serial'))}</span></div>
  <div class="grid">{grid}</div>
  <div class="who">{av}<div><div class="n">{e(c.get('display') or c.get('username') or '?')}</div>
    <div class="u">{user}</div></div>
    <div class="egg"><div class="k">Hatched from</div><div class="v">{e(c.get('egg'))}</div></div></div>
  <div class="foot"><span>{foot_l}</span><span>Pets Universe · Hatch Tracker</span></div>
</div></body></html>"""


def render(cards):
    """List of card dicts -> list of PNG bytes (None where rendering failed)."""
    if not cards:
        return []
    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 640, "height": 1000}, device_scale_factor=2)
        pg = ctx.new_page()
        for c in cards:
            try:
                pg.set_content(build_html(c), wait_until="networkidle", timeout=20000)
                pg.evaluate("document.fonts.ready")
                out.append(pg.locator(".card").screenshot(omit_background=True, timeout=10000))
            except Exception as ex:
                print("card render failed:", str(ex)[:120])
                out.append(None)
        b.close()
    return out
