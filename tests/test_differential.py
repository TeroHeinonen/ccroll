"""With reads landing (no refusal; a reading older than the interval only
while the next poll is out) every rotation decision is exactly the
baseline's: randomized states, readings and settings, judged by both, with
each reading stamped as far in the past as a real lane's would be.  The
baseline is read with the scoped window rotating in either --by mode
(`baseline_with_scoped_rotation`), the one rule added since."""
import os
import random
import unittest

import _ccroll as T

NAME = "acct"
N = int(os.environ.get("CCROLL_DIFF_N", "4000"))


def scenario(rng: random.Random, t_now: float) -> dict:
    """Everything one judgement depends on, as plain data both modules load."""
    interval = rng.choice([15, 30, 60, 60, 60, 120, 300])
    sc = {
        "args": dict(threshold=rng.choice([90, 95, 97, 98, 99]),
                     weekly_threshold=rng.choice([95, 97, 98]),
                     scoped_threshold=rng.choice([90, 97]),
                     interval=interval,
                     lead=rng.choice([None, 30, 60, 180, 600]),
                     grace=rng.choice([0, 300, 600]),
                     by=rng.choice(["weekly", "scoped"])),
        "keys": [k for k in ("session", "weekly", "scoped") if rng.random() < 0.8] or ["weekly"],
    }
    swapped = t_now - rng.uniform(0, 6 * 3600)
    sc["swapped"] = swapped
    sc["lane_burn"] = ({k: rng.uniform(0, 80) for k in ("session", "weekly", "scoped")}
                       if rng.random() < 0.5 else None)
    sc["preload"] = [{"t": swapped - rng.uniform(0, 9e4), "name": "x",
                      "session": rng.choice([None, rng.uniform(0, 20)]),
                      "weekly": rng.uniform(0, 5), "scoped": rng.uniform(0, 8)}
                     for _ in range(rng.randint(0, 3))]
    integer = rng.random() < 0.8
    samples = {}
    start = max(swapped, t_now - 5 * 3600) if rng.random() < 0.8 else t_now - rng.uniform(0, 600)
    for key in sc["keys"]:
        pct = rng.uniform(0, 99)
        rate = rng.choice([0, 1, 5, 9, 13, 25, 60, 150])
        pts, t = [], start
        while t < t_now - 1:
            r = rate * (rng.choice([0, 1, 1, 2, 4]) if rng.random() < 0.2 else 1)
            pct = min(100.0, pct + r * interval / 3600 * rng.uniform(0.5, 1.5))
            if rng.random() < 0.01:
                pct = rng.uniform(0, 10)        # a reset
            if rng.random() > 0.1:
                pts.append([t, float(int(pct)) if integer else round(pct, 3)])
            t += interval * rng.uniform(1.0, 1.02)
        samples[key] = pts
    sc["samples"] = samples
    # When the judgement happens relative to the reading's stamp, as a lane
    # really sees it: a poll is stamped when it is asked for, then waits for
    # a pacer slot (0–20 s behind a scan or a refresh) and its round trip
    # (0.1–3 s), and the next poll goes out an interval after this one did —
    # or on the first tick after this one landed, if that was later.  From
    # then until it answers, that next poll is in flight.
    q = rng.uniform(0, 20) + rng.uniform(0.1, 3)
    q_next = rng.uniform(0, 20) + rng.uniform(0.1, 3) + rng.uniform(0, 1.5)   # + tick slip
    # The tick that first finds the poll due sends it before it judges
    # (`step`), so from `next_out` on every judgement has it in flight.
    next_out = max(interval, q)
    since = rng.uniform(q, next_out + q_next)
    fetched = t_now - since
    polling = since >= next_out
    if rng.random() < 0.1:
        # the target's read before the swap, standing in while the first
        # poll after the swap (sent at once) is out
        fetched = min(fetched, swapped - rng.uniform(0, 30))
        polling = polling or t_now - fetched > interval
    sc["polling"] = polling
    reading = {"fetched_at": fetched, "status": rng.choice(["ok"] * 30 + ["rejected", "allowed_warning"])}
    for key in sc["keys"]:
        last = samples[key][-1][1] if samples[key] else rng.uniform(0, 100)
        if rng.random() < 0.3:
            last = rng.choice([49, 50, 89, 94, 95, 96, 97, 97.5, 98, 99, 100])
        reading[f"{key}_pct"] = last
        roll = rng.random()
        reading[f"{key}_reset"] = (None if roll < 0.05 else
                                   t_now - rng.uniform(1, 3600) if roll < 0.15 else
                                   t_now + rng.uniform(1, 7 * 86400))
    sc["reading"] = reading
    return sc


