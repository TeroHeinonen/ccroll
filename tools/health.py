#!/usr/bin/env python3
"""ccroll health check: is the master running, is it keeping its state and
readings fresh, are its clients linked.  Prints a short report and exits 1
when something needs a look, so it fits a cron job or a loop.

Read-only.  By default it reads only local files (state.json, the store);
--probe N also reads N accounts' usage endpoint per run, in turn, to log the
limit windows each one reports.  A probe never touches an account some
machine is live on: its tokens belong to that machine's Claude Code, and an
extra read there only adds to the rate limit its rotation depends on.

    python3 tools/health.py                 # local checks only
    python3 tools/health.py --probe 3       # plus 3 accounts' usage, in turn
"""
import argparse
import glob
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def load_ccroll():
    spec = importlib.util.spec_from_file_location("ccroll", os.path.join(HERE, "..", "ccroll.py"))
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    return cc


def ccroll_masters() -> list[int]:
    """PIDs of running ccroll masters: a process whose argv runs ccroll with
    no subcommand or with `watch` (clients, relays and one-shot commands are
    not the master)."""
    out = []
    for d in glob.glob("/proc/[0-9]*"):
        try:
            argv = open(os.path.join(d, "cmdline"), "rb").read().split(b"\0")
        except OSError:
            continue
        argv = [a.decode(errors="replace") for a in argv if a]
        for i, a in enumerate(argv):
            if os.path.basename(a) in ("ccroll", "ccroll.py"):
                rest = [x for x in argv[i + 1:] if not x.startswith("-")]
                if not rest or rest[0] == "watch":
                    out.append(int(os.path.basename(d)))
                break
    return sorted(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--root", default="~/.claude-accounts", help="account store dir")
    p.add_argument("--state-age", type=float, default=300,
                   help="flag state.json older than this many seconds (default 300)")
    p.add_argument("--reading-age", type=float, default=1800,
                   help="flag an account reading older than this many seconds (default 1800)")
    p.add_argument("--probe", type=int, default=0, metavar="N",
                   help="also read N accounts' usage endpoint, in turn across runs")
    a = p.parse_args()

    root = os.path.expanduser(a.root)
    here = os.path.join(root, ".ccroll")
    t = time.time()
    out, bad = [], []

    pids = ccroll_masters()
    out.append(f"ccroll master: {', '.join(map(str, pids)) if pids else 'NOT RUNNING'}")
    if not pids:
        bad.append("no ccroll master running")

    sp = os.path.join(here, "state.json")
    try:
        s = json.load(open(sp))
    except (OSError, ValueError) as e:
        print("\n".join(out + [f"state.json unreadable: {e}"]))
        return 1
    age = t - os.path.getmtime(sp)
    out.append(f"state.json written {age:.0f}s ago · live {s.get('active')}")
    if age > a.state_age:
        bad.append(f"state.json not written for {age:.0f}s")

    # only accounts still in the store: a removed one keeps its last reading
    stored = sorted(os.path.basename(os.path.dirname(c)) for c in glob.glob(root + "/*/.credentials.json"))
    readings = s.get("readings") or {}
    stale = [n for n in stored if n in readings and t - readings[n]["fetched_at"] > a.reading_age]
    unread = [n for n in stored if n not in readings]
    no5h = [n for n in stored if n in readings and readings[n].get("session_pct") is None]
    out.append(f"accounts: {len(stored)} · without a 5-hour window: {len(no5h)}"
               + (f" · stale: {', '.join(stale)}" if stale else "")
               + (f" · never read: {', '.join(unread)}" if unread else ""))
    if stale:
        bad.append(f"stale readings: {', '.join(stale)}")

    lanes = s.get("lanes") or {}
    for host, ls in sorted(lanes.items()):
        off = ls.get("offline_since")
        state = (f"offline since {time.strftime('%H:%M', time.localtime(off))}" if off else "linked")
        out.append(f"client {host}: {state} · on {ls.get('active') or '—'}")
        if off and ls.get("active"):
            bad.append(f"client {host} offline, holding {ls['active']}")

    for e in (s.get("events") or [])[-3:]:
        out.append(f"  event {time.strftime('%H:%M', time.localtime(e[0]))} {e[1]}")

    if a.probe > 0 and stored:
        cc = load_ccroll()
        live = {s.get("active")} | {ls.get("active") for ls in lanes.values()}
        os.makedirs(os.path.join(here, "health"), exist_ok=True)
        cur_f = os.path.join(here, "health", "cursor")
        try:
            cur = int(open(cur_f).read())
        except (OSError, ValueError):
            cur = 0
        pick = [stored[(cur + i) % len(stored)] for i in range(min(a.probe, len(stored)))]
        open(cur_f, "w").write(str((cur + len(pick)) % len(stored)))
        with open(os.path.join(here, "health", "windows.log"), "a") as log:
            for n in pick:
                if n in live:
                    out.append(f"  probe {n}: live on a machine, skipped")
                    continue
                o = (cc.oauth_of(cc.read_json(os.path.join(root, n, ".credentials.json"))) or {})
                if not o.get("accessToken") or o.get("expiresAt", 0) / 1000 < t + 30:
                    out.append(f"  probe {n}: token expired, skipped (ccroll refreshes it when needed)")
                    continue
                status, body, err, _ = cc._send("GET", cc.API_BASE + cc.USAGE_PATH, o["accessToken"], None)
                try:
                    j = json.loads(body)
                except ValueError:
                    j = {}
                kinds = [l.get("kind") for l in (j.get("limits") or [])] if isinstance(j, dict) else []
                log.write(json.dumps({"t": t, "account": n, "status": status, "kinds": kinds}) + "\n")
                out.append(f"  probe {n}: http {status or err} · windows {', '.join(kinds) or '—'}")
                time.sleep(1)

    if bad:
        out.append("NEEDS A LOOK: " + "; ".join(bad))
    print("\n".join(out))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
