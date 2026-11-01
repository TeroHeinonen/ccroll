#!/usr/bin/env python3
"""Build tests/fixtures/replay_swaps.json from a live ccroll's state.json and
its account-switch feed.  The output is anonymised: accounts appear only as
their feed labels (`acct-07`), and nothing else from state.json (emails,
tokens, reasons naming accounts) is copied.

    python3 tests/fixtures/build_replay_fixture.py \\
        --state ~/.claude-accounts/.ccroll/state.json \\
        --feed ~/.claude/account-switch/events.jsonl \\
        --since 2026-10-30T00:00:00Z --until 2026-11-02T00:00:00Z

Samples are kept for 24 hours only (SAMPLE_RETENTION_S), so swaps older than
that can be described from the feed alone; the fixture says which."""
import argparse
import json
import os
import re
from datetime import datetime, timezone

GRACE_S = 300          # ccroll's --grace default, which the live instance ran with
POLL_S = 60            # its --interval default
HERE = os.path.dirname(os.path.abspath(__file__))


def epoch(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--feed", required=True)
    ap.add_argument("--since", default="2026-10-30T00:00:00Z")
    ap.add_argument("--until", default="2026-11-02T00:00:00Z")
    ap.add_argument("--out", default=os.path.join(HERE, "replay_swaps.json"))
    a = ap.parse_args()
    state = json.load(open(os.path.expanduser(a.state)))
    feed = [json.loads(line) for line in open(os.path.expanduser(a.feed)) if line.strip()]
    labels = state.get("signal_labels") or {}
    by_label = {v: k for k, v in labels.items()}
    since, until = epoch(a.since), epoch(a.until)

    # the binding window of each natural swap, from ccroll's own log where it
    # still has the line: "→ <to> (left <from>: weekly 98%)"
    reasons = {}
    for t, msg in state.get("events") or []:
        m = re.match(r"→ (\S+) \(left (\S+): (.*)\)$", msg)
        if m:
            reasons[round(t)] = (labels.get(m.group(2)), m.group(3))

    dones = [e for e in feed if e.get("event") == "switch_done"]
    swaps, feed_only = [], []
    for prev, done in zip(dones, dones[1:]):
        t_done = epoch(done["ts"])
        if not since <= t_done < until:
            continue
        t_prev = epoch(prev["ts"])
        stint = [e for e in feed if prev["seq"] < e["seq"] < done["seq"]]
        expected = [e for e in stint if e["event"] == "switch_expected"]
        why = next((r for k, r in reasons.items() if abs(k - t_done) <= 2), None)
        kind = None
        if why:
            # a limit reason reads "<window> 98%" or "<window> ≈98% (…)"; a
            # pre-emptive move ("weekly resets 2d sooner…") or a keypress is not one
            m = re.match(r"(?:peak-hour hold ended · )?(session|weekly|\w+ weekly) ≈?\d", why[1])
            kind = ("session" if m.group(1) == "session" else "weekly") if m else None
        natural = kind is not None or (why is None and bool(expected))
        if not natural:
            continue                      # a pre-emptive move, never announced by design
        label = prev["account"]
        entry = {
            "seq": done["seq"], "swap_utc": done["ts"], "account": label,
            "stint_start_utc": prev["ts"], "binding": kind,
            "feed": [{k: v for k, v in e.items() if k != "account"} for e in stint + [done]],
            "old_lead_s": (t_done - epoch(expected[0]["ts"])) if expected else None,
        }
        name = by_label.get(label)
        series = ((state.get("samples") or {}).get(name) or {}) if name else {}
        weekly = [p for p in series.get("weekly") or [] if t_prev <= p[0] <= t_done + 2 * 3600]
        before = [p for p in weekly if p[0] <= t_done]
        reading = (state.get("readings") or {}).get(name) or {}
        if before and reading.get("weekly_reset", 0) > t_done:
            entry["coverage"] = ("full" if before[0][0] <= t_prev + GRACE_S + 3 * POLL_S
                                 else "partial")
            entry["samples"] = {k: [p for p in series.get(k) or []
                                    if t_prev <= p[0] <= t_done + 2 * 3600]
                                for k in ("session", "weekly", "scoped") if series.get(k)}
            entry["resets"] = {k: reading.get(f"{k}_reset")
                               for k in ("session", "weekly", "scoped")
                               if reading.get(f"{k}_reset")}
            entry["swap_s"] = t_done
            entry["stint_start_s"] = t_prev
            swaps.append(entry)
        else:
            entry["coverage"] = "feed-only"
            feed_only.append(entry)
    blob = {"note": "anonymised: accounts are feed labels; built by build_replay_fixture.py",
            "grace_s": GRACE_S, "interval_s": POLL_S, "replayable": swaps, "feed_only": feed_only}
    text = json.dumps(blob, indent=1)
    if "@" in text:
        raise SystemExit("refusing to write a fixture containing '@'")
    with open(a.out, "w") as fh:
        fh.write(text + "\n")
    print(f"{len(swaps)} replayable, {len(feed_only)} feed-only -> {a.out}")


if __name__ == "__main__":
    main()
