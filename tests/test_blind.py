"""When is the active account judged blind (E)?  Only when its last read
attempt failed — refused, held, erred — or its reading is older than the
interval with no poll in flight.  A healthy lane whose next poll is waiting
for a pacer slot or an answer is not blind, however old its reading's stamp."""
import json
import os
import random
import unittest

import _ccroll as T

NAME = "acct"
RATE = 9.0                                # %/h, the Nov 1 fit


class Sim:
    """One dashboard lane driven tick by tick on a fake clock, its polls run
    by a fake worker: a poll's reading is stamped when it is asked for (as
    `fetch_usage` does, before the pacer slot), answers after a pacer wait
    and a round trip, and reports what the account was at when the request
    reached the server."""

    def __init__(self, mod, sched: list, ticks: list, t0: float, start_pct: float,
                 hang: tuple | None = None):
        self.m, self.sched, self.ticks, self.t0 = mod, sched, ticks, t0
        self.start_pct, self.hang = start_pct, hang
        self.clock = T.Clock(t0).attach(mod)
        self.cfg = T.cfg(mod)
        os.makedirs(os.path.dirname(self.cfg.live_path), exist_ok=True)
        with open(self.cfg.live_path, "w") as fh:
            json.dump({"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt",
                                         "expiresAt": (t0 + 86400 * 30) * 1000}}, fh)
        self.mm = mm = T.bare_master(mod, self.cfg, NAME)
        ls = mm.local.ls
        ls["last_swap"] = ls["active_since"] = t0 - 5 * 3600
        ls["grace_until"] = ls["last_swap"] + self.cfg.grace
        for i in range(120, 0, -1):         # two hours of minute polls at RATE
            self.clock.t = t0 - 60 * i
            mod.append_sample(mm.state, NAME, "weekly", float(int(self.true(self.clock.t))))
        first = T.reading(mod, t0 - 60, weekly=float(int(self.true(t0 - 60))))
        mm.usages[NAME] = mm.last_good[NAME] = first
        self.pending, self.n = [], 0
        self.blind_judged = []
        real = getattr(mod, "judge_rotation", None)

        def spy(*a, **kw):
            u, why = real(*a, **kw)
            if u is not None and u.projected_conservative:
                self.blind_judged.append(mod.now())
            return u, why
        if real is not None:              # the baseline has no blind judgement to spy on
            mod.judge_rotation = spy
        mm.submit = self.submit

    def true(self, t: float) -> float:
        return self.start_pct + RATE * (t - self.t0) / 3600

    def submit(self, key, what, fn, *args, then=None, urgent=False):
        if key in self.mm.tasks:
            return False
        self.mm.tasks[key] = what
        wait, rtt = self.sched[self.n % len(self.sched)]
        self.n += 1
        asked = self.m.now()
        u = self.m.Usage()                  # stamped now, like fetch_usage
        if self.hang and self.hang[0] <= asked < self.hang[1]:
            u.error = "read failed: timed out"
            lands = asked + self.m.HTTP_TIMEOUT_S
        else:
            u.weekly_pct = float(int(self.true(asked + wait)))
            u.weekly_reset = self.t0 + 4 * 86400
            u.status = "ok"
            lands = asked + wait + rtt
        self.pending.append((lands, key, then, u))
        return True

    def run(self, until: float) -> list:
        mm, lr, k = self.mm, self.mm.local, 0
        t = self.t0
        while t < until and not mm.started:
            t += self.ticks[k % len(self.ticks)]
            k += 1
            self.clock.t = t
            for item in sorted(p for p in self.pending if p[0] <= t):
                self.pending.remove(item)
                _, key, then, u = item
                mm.tasks.pop(key, None)
                then(u, None)
            if t >= lr.next_poll:
                mm.poll(lr)
            mm.maybe_rotate(lr)
            if hasattr(self.m, "reading_blind"):
                view = self.m.merged_view(mm.usages, mm.last_good, [lr.ls], self.cfg.interval,
                                          {NAME} if lr.polling is not None else set())
                if view[NAME].projected_conservative and not self.hang:
                    self.blind_judged.append(("display", t))
        return mm.started


