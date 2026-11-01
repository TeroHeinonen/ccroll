"""Replay a recorded stint through ccroll's rotation and signal rules.

A stint is one account's time as the live account, from the swap onto it
to the swap away: its polls (the burn samples ccroll kept, each a good
read), and the polls that came back refused.  The replay drives the same
functions the dashboard calls, with ccroll's clock set to the recorded
moments:

- old (the baseline commit): `maybe_signal_expected` at each good poll with
  the raw reading (what `take_reading` did), and the rotation judgement every
  tick from the projected reading;
- new: `judge_rotation` every tick, the announcement from the usage it
  acts on, and `signal_rotation_start` when it fires.

Run directly for the table:  python3 tests/replay.py"""
import copy
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ccroll  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "replay_swaps.json")
NAME = "acct"                             # the account's name inside the replay


def load_fixture() -> dict:
    with open(FIXTURE) as fh:
        return json.load(fh)


def _polls(entry: dict) -> list:
    """[(t, {window: pct})] for every good read before the swap."""
    swap = entry["swap_s"]
    by_t: dict = {}
    for key, pts in entry["samples"].items():
        for t, p in pts:
            if t <= swap:
                by_t.setdefault(t, {})[key] = p
    return sorted(by_t.items())


def first_after(entry: dict) -> tuple | None:
    """The first read after the swap: (t, weekly pct)."""
    pts = [p for p in entry["samples"].get("weekly", []) if p[0] > entry["swap_s"]]
    return tuple(pts[0]) if pts else None


def _usage(mod, t: float, wins: dict, resets: dict):
    u = mod.Usage()
    u.fetched_at = t
    u.status = "ok"
    for key in ("session", "weekly", "scoped"):
        if key in wins:
            setattr(u, f"{key}_pct", wins[key])
            setattr(u, f"{key}_reset", resets.get(key))
    return u


def run(mod, entry: dict, new: bool, refuse_after: float | None = None) -> dict:
    """Replay one stint.  `refuse_after`: every poll after this moment is
    refused (a blackout); by default the recorded polls are all there is, and
    a gap between them is simply time nobody looked.  Returns the moment the
    rules would have started the rotation, the switch_expected lines sent
    before it, and the reason."""
    clock = _ccroll.Clock(entry["stint_start_s"]).attach(mod)
    sent = _ccroll.capture_signals(mod)
    cfg = _ccroll.cfg(mod)
    start, swap_rec = entry["stint_start_s"], entry["swap_s"]
    state = _ccroll.lane_state(mod, NAME, start, grace=cfg.grace)
    state["samples"] = {}
    polls = _polls(entry)
    resets = {k: v for k, v in entry.get("resets", {}).items()}
    # A poll goes out `interval` after the one before it went out and its
    # sample lands when it answers, so recorded samples sit a little more than
    # an interval apart; while one is in flight the lane is busy and nothing is
    # judged.  A sample missing from that cadence is a poll that failed (only
    # good reads are kept): it is replayed as a refusal landing on time.
    filled = []
    for k, (pt, wins) in enumerate(polls):
        if filled:
            prev = filled[-1][0]
            nxt = prev + cfg.interval
            while pt - nxt > cfg.interval / 2:
                filled.append((nxt, None))
                nxt += cfg.interval
        filled.append((pt, wins))
    polls = filled
    if refuse_after is not None:
        polls = [p for p in polls if p[0] <= refuse_after]
        t = polls[-1][0]
        while t + cfg.interval < swap_rec + 3600:
            t += cfg.interval
            polls.append((t, None))       # a refused read
    last_good: dict = {}
    live = None
    i = 0
    t = polls[0][0]
    end = swap_rec + 3600
    fired = None
    while t <= end:
        clock.t = t
        while i < len(polls) and polls[i][0] <= t:
            pt, wins = polls[i]
            i += 1
            clock.t = pt
            if wins is None:
                live = _ccroll.refused(mod, pt, "rate_limit_error · server asked to wait 3600s")
            else:
                live = _usage(mod, pt, wins, resets)
                last_good[NAME] = live
                mod.record_samples(state, {NAME: live})
                if not new:
                    mod.maybe_signal_expected(cfg, state, NAME, live, True)
            clock.t = t
        due = (live.fetched_at if live is not None else t) + cfg.interval
        in_flight = i < len(polls) and due <= t < polls[i][0] and polls[i][0] - due <= cfg.interval / 2
        if live is not None and not in_flight:
            if new:
                u_act, why = mod.judge_rotation(state, NAME, live, last_good, cfg)
                mod.maybe_signal_expected(cfg, state, NAME, u_act, True)
                if why:
                    mod.signal_rotation_start(cfg, state, NAME, u_act, True)
            else:
                u_act, age = mod.rotation_usage(last_good, NAME, live)
                u_act = mod.projected_usage(state, NAME, u_act) if u_act else None
                why = mod.is_exhausted(u_act, cfg) or mod.about_to_exhaust(state, NAME, u_act, cfg)
            if why:
                fired = (t, why)
                break
        # one-second ticks where a decision can happen; elsewhere only the polls
        hot = live is not None and max(v for v in (last_good[NAME].windows().values()
                                                   if NAME in last_good else [0]) if v is not None) >= 90
        nxt = polls[i][0] if i < len(polls) else end + 1
        t = t + 1 if hot else max(t + 1, nxt)
    out = {"swap": fired[0] if fired else None, "reason": fired[1] if fired else None,
           "expected": [s for s in sent if fired is None or s[0] <= fired[0]]}
    # the stint's last announcement sequence: the first line of it is the warning a
    # session had (an earlier one for an approach that never came does not count)
    out["first_expected"] = out["expected"][0][0] if out["expected"] else None
    out["lead"] = (out["swap"] - out["first_expected"]) if out["swap"] and out["expected"] else None
    return out


