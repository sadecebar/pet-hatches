#!/usr/bin/env python3
"""Long-running loop (one GitHub Actions job lasts ~5h45m, then restarts itself).

Uses whichever tracker files are present in the repo:
- hatch.py  -> hatch check every POLL_SECONDS (default 60)
- track.py  -> Flower leaderboard once at the start of each Stockholm hour
STOP_AT (ISO time, optional): after that moment, do a final check and stop for good.
"""
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    import hatch
except ImportError:
    hatch = None
try:
    import track
except ImportError:
    track = None

TZ = ZoneInfo("Europe/Stockholm")
POLL = int(os.environ.get("POLL_SECONDS", "60"))
RUN_FOR = int(float(os.environ.get("RUN_FOR_SECONDS", str(345 * 60))))
STOP_AT = datetime.fromisoformat(os.environ["STOP_AT"]) if os.environ.get("STOP_AT") else None
FILES = ["data/seen_hatches.json", "data/tracker.db"]

if track and os.environ.get("FLOWER_WEBHOOK_URL"):  # optional: flower reports to another channel
    track.WEBHOOK = os.environ["FLOWER_WEBHOOK_URL"]


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120).returncode
    except Exception as e:
        print("cmd failed:", cmd[:2], e)
        return 1


def save_state(msg):
    """Commit changed state files so nothing is lost if the job ends."""
    sh("git", "config", "user.name", "tracker")
    sh("git", "config", "user.email", "tracker@users.noreply.github.com")
    present = [f for f in FILES if os.path.exists(f)]
    if not present:
        return
    sh("git", "add", *present)
    if sh("git", "diff", "--cached", "--quiet") != 0:
        sh("git", "commit", "-m", msg)
        if sh("git", "pull", "--rebase", "-X", "theirs") != 0:
            sh("git", "rebase", "--abort")
        sh("git", "push")


def set_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def safe(label, fn):
    try:
        print(f"[{datetime.now(TZ):%H:%M:%S}] {label}")
        return fn()
    except Exception:
        print(f"{label} crashed:")
        traceback.print_exc()
        return 1


def main():
    if hatch and hatch.PREVIEW:  # manual "preview" run: one sample alert, then stop
        return hatch.main()
    if STOP_AT and datetime.now(TZ) >= STOP_AT:
        print("stop time already passed - nothing to do")
        set_output("done", "true")
        return 0
    start = time.time()
    last_hour = None
    while time.time() - start < RUN_FOR:
        t0 = time.time()
        now = datetime.now(TZ)
        hour_key = now.strftime("%Y-%m-%d %H")
        if track and now.minute < 5 and hour_key != last_hour:
            last_hour = hour_key
            safe("flower check", track.main)
            save_state("flower snapshot")
        if hatch:
            safe("hatch check", hatch.main)
            save_state("seen hatches")
        if STOP_AT and now >= STOP_AT:
            print("stop time reached - finished")
            if track and track.WEBHOOK:
                safe("final notice", lambda: track.notify(
                    "\U0001F3C1 **Flower tracker finished** - the leaderboard has ended. "
                    "All saved history stays in the repo."))
            set_output("done", "true")
            save_state("final state")
            return 0
        time.sleep(max(5, POLL - (time.time() - t0)))
    save_state("final state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