def build(mod, sc, t_now):
    clock = T.Clock(t_now).attach(mod)
    cfg = T.cfg(mod, **sc["args"])
    st = T.lane_state(mod, NAME, sc["swapped"], grace=cfg.grace)
    st["lane_burn"] = sc["lane_burn"]
    st["preload"] = [dict(p) for p in sc["preload"]]
    st["samples"] = {NAME: {k: [list(p) for p in v] for k, v in sc["samples"].items()}}
    u = mod.Usage()
    for k, v in sc["reading"].items():
        setattr(u, k, v)
    return clock, cfg, st, u


def windows(u):
    return None if u is None else (u.session_pct, u.weekly_pct, u.scoped_pct, u.projected_full)


class Differential(unittest.TestCase):
    def test_fresh_reads_rotate_exactly_as_before(self):
        base = T.baseline_with_scoped_rotation()
        if base is None:
            self.skipTest("no baseline (git history or CCROLL_BASELINE)")
        new = T.load()
        rng = random.Random(int(os.environ.get("CCROLL_DIFF_SEED", "20261101")))
        fired = 0
        for i in range(N):
            t_now = 1_800_000_000.0 + rng.uniform(0, 1e6)
            sc = scenario(rng, t_now)
            _, cfg_b, st_b, u_b = build(base, sc, t_now)
            u_old, age = base.rotation_usage({NAME: u_b}, NAME, u_b)
            u_old = base.projected_usage(st_b, NAME, u_old)
            old = base.is_exhausted(u_old, cfg_b) or base.about_to_exhaust(st_b, NAME, u_old, cfg_b)
            _, cfg_n, st_n, u_n = build(new, sc, t_now)
            u_new, got = new.judge_rotation(st_n, NAME, u_n, {NAME: u_n}, cfg_n,
                                            polling=sc["polling"])
            self.assertFalse(u_new.projected_conservative, i)
            self.assertEqual(got, old, f"scenario {i}: {sc}")
            self.assertEqual(windows(u_new), windows(u_old), f"scenario {i}")
            fired += bool(old)
        # the scenarios must actually exercise both outcomes
        self.assertGreater(fired, N // 10)
        self.assertLess(fired, N - N // 10)

    def test_master_tick_rotates_exactly_as_before(self):
        """The dashboard's own per-tick path, both versions, same inputs."""
        base = T.baseline_with_scoped_rotation()
        if base is None:
            self.skipTest("no baseline (git history or CCROLL_BASELINE)")
        new = T.load()
        rng = random.Random(11)
        for i in range(N // 8):
            t_now = 1_800_000_000.0 + rng.uniform(0, 1e6)
            sc = scenario(rng, t_now)
            sc["args"]["no_signal"] = True
            out = []
            for mod in (base, new):
                _, cfg, st, u = build(mod, sc, t_now)
                mm = T.bare_master(mod, cfg, NAME)
                for k, v in st.items():
                    mm.state[k] = v
                mm.usages[NAME] = mm.last_good[NAME] = u
                mm.local.polling = t_now if sc["polling"] else None
                mm.maybe_rotate(mm.local)
                out.append([r for _, r in mm.started])
            self.assertEqual(out[1], out[0], f"scenario {i}")


if __name__ == "__main__":
    unittest.main()
