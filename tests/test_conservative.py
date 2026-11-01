"""E: blind, rotation projects conservatively — the reading at the top of its
whole-percent step, rising at the stint's peak burn — and with reads
landing it is exactly what it was."""
import random
import unittest

import _ccroll as T

NAME = "acct"


class PeakBurn(unittest.TestCase):
    def setUp(self):
        self.m = T.load()
        self.clock = T.Clock().attach(self.m)

    def test_sliding_fit_matches_burn_fit(self):
        rng = random.Random(7)
        for _ in range(50):
            t, p, series = 0.0, 0.0, []
            for _ in range(rng.randint(3, 400)):
                t += rng.uniform(55, 130)
                p += rng.choice([0, 0, 0, 1, 1, 2]) * rng.random()
                series.append([t, round(p)])
            want = []
            for i in range(len(series)):
                fit = self.m.burn_fit(series[:i + 1])
                if fit and fit[1] >= self.m.BURN_SETTLED_SPAN_S:
                    want.append(fit[0])
            got = self.m.settled_fit_rates(series)
            self.assertEqual(len(got), len(want))
            for a, b in zip(got, want):
                self.assertAlmostEqual(a, b, places=6)

    def test_peak_is_the_stints_highest_settled_fit(self):
        st = T.lane_state(self.m, NAME, self.clock.t - 6 * 3600)
        t0 = self.clock.t - 5 * 3600
        pct = 20.0
        for i in range(300):
            self.clock.t = t0 + 60 * i
            pct += (35 if 60 <= i < 120 else 9) / 60
            self.m.append_sample(st, NAME, "weekly", float(int(pct)))
        peak = self.m.peak_burn(st, NAME, "weekly")
        now_rate = self.m.active_burn(st, NAME, "weekly")
        self.assertLess(now_rate, 12)
        self.assertGreater(peak, 30)
        self.assertLess(peak, 40)


class Judge(unittest.TestCase):
    def setUp(self):
        self.m = T.load()
        self.clock = T.Clock().attach(self.m)
        self.cfg = T.cfg(self.m)
        self.st = T.lane_state(self.m, NAME, self.clock.t - 6 * 3600)
        t0 = self.clock.t - 5 * 3600
        pct = 20.0
        for i in range(300):
            self.clock.t = t0 + 60 * i
            pct += (35 if 60 <= i < 120 else 9) / 60
            self.m.append_sample(self.st, NAME, "weekly", float(int(pct)))
            self.m.append_sample(self.st, NAME, "scoped", float(int(pct * 0.6)))
        self.t_read = self.clock.t
        self.read = T.reading(self.m, self.t_read, weekly=93, scoped=56)
        self.peak = self.m.peak_burn(self.st, NAME, "weekly")

    def judge(self, live, t):
        self.clock.t = t
        return self.m.judge_rotation(self.st, NAME, live, {NAME: self.read}, self.cfg)

    def test_fresh_read_is_not_conservative(self):
        u, why = self.judge(self.read, self.t_read + self.cfg.interval - 1)
        self.assertFalse(u.projected_conservative)
        self.assertIsNone(why)

    def test_refused_read_adds_rounding_and_peak_burn(self):
        dt = self.cfg.interval + 1
        u, why = self.judge(T.refused(self.m, self.t_read + dt), self.t_read + dt)
        self.assertTrue(u.projected_conservative)
        self.assertAlmostEqual(u.weekly_pct, 93 + self.m.USAGE_PCT_STEP + self.peak * dt / 3600, places=6)
        self.assertIsNone(why)

    def test_stale_reading_without_error_is_blind_too(self):
        """A restart's kept reading, a poll that never went out."""
        u, _ = self.judge(self.read, self.t_read + self.cfg.interval + 1)
        self.assertTrue(u.projected_conservative)

    def test_margin_grows_with_blind_time_and_rotates(self):
        due = self.t_read + (98 - 93 - self.m.USAGE_PCT_STEP) / self.peak * 3600
        u, why = self.judge(T.refused(self.m, due - 2), due - 2)
        self.assertIsNone(why)
        u, why = self.judge(T.refused(self.m, due + 1), due + 1)
        self.assertIsNotNone(why)
        self.assertTrue(why.startswith("weekly ≈98% conservative (93% read "), why)
        self.assertIn("+ 1% rounding + ", why)
        self.assertIn("%/h peak)", why)
        self.assertIn("· usage read: rate_limit_error", why)
        # the plain projection would have waited for 98 at the current fit
        plain = self.m.projected_usage(self.st, NAME, self.read, due + 1)
        self.assertLess(plain.weekly_pct, 95)

    def test_announcement_agrees_with_the_conservative_swap(self):
        due = self.t_read + (98 - 93 - self.m.USAGE_PCT_STEP) / self.peak * 3600
        t = due - 100
        u, _ = self.judge(T.refused(self.m, t), t)
        eta = self.m.predicted_switch_eta(self.st, NAME, u, self.cfg)
        self.assertAlmostEqual(t + eta[0], due, delta=1)


class SessionAccounts(unittest.TestCase):
    """Accounts that still have the 5-hour window: unchanged while reads
    land, and blind they get the same margin on the session window."""

    def setUp(self):
        self.m = T.load()
        self.base = T.load_baseline()
        self.clock = T.Clock().attach(*(x for x in (self.m, self.base) if x))
        self.cfg = T.cfg(self.m)

    def _state(self, mod):
        st = T.lane_state(mod, NAME, self.clock.t - 3 * 3600)
        t_end = self.clock.t
        for i in range(90, -1, -1):
            self.clock.t = t_end - 60 * i
            mod.append_sample(st, NAME, "session", float(int(96 - 30 * i / 60)))
            mod.append_sample(st, NAME, "weekly", float(int(60 - 3 * i / 60)))
        self.clock.t = t_end
        return st

    def test_fresh_session_reading_unchanged(self):
        if self.base is None:
            self.skipTest("no git history")
        t0 = self.clock.t
        for dt in range(0, self.cfg.interval):
            for pct in (90, 95, 96, 97, 98):
                self.clock.t = t0
                st_new, st_old = self._state(self.m), self._state(self.base)
                u = T.reading(self.m, t0, session=pct, weekly=60)
                self.clock.t = t0 + dt
                got_u, got = self.m.judge_rotation(st_new, NAME, u, {NAME: u}, self.cfg)
                ub = T.reading(self.base, t0, session=pct, weekly=60)
                old_u = self.base.projected_usage(st_old, NAME, ub)
                old = self.base.is_exhausted(old_u, self.cfg) or \
                    self.base.about_to_exhaust(st_old, NAME, old_u, self.cfg)
                self.assertEqual(got, old, (pct, dt))
                self.assertEqual(got_u.session_pct, old_u.session_pct)

    def test_blind_session_window_gets_the_margin(self):
        st = self._state(self.m)
        u = T.reading(self.m, self.clock.t, session=97, weekly=60)
        self.clock.t += self.cfg.interval + 1
        got_u, why = self.m.judge_rotation(st, NAME, T.refused(self.m, self.clock.t), {NAME: u}, self.cfg)
        self.assertGreaterEqual(got_u.session_pct, 98)
        self.assertTrue(why.startswith("session ≈"), why)


if __name__ == "__main__":
    unittest.main()
