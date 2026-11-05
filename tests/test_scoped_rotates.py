"""The per-model (scoped) weekly window rotates in either `--by` mode.  A
full Fable window refuses every Fable request, so an account whose scoped
window is spent is as spent as one with a full session window: it rotates
away, it never gets picked as a target, and the fleet waits for its reset.
`--by` only decides which weekly window ranks the next account."""
import unittest

import _ccroll as T

NAME = "acct"
DAY = 86400.0


class ScopedRotates(unittest.TestCase):
    def setUp(self):
        self.m = T.load()
        self.clock = T.Clock().attach(self.m)
        self.t = self.clock.t
        self.cfg = T.cfg(self.m, by="weekly")

    def read(self, session=10.0, weekly=20.0, scoped=None, **kw):
        u = T.reading(self.m, self.t, weekly=weekly, scoped=scoped, session=session, **kw)
        u.scoped_label = "Fable"
        return u

    def test_scoped_at_its_threshold_exhausts_in_weekly_mode(self):
        thr = self.cfg.scoped_threshold
        spent = self.read(scoped=thr)
        reason = self.m.is_exhausted(spent, self.cfg)
        self.assertIsNotNone(reason)
        self.assertTrue(reason.startswith("fable weekly"), reason)
        self.assertIn(f"{thr:.0f}%", reason)
        # just under the threshold, with session and weekly low, is not spent
        self.assertIsNone(self.m.is_exhausted(self.read(scoped=thr - 1), self.cfg))
        # the scoped figure counts towards the utilisation the signal carries
        self.assertEqual(self.m.rotation_utilisation(spent, self.cfg), thr)

    def test_blocked_until_is_the_scoped_reset_when_it_reads_full(self):
        reset = self.t + 2 * DAY
        full = self.read(scoped=100.0, scoped_reset=reset)
        self.assertEqual(self.m.blocked_until(full, self.cfg), reset)
        # over the rotation threshold but not full: spent for rotation, yet
        # still serving requests, so nothing blocks
        over = self.read(scoped=self.cfg.scoped_threshold, scoped_reset=reset)
        self.assertIsNone(self.m.blocked_until(over, self.cfg))
        # judged a lead ahead: still blocked while the reset lies beyond it
        self.assertEqual(self.m.blocked_until(full, self.cfg, reset - 60), reset)
        self.assertIsNone(self.m.blocked_until(full, self.cfg, reset + 1))

    def test_a_spent_scoped_window_is_never_a_target(self):
        usages = {"spent": self.read(session=5.0, weekly=30.0, scoped=100.0),
                  "fine": self.read(session=10.0, weekly=60.0, scoped=50.0)}
        strict, ok = self.m.candidates(usages, self.cfg, exclude=None)
        self.assertEqual([n for n, _ in ok], ["fine"])
        self.assertEqual([n for n, _ in strict], ["fine"])
        self.assertEqual(self.m.pick_target(usages, self.cfg, exclude=None), "fine")
        # the only account left is the spent one: nowhere to go
        self.assertIsNone(self.m.pick_target({"spent": usages["spent"]}, self.cfg, exclude=None))
        # nearly spent on the scoped window alone: not comfortable, but usable
        warm = {"warm": self.read(session=5.0, weekly=30.0, scoped=92.0)}
        strict, ok = self.m.candidates(warm, self.cfg, exclude=None)
        self.assertEqual([n for n, _ in ok], ["warm"])
        self.assertEqual(strict, [])

    def test_recovery_moment_includes_the_scoped_reset(self):
        scoped_reset = self.t + 3 * DAY
        u = self.read(session=10.0, weekly=20.0, scoped=98.0, scoped_reset=scoped_reset)
        self.assertEqual(self.m.recovery_moment(u, self.cfg, self.t), scoped_reset)
        # every binding limit must have reset: the latest one wins
        both = self.read(session=10.0, weekly=99.0, weekly_reset=self.t + DAY,
                         scoped=98.0, scoped_reset=scoped_reset)
        self.assertEqual(self.m.recovery_moment(both, self.cfg, self.t), scoped_reset)
        later = self.read(session=10.0, weekly=99.0, weekly_reset=self.t + 5 * DAY,
                          scoped=98.0, scoped_reset=scoped_reset)
        self.assertEqual(self.m.recovery_moment(later, self.cfg, self.t), self.t + 5 * DAY)

    def test_threshold_etas_carry_a_scoped_entry(self):
        st = T.lane_state(self.m, NAME, self.t - 3600)
        u = self.read(scoped=40.0)
        rows = {key: (label, pct) for key, label, pct, _, _ in
                self.m.threshold_etas(st, NAME, u, self.cfg)}
        self.assertIn("scoped", rows)
        self.assertEqual(rows["scoped"], ("fable weekly", 40.0))
        self.assertEqual(set(rows), {"session", "weekly", "scoped"})
        limit = {key for key, *_ in self.m.limit_etas(st, NAME, u, self.cfg)}
        self.assertEqual(limit, {"session", "weekly", "scoped"})

    def test_a_scoped_window_whose_reset_has_passed_does_not_count(self):
        stale = self.read(scoped=100.0, scoped_reset=self.t - 60)
        self.assertIsNone(self.m.is_exhausted(stale, self.cfg))
        self.assertIsNone(self.m.blocked_until(stale, self.cfg))
        self.assertIsNone(self.m.recovery_moment(stale, self.cfg, self.t))
        _, ok = self.m.candidates({"a": stale}, self.cfg, exclude=None)
        self.assertEqual([n for n, _ in ok], ["a"])
        # the same reading judged before its reset is spent
        self.assertIsNotNone(self.m.is_exhausted(stale, self.cfg, self.t - 120))
        self.assertEqual(self.m.blocked_until(stale, self.cfg, self.t - 120), self.t - 60)

    def test_scoped_mode_still_rotates_on_every_window(self):
        cfg = T.cfg(self.m, by="scoped")
        reset = self.t + 2 * DAY
        spent = self.read(scoped=cfg.scoped_threshold, scoped_reset=reset)
        self.assertTrue(self.m.is_exhausted(spent, cfg).startswith("fable weekly"))
        full = self.read(scoped=100.0, scoped_reset=reset)
        self.assertEqual(self.m.blocked_until(full, cfg), reset)
        self.assertEqual(self.m.recovery_moment(full, cfg, self.t), reset)
        self.assertIsNone(self.m.is_exhausted(self.read(scoped=cfg.scoped_threshold - 1), cfg))
        weekly = self.read(weekly=cfg.weekly_threshold, scoped=10.0)
        self.assertTrue(self.m.is_exhausted(weekly, cfg).startswith("weekly"))
        session = self.read(session=cfg.threshold, scoped=10.0)
        self.assertTrue(self.m.is_exhausted(session, cfg).startswith("session"))
        usages = {"spent": full, "fine": self.read(session=10.0, weekly=60.0, scoped=50.0)}
        self.assertEqual(self.m.pick_target(usages, cfg, exclude=None), "fine")


if __name__ == "__main__":
    unittest.main()
