#!/usr/bin/env python3
"""Daily hatch recap: what every account hatched in a day, and what it is worth.

Data comes from the site's own public API (the same calls the website makes):
- r=hatches&q=<HATCH_QUERY>  -> every rare hatch with exact time, account and pet variant
- r=values                   -> the price list. "Value" is looked up exactly like the
                                hatch popup does: by variant key (P:<pet>), then by name.

Posted automatically shortly after 00:00 Stockholm for the day that just ended
(runner.py calls maybe_daily). RECAP=true in the workflow posts the last 24 h right away.
"""
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import hatch

TZ = ZoneInfo("Europe/Stockholm")
API = "https://petsuniverse.org/api/public.php"
STATE = os.environ.get("RECAP_STATE", "data/recap_state.json")
LIMIT = 60          # what the website itself asks for
SEND_AFTER_MIN = 2  # wait a couple of minutes after midnight so late hatches are in the feed
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
RARITY_DOT = {"secret": "\U0001F7E3", "exclusive": "\U0001F7E1", "mythical": "\U0001F534",
              "legendary": "\U0001F7E0"}
WHITE = "⚪"
PING = os.environ.get("RECAP_PING", "").strip().lower() == "true"  # @ping on the daily recap
_next_try = 0.0  # throttles retries after a failed recap