def table(fixture: dict | None = None) -> list:
    fixture = fixture or load_fixture()
    base = _ccroll.load_baseline()
    rows = []
    for entry in fixture["replayable"]:
        blackout = None
        if entry["seq"] == 3997:
            blackout = max(t for t, _ in _polls(entry))
        old = run(base, entry, new=False, refuse_after=blackout) if base else None
        new = run(_ccroll.load(), entry, new=True, refuse_after=blackout)
        rows.append({"entry": entry, "old": old, "new": new, "after": first_after(entry)})
    return rows


def _utc(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S") if t else "—"


def main() -> None:
    fx = load_fixture()
    print(f"{'seq':>5} {'acct':7} {'cov':8} {'recorded':>9} {'feed lead':>9} │ "
          f"{'old swap':>9} {'old lead':>8} │ {'new swap':>9} {'new lead':>8} {'#exp':>4}")
    for r in table(fx):
        e, o, n = r["entry"], r["old"], r["new"]
        fl = "—" if e["old_lead_s"] is None else f"{e['old_lead_s']:.0f}s"
        ol = "—" if not o or o["lead"] is None else f"{o['lead']:.0f}s"
        nl = "—" if n["lead"] is None else f"{n['lead']:.0f}s"
        print(f"{e['seq']:>5} {e['account']:7} {e['coverage']:8} {_utc(e['swap_s']):>9} {fl:>9} │ "
              f"{_utc(o and o['swap']):>9} {ol:>8} │ {_utc(n['swap']):>9} {nl:>8} {len(n['expected']):>4}")
        print(f"      new reason: {n['reason']}")
    for e in fx["feed_only"]:
        fl = "—" if e["old_lead_s"] is None else f"{e['old_lead_s']:.0f}s"
        print(f"{e['seq']:>5} {e['account']:7} feed-only {e['swap_utc']} {e['binding'] or '?':7} "
              f"recorded lead {fl} — no samples kept, not replayable")


if __name__ == "__main__":
    main()
