"""A: every swap is announced from what rotation acts on, every tick.
B: the warn decision allows for the whole-percent rounding of a reading."""
import unittest
from datetime import datetime, timezone

import _ccroll as T

NAME = "acct"


def _ts(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def burn(mod, state, t_end: float, pct_end: float, rate_h: float, interval: float = 60,
         n: int = 30, key: str = "weekly", clock=None) -> None:
    """Fill `key`'s series with polls ending at (t_end, pct_end), rising at rate_h, as integers."""
    for i in range(n, -1, -1):
        t = t_end - i * interval
        clock.t = t
        mod.append_sample(state, NAME, key, float(int(pct_end - rate_h * i * interval / 3600)))
    clock.t = t_end


class RoundingWarn(unittest.TestCase):
    """B: a 97 can be 97.99, so at 9%/h the swap at 98 may be seconds away."""

    def setUp(self):
        self.m = T.load()
        self.clock = T.Clock().attach(self.m)
        self.sent = T.capture_signals(self.m)
        self.cfg = T.cfg(self.m)
        t0 = self.clock.t
        self.state = T.lane_state(self.m, NAME, t0 - 4 * 3600)
        burn(self.m, self.state, t0, 97, 9.0, clock=self.clock)
        self.u = T.reading(self.m, t0, weekly=97)

    def test_reading_one_step_below_is_announced(self):
        own = self.m.predicted_switch_eta(self.state, NAME, self.u, self.cfg)
        self.assertGreater(own[0], self.m.SIGNAL_EXPECT_LEAD_S)     # the old rule stays quiet
        self.m.maybe_signal_expected(self.cfg, self.state, NAME, self.u, True)
        self.assertEqual(len(self.sent), 1)
        sent_at, eta, pct = self.sent[0]
        # the ETA sent is the reading's own prediction: the latest the swap comes
        self.assertAlmostEqual(eta, self.clock.t + own[0], places=3)
        self.assertEqual(pct, 97)

    def test_baseline_did_not_announce(self):
        base = T.load_baseline()
        if base is None:
            self.skipTest("no git history")
        T.Clock(self.clock.t).attach(base)
        sent = T.capture_signals(base)
        st = T.lane_state(base, NAME, self.clock.t - 4 * 3600)
        st["samples"] = self.state["samples"]
        u = T.reading(base, self.clock.t, weekly=97)
        base.maybe_signal_expected(self.cfg, st, NAME, u, True)
        self.assertEqual(sent, [])

    def test_no_repeat_while_reading_holds(self):
        """The same 97 re-read every minute, projected every second: one line."""
        t0 = self.clock.t
        for k in range(0, 300):
            self.clock.t = t0 + k
            if k and k % 60 == 0:
                self.m.append_sample(self.state, NAME, "weekly", 97.0)
                self.u = T.reading(self.m, self.clock.t, weekly=97)
            u = self.m.projected_usage(self.state, NAME, self.u)
            self.m.maybe_signal_expected(self.cfg, self.state, NAME, u, True)
        self.assertEqual(len(self.sent), 1)

    def test_far_from_threshold_is_quiet(self):
        u = T.reading(self.m, self.clock.t, weekly=90)
        self.m.maybe_signal_expected(self.cfg, self.state, NAME, u, True)
        self.assertEqual(self.sent, [])

    def test_scheduled_only_behaves_as_before(self):
        """A scheduled swap carries no rounding: announced once, re-sent only on drift."""
        st = T.lane_state(self.m, NAME, self.clock.t - 4 * 3600)
        when = self.clock.t + 100
        self.m.maybe_signal_expected(self.cfg, st, NAME, None, True, scheduled=when)
        self.m.maybe_signal_expected(self.cfg, st, NAME, None, True, scheduled=when + 150)
        self.assertEqual(len(self.sent), 1)
        self.m.maybe_signal_expected(self.cfg, st, NAME, None, True, scheduled=when - 120)
        self.assertEqual(len(self.sent), 1)        # 120 s earlier: within the drift
        self.clock.t += 10
        self.m.maybe_signal_expected(self.cfg, st, NAME, None, True, scheduled=self.clock.t + 170)
        self.assertEqual(len(self.sent), 1)
        st["signal_expect_eta"] = when + 400
        self.m.maybe_signal_expected(self.cfg, st, NAME, None, True, scheduled=when)
        self.assertEqual(len(self.sent), 2)


class AnnounceEveryTick(unittest.TestCase):
    """A: the dashboard's per-tick rotation path announces before it swaps."""

    def setUp(self):
        self.m = T.load()
        self.clock = T.Clock().attach(self.m)
        self.cfg = T.cfg(self.m)

    def _master(self, swapped_ago: float):
        mm = T.bare_master(self.m, self.cfg, NAME)
        ls = mm.local.ls
        ls["last_swap"] = ls["active_since"] = self.clock.t - swapped_ago
        ls["grace_until"] = ls["last_swap"] + self.cfg.grace
        return mm

    def test_blackout_swap_is_announced_ahead(self):
        """The Nov 1 shape: read at 93% with a fit of 9%/h, the stint having
        burned far harder earlier, then nothing but refusals.  The old path
        only announced from a good read, so the swap the projection fired
        came unannounced; now the conservative swap is announced a lead
        ahead of it."""
        mm = self._master(5 * 3600)
        st = mm.state
        t_read = self.clock.t
        # an hour at 35%/h two hours ago, then 9%/h up to the last read
        burn(self.m, st, t_read - 2 * 3600, 60, 35.0, n=45, clock=self.clock)
        burn(self.m, st, t_read, 93, 9.0, n=120, clock=self.clock)
        good = T.reading(self.m, t_read, weekly=93)
        mm.last_good[NAME] = good
        mm.usages[NAME] = good
        refused_from = t_read + self.cfg.interval
        for k in range(1, 3600):
            self.clock.t = t_read + k
            if self.clock.t >= refused_from:
                mm.usages[NAME] = T.refused(self.m, self.clock.t)
            mm.maybe_rotate(mm.local)
            if mm.started:
                break
        self.assertTrue(mm.started, "the blackout never rotated")
        started_at, reason = mm.started[0]
        self.assertIn("conservative", reason)
        self.assertIn("usage read: rate_limit_error", reason)
        events = [e for e in T.feed_events(self.cfg) if e["event"] == "switch_expected"]
        self.assertTrue(events, "the swap was not announced")
        first = _ts(events[0]["ts"])
        self.assertGreaterEqual(started_at - first, self.m.SIGNAL_EXPECT_LEAD_S - 2)
        self.assertLessEqual(abs(_ts(events[0]["eta_utc"]) - started_at), 2)
        self.assertEqual(len(events), 1)

    def test_swap_nothing_predicted_is_still_announced(self):
        """A static threshold inside the grace has no prediction behind it;
        the rotation start announces it."""
        mm = self._master(60)                 # in the grace
        u = T.reading(self.m, self.clock.t, weekly=99)
        mm.usages[NAME] = mm.last_good[NAME] = u
        mm.maybe_rotate(mm.local)
        self.assertEqual(len(mm.started), 1)
        events = [e for e in T.feed_events(self.cfg) if e["event"] == "switch_expected"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["usage_pct"], 99)
        # the next tick's rotation start (no target was found) does not repeat it
        self.clock.t += 1
        mm.maybe_rotate(mm.local)
        events = [e for e in T.feed_events(self.cfg) if e["event"] == "switch_expected"]
        self.assertEqual(len(events), 1)

    def test_spent_with_nowhere_to_go_is_announced_once(self):
        """Every account spent: the rotation starts every tick and finds no
        target, for half an hour.  One line, not one every few minutes."""
        mm = self._master(5 * 3600)
        st = mm.state
        t0 = self.clock.t
        burn(self.m, st, t0, 99, 9.0, n=60, clock=self.clock)
        for k in range(0, 1800):
            self.clock.t = t0 + k
            if k % 60 == 0:
                self.m.append_sample(st, NAME, "weekly", 99.0)
                mm.usages[NAME] = mm.last_good[NAME] = T.reading(self.m, self.clock.t, weekly=99)
            mm.maybe_rotate(mm.local)
        self.assertEqual(len(mm.started), 1800)
        events = [e for e in T.feed_events(self.cfg) if e["event"] == "switch_expected"]
        self.assertEqual(len(events), 1)

    def test_next_approach_after_a_passed_one_is_announced(self):
        st = T.lane_state(self.m, NAME, self.clock.t - 4 * 3600)
        sent = T.capture_signals(self.m)
        t0 = self.clock.t
        self.m.maybe_signal_expected(self.cfg, st, NAME, None, True, scheduled=t0 + 60)
        self.clock.t = t0 + 600                   # passed, nothing due: forgotten
        self.m.maybe_signal_expected(self.cfg, st, NAME, None, True)
        self.assertIsNone(st["signal_expect_eta"])
        self.m.maybe_signal_expected(self.cfg, st, NAME, None, True, scheduled=self.clock.t + 60)
        self.assertEqual(len(sent), 2)

    def test_fresh_read_reaching_threshold_announced_one_poll_ahead(self):
        """97 → 98 one poll apart at 9%/h: the 97 already announces it."""
        mm = self._master(5 * 3600)
        st = mm.state
        t0 = self.clock.t
        burn(self.m, st, t0, 97, 9.0, n=60, clock=self.clock)
        mm.usages[NAME] = mm.last_good[NAME] = T.reading(self.m, t0, weekly=97)
        mm.maybe_rotate(mm.local)
        self.assertEqual(mm.started, [])
        self.clock.t = t0 + 60
        self.m.append_sample(st, NAME, "weekly", 98.0)
        mm.usages[NAME] = mm.last_good[NAME] = T.reading(self.m, self.clock.t, weekly=98)
        mm.maybe_rotate(mm.local)
        self.assertEqual(len(mm.started), 1)
        events = [e for e in T.feed_events(self.cfg) if e["event"] == "switch_expected"]
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(_ts(events[0]["ts"]), t0)


if __name__ == "__main__":
    unittest.main()