def jitter(seed: int):
    rng = random.Random(seed)
    sched = [(rng.uniform(0, 20), rng.uniform(0.1, 3)) for _ in range(200)]
    ticks = [rng.uniform(0.3, 1.0) if rng.random() < 0.9 else rng.uniform(1.0, 3.0)
             for _ in range(5000)]
    return sched, ticks


class HealthyCadence(unittest.TestCase):
    def test_never_blind_and_decides_as_before(self):
        base = T.load_baseline()
        if base is None:
            self.skipTest("no baseline (git history or CCROLL_BASELINE)")
        for seed in range(12):
            sched, ticks = jitter(seed)
            t0 = 1_800_000_000.0 + seed * 1000
            start = 96.5 + seed % 4 * 0.15     # the reading sits at 97 for a while
            new = Sim(T.load(), sched, ticks, t0, start)
            got = new.run(t0 + 3600)
            old = Sim(base, sched, ticks, t0, start).run(t0 + 3600)
            with self.subTest(seed=seed):
                self.assertEqual(new.blind_judged, [])
                self.assertTrue(old)
                self.assertEqual(got, old)
                self.assertNotIn("conservative", got[0][1])


class FailedOrHeld(unittest.TestCase):
    def setUp(self):
        self.m = T.load()
        self.clock = T.Clock().attach(self.m)
        self.cfg = T.cfg(self.m)

    def test_pure_rule(self):
        u = T.reading(self.m, self.clock.t - self.cfg.interval - 30, weekly=97)
        rb = self.m.reading_blind
        self.assertFalse(rb(u, None, self.cfg.interval, polling=True))   # its poll is out
        self.assertTrue(rb(u, None, self.cfg.interval, polling=False))   # nobody is asking
        self.assertTrue(rb(u, 5.0, self.cfg.interval, polling=True))     # the last attempt failed
        fresh = T.reading(self.m, self.clock.t - 10, weekly=97)
        self.assertFalse(rb(fresh, None, self.cfg.interval))

    def test_restart_reading_not_blind_until_its_first_poll_fails(self):
        st = T.lane_state(self.m, NAME, self.clock.t - 5 * 3600)
        kept = T.reading(self.m, self.clock.t - 3600, weekly=90)
        u, _ = self.m.judge_rotation(st, NAME, kept, {NAME: kept}, self.cfg, polling=True)
        self.assertFalse(u.projected_conservative)
        failed = T.refused(self.m, self.clock.t, "read failed: timed out")
        u, why = self.m.judge_rotation(st, NAME, failed, {NAME: kept}, self.cfg)
        self.assertTrue(u.projected_conservative)

    def test_hung_poll_turns_blind_when_it_fails(self):
        sched, ticks = jitter(99)
        t0 = 1_800_000_000.0
        sim = Sim(T.load(), sched, ticks, t0, 96.2, hang=(t0 + 300, t0 + 10_000))
        started = sim.run(t0 + 3600)
        self.assertTrue(started)
        t, reason = started[0]
        self.assertIn("conservative", reason)
        self.assertIn("usage read: read failed: timed out", reason)
        # nothing was judged blind while the hung poll was still out
        self.assertTrue(all(b >= t0 + 300 + self.m.HTTP_TIMEOUT_S for b in sim.blind_judged
                            if not isinstance(b, tuple)))

    def test_held_poll_is_blind(self):
        """D: a hold means no read can land — the lane is blind at once."""
        mm = T.bare_master(self.m, self.cfg, NAME)
        ls = mm.local.ls
        ls["last_swap"] = ls["active_since"] = self.clock.t - 5 * 3600
        good = T.reading(self.m, self.clock.t - 30, weekly=97)
        mm.usages[NAME] = mm.last_good[NAME] = good
        mm.maybe_rotate(mm.local)
        self.assertEqual(mm.started, [])
        held = self.m.Usage()
        held.error = "rate_limit_error · server asked to wait 1200s"
        held.held_until = self.clock.t + 1200
        mm.local.polling = self.clock.t
        mm.on_polled(mm.local, NAME, NAME, held, None)
        self.assertIsNone(mm.local.polling)
        mm.maybe_rotate(mm.local)
        self.assertEqual(len(mm.started), 1)
        self.assertIn("conservative", mm.started[0][1])
        self.assertIn("server asked to wait", mm.started[0][1])


if __name__ == "__main__":
    unittest.main()