# ------------------------------------------------------------------ data
def _api_direct(query):
    req = urllib.request.Request(f"{API}?{query}", headers={
        "User-Agent": BROWSER_UA, "Referer": "https://petsuniverse.org/hatches",
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _api_browser(queries):
    """Fallback: ask from inside a real page (same origin), in case plain requests are blocked."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto("https://petsuniverse.org/hatches", wait_until="domcontentloaded", timeout=60000)
        out = [pg.evaluate("q => fetch('/api/public.php?' + q).then(r => r.json())", q) for q in queries]
        b.close()
    return out


def fetch():
    """Returns (hatches, value_items)."""
    qs = [f"r=hatches&limit={LIMIT}&kind=pets&q={quote(q)}" for q in hatch.QUERIES] + ["r=values"]
    try:
        res = [_api_direct(q) for q in qs]
    except Exception as e:
        print("direct API failed, using browser:", str(e)[:100])
        res = _api_browser(qs)
    *hs, v = res
    if not all(h.get("ok") for h in hs) or not v.get("ok"):
        raise RuntimeError(f"API not ok: hatches={[h.get('ok') for h in hs]} values={v.get('ok')}")
    got = {}
    for h in hs:
        for x in h.get("list") or []:
            got.setdefault(log_key(x), x)
    return sorted(got.values(), key=lambda x: -(x.get("at") or 0)), v.get("items") or []


# ------------------------------------------------------------------ hatch log
# The site only ever returns the newest 60 hatches per search, so a busy day can't be read back
# at midnight. Instead the tracker writes every hatch down as it sees it (every minute), with the
# site's own hatch time, and the recap adds up that log.
LOG = os.environ.get("HATCH_LOG", "data/hatch_log.json")
LOG_KEEP_DAYS = 10
LOG_FIELDS = ("at", "uid", "name", "display", "pet", "pretty", "rarity", "serial", "icon")
PETS_SPLIT = ("Flower Swarm", "Road Pirates Bees", "Golden Flower Swarm", "Golden Road Pirates Bees",
              "Wet Flower Swarm", "Wet Road Pirates Bees")


def log_key(h):
    return f"{h.get('uid')}|{h.get('pretty')}|{h.get('serial')}"


def load_log():
    try:
        with open(LOG, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def save_log(lg):
    cut = datetime.now(timezone.utc).timestamp() - LOG_KEEP_DAYS * 86400
    lg["hatches"] = {k: h for k, h in lg["hatches"].items() if h.get("at", 0) >= cut}
    lg["gaps"] = [g for g in lg.get("gaps", []) if g[1] >= cut]
    os.makedirs(os.path.dirname(LOG) or ".", exist_ok=True)
    with open(LOG, "w", encoding="utf-8") as f:
        json.dump(lg, f, separators=(",", ":"))


def _merge(lg, hatches):
    new = 0
    for h in hatches:
        if h.get("at") is None:
            continue
        k = log_key(h)
        if k not in lg["hatches"]:
            new += 1
        lg["hatches"][k] = {f: h.get(f) for f in LOG_FIELDS}
        if h.get("name") and h["name"] not in lg["accounts"]:
            lg["accounts"].append(h["name"])
    return new


def deep_fetch(since):
    """Everything since `since` (unix s), working around the 60 limit: one search per account,
    split by pet when an account alone has more than 60. Returns (hatches, complete)."""
    got, complete = [], True

    def q(extra):
        return _api_direct(f"r=hatches&limit={LIMIT}&kind=pets&{extra}").get("list") or []

    def full(lst):
        return len(lst) >= LIMIT and min(h.get("at", 0) for h in lst) > since

    lg = load_log() or {"accounts": []}
    accts = set(lg.get("accounts", []))
    for extra in [f"q={quote(w)}" + (f"&pet={quote(p)}" if p else "") for w in hatch.QUERIES for p in ("",) + PETS_SPLIT]:
        lst = q(extra)
        got += lst
        accts |= {h["name"] for h in lst if h.get("name")}
    for a in sorted(accts):
        lst = q(f"q={quote(a)}")
        got += lst
        if full(lst):
            for p in PETS_SPLIT:
                l2 = q(f"q={quote(a)}&pet={quote(p)}")
                got += l2
                complete = complete and not full(l2)
    return got, complete


def log_feed():
    """Called every loop: write the newest hatches into the log. If the tracker was away so long
    that the newest 60 don't reach back to the last check, fill the gap with a deep search."""
    now = datetime.now(timezone.utc).timestamp()
    lg = load_log() or {"hatches": {}, "accounts": [], "since": None, "last_ok": None, "gaps": []}
    lst, oldest, full = [], 0, False
    for w in hatch.QUERIES:  # each search on its own: the 60 limit applies per search
        one = _api_direct(f"r=hatches&limit={LIMIT}&kind=pets&q={quote(w)}").get("list") or []
        lst += one
        if len(one) >= LIMIT:  # this search is full: how far back it reaches decides the gap check
            full = True
            oldest = max(oldest, min(h.get("at", now) for h in one))
    if not full:
        oldest = min((h.get("at", now) for h in lst), default=now)
    if lg.get("since") is None:  # first start (no saved log): rebuild from yesterday 00:00 with a deep search
        y0 = datetime.combine((datetime.now(TZ) - timedelta(days=1)).date(), datetime.min.time(), TZ).timestamp()
        try:
            more, _ = deep_fetch(y0)
            lst += more
            lg["since"] = y0 if not full or oldest <= y0 or more else oldest
            print(f"hatch log: new log, rebuilt from {datetime.fromtimestamp(lg['since'], TZ):%d %b %H:%M}")
        except Exception as e:
            print("hatch log: deep search failed:", str(e)[:120])
            lg["since"] = oldest if full else now - 45 * 86400
    elif full and lg.get("last_ok") and oldest > lg["last_ok"]:  # away too long: newest 60 don't reach back
        print(f"hatch log: gap since {datetime.fromtimestamp(lg['last_ok'], TZ):%d %b %H:%M}, deep search")
        try:
            more, complete = deep_fetch(lg["last_ok"] - 300)
            lst += more
            if not complete:
                lg.setdefault("gaps", []).append([lg["last_ok"], oldest])
        except Exception as e:
            print("hatch log: deep search failed:", str(e)[:120])
            lg.setdefault("gaps", []).append([lg["last_ok"], oldest])
    new = _merge(lg, lst)
    lg["last_ok"] = now
    save_log(lg)
    if new:
        print(f"hatch log: +{new} (total {len(lg['hatches'])})")
    return 0


def logged(start, end):
    """(hatches in [start, end) from the log, note or None). None when there is no usable log."""
    lg = load_log()
    if not lg or lg.get("since") is None:
        return None
    s, e = start.timestamp(), end.timestamp()
    if lg["since"] >= e:
        return None
    day = [h for h in lg["hatches"].values() if s <= h.get("at", 0) < e]
    note = None
    if lg["since"] > s:
        note = f"counted since {datetime.fromtimestamp(lg['since'], TZ):%H:%M} (hatch log started)"
    gaps = [g for g in lg.get("gaps", []) if g[0] < e and g[1] > s]
    if gaps:
        note = ((note + " · ") if note else "") + "tracker was offline " + ", ".join(
            f"{datetime.fromtimestamp(a, TZ):%H:%M}-{datetime.fromtimestamp(b, TZ):%H:%M}" for a, b in gaps)
    return day, note


def value_of(h, by_key, by_name):
    """Same lookup as the site's hatch popup: exact variant first, then the pretty name."""
    it = by_key.get(f"P:{h.get('pet')}") or by_name.get(h.get("pretty"))
    v = it.get("rap") if it else None
    return float(v) if v is not None else None


# ------------------------------------------------------------------ formatting
def fmt(v):
    if v is None:
        return "?"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            x = v / div
            t = f"{x:.2f}" if x < 10 else f"{x:.1f}" if x < 100 else f"{x:.0f}"
            return (t.rstrip("0").rstrip(".") if "." in t else t) + suf  # never turn 400 into 4
    return f"{v:,.0f}"


def build(hatches, items, start, end, label, partial=False):
    by_key = {i["k"]: i for i in items if i.get("k")}
    by_name = {}
    for i in items:
        by_name.setdefault(i.get("name"), i)
    day = [h for h in hatches if start.timestamp() <= h.get("at", 0) < end.timestamp()]
    day.sort(key=lambda h: h["at"])

    accts = defaultdict(lambda: {"display": "", "pets": defaultdict(lambda: [0, 0.0, False]), "n": 0, "v": 0.0})
    total, unpriced, best, rar = 0.0, 0, None, defaultdict(int)
    for h in day:
        v = value_of(h, by_key, by_name)
        a = accts[h.get("name") or h.get("display") or "?"]
        a["display"] = h.get("display") or a["display"]
        a["n"] += 1
        p = a["pets"][h.get("pretty") or h.get("pet") or "?"]
        p[0] += 1
        if v is None:
            unpriced += 1
            p[2] = True
        else:
            p[1] += v
            a["v"] += v
            total += v
            if best is None or v > best[0]:
                best = (v, h)
        rar[(h.get("rarity") or "").lower()] += 1

    lines = []
    if not day:
        lines.append("No rare hatches on any account. \U0001F634")
    else:
        rar_txt = "  ·  ".join(f"{RARITY_DOT.get(r, WHITE)} {r.title() or 'Other'} {n}"
                                   for r, n in sorted(rar.items(), key=lambda x: -x[1]))
        na = len([u for u in accts if u != "unknown"])  # "unknown" = alerted, but the site no longer lists its account
        lines += [f"\U0001F95A **{len(day)}** hatch{'es' * (len(day) != 1)} on **{na}** "
                  f"account{'s' * (na != 1)}",
                  f"\U0001F3AB **{fmt(total)}** total value" + (f"  *(+{unpriced} without a price)*" if unpriced else ""),
                  rar_txt]
        if best:
            bv, bh = best
            lines += ["", f"\U0001F3C6 **Best hatch:** {bh.get('pretty')} · \U0001F3AB **{fmt(bv)}**",
                      f" by {bh.get('display')} (@{bh.get('name')})"]
        for user, a in sorted(accts.items(), key=lambda kv: (-kv[1]["v"], -kv[1]["n"])):
            lines += ["", f"\U0001F464 **{a['display']}** · @{user}",
                      f" {a['n']} hatch{'es' * (a['n'] != 1)} · \U0001F3AB **{fmt(a['v'])}**"]
            for pet, (n, v, missing) in sorted(a["pets"].items(), key=lambda kv: (-kv[1][1], -kv[1][0])):
                price = "no price yet" if missing and not v else f"\U0001F3AB {fmt(v)}"
                lines.append(f" ↳ {n}× {pet} · {price}")
    if isinstance(partial, str):
        lines += ["", f"ℹ️ {partial}"]
    elif partial:
        lines += ["", f"⚠️ The feed only returns the latest {LIMIT} hatches, so the oldest part of this "
                      "period may be missing."]

    desc = "\n".join(lines)
    if len(desc) > 4000:
        desc = desc[:3990].rsplit("\n", 1)[0] + "\n…"
    return {"title": f"\U0001F4C5 Hatch recap · {label}",
            "description": desc,
            "color": 0xF1C40F,
            "footer": {"text": "Pets Universe • Daily recap • Value = site value list"},
            "timestamp": end.astimezone(timezone.utc).isoformat()}


def card_data(hatches, items, start, end, label, partial=False):
    """Same numbers as build(), shaped for the picture card."""
    by_key = {i["k"]: i for i in items if i.get("k")}
    by_name = {}
    for i in items:
        by_name.setdefault(i.get("name"), i)
    day = [h for h in hatches if start.timestamp() <= h.get("at", 0) < end.timestamp()]
    accts, rar = {}, defaultdict(int)
    total, unpriced, best = 0.0, 0, None
    for h in sorted(day, key=lambda h: h["at"]):
        v = value_of(h, by_key, by_name)
        user = h.get("name") or h.get("display") or "?"
        a = accts.setdefault(user, {"display": h.get("display") or user, "name": user, "uid": h.get("uid"),
                                    "n": 0, "v": 0.0, "pets": {}})
        a["n"] += 1
        pet = h.get("pretty") or h.get("pet") or "?"
        p = a["pets"].setdefault(pet, [0, 0.0, h.get("icon")])
        p[0] += 1
        if v is None:
            unpriced += 1
        else:
            p[1] += v
            a["v"] += v
            total += v
            if best is None or v > best[0]:
                best = (v, h)
        rar[(h.get("rarity") or "").lower()] += 1
    accounts = []
    for a in sorted(accts.values(), key=lambda a: (-a["v"], -a["n"])):
        pets = sorted(((pn, c, v, icon) for pn, (c, v, icon) in a["pets"].items()), key=lambda x: (-x[2], -x[1]))
        accounts.append({"display": a["display"], "name": a["name"], "uid": a["uid"], "n": a["n"],
                         "value": fmt(a["v"]), "pets": pets})
    return {"label": label, "total": fmt(total), "n": len(day), "accounts_n": len([u for u in accts if u != "unknown"]),
            "rarity": sorted(rar.items(), key=lambda x: -x[1]), "unpriced": unpriced, "partial": partial,
            "best": ({"pretty": best[1].get("pretty"), "icon": best[1].get("icon"), "value": fmt(best[0]),
                      "display": best[1].get("display"), "name": best[1].get("name")} if best else None),
            "accounts": accounts}


def send(start, end, label):
    from_log = None
    try:
        from_log = logged(start, end)
    except Exception as e:
        print("hatch log unreadable, using the feed:", str(e)[:120])
    if from_log is not None:
        hatches, partial = from_log
        try:
            items = _api_direct("r=values").get("items") or []
        except Exception:
            items = _api_browser(["r=values"])[0].get("items") or []
        print(f"recap from hatch log: {len(hatches)} hatches", f"({partial})" if partial else "")
    else:  # no log yet: the newest 60 from the feed
        hatches, items = fetch()
        oldest = min((h.get("at", 0) for h in hatches), default=0)
        partial = len(hatches) >= LIMIT and oldest > start.timestamp()
    emb = build(hatches, items, start, end, label, partial)
    print(emb["description"])
    png = None
    try:
        import card
        png = card.render_html([card.build_recap_html(card_data(hatches, items, start, end, label, partial))])[0]
    except Exception as e:
        print("recap card failed, sending text:", str(e)[:150])
    head = hatch.ping().strip() if PING else ""
    if png:
        pic = {"title": emb["title"], "color": emb["color"], "image": {"url": "attachment://recap.png"},
               "timestamp": emb["timestamp"]}
        st = hatch.post(head, pic, png, "recap.png")
    else:
        st = hatch.post(head, emb)
    print("recap discord status", st, "(card)" if png else "(text)")
    return 0


# ------------------------------------------------------------------ entry points
def day_recap(day):
    """Repost the recap of one finished day (YYYY-MM-DD): only hatches from 00:00 to 24:00 that day."""
    start = datetime.combine(datetime.fromisoformat(day).date(), datetime.min.time(), TZ)
    return send(start, start + timedelta(days=1), f"{start:%a %d %b}")


def last24():
    """Manual test: the last 24 hours up to now."""
    end = datetime.now(TZ)
    return send(end - timedelta(hours=24), end, f"last 24 h (until {end:%H:%M})")


def _load():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def _save(st):
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def maybe_daily(now=None):
    """Called every loop: posts yesterday's recap once, shortly after midnight Stockholm.
    Catches up later in the morning if the tracker was down at midnight."""
    now = now or datetime.now(TZ)
    yesterday = (now - timedelta(days=1)).date()
    st = _load()
    if st is None:  # first start with this feature: begin with tonight's recap, don't post an old one
        _save({"last_day": yesterday.isoformat()})
        print("recap: state created, first recap after next midnight")
        return 0
    if st.get("last_day") >= yesterday.isoformat():
        return 0
    if now.hour == 0 and now.minute < SEND_AFTER_MIN:
        return 0
    if now.hour >= 12:  # too late to call it "last night's" recap - skip quietly
        print("recap: missed the morning window for", yesterday)
        _save({"last_day": yesterday.isoformat()})
        return 0
    global _next_try
    if now.timestamp() < _next_try:
        return 0
    _next_try = now.timestamp() + 300  # if this attempt fails, try again in 5 min
    start = datetime.combine(yesterday, datetime.min.time(), TZ)
    rc = send(start, start + timedelta(days=1), f"{start:%a %d %b}")
    if rc == 0:
        _save({"last_day": yesterday.isoformat()})
    return rc


if __name__ == "__main__":
    sys.exit(last24())
