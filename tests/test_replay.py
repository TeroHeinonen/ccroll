"""Replay of the weekly-limited swaps of Oct 31 – Nov 1 2026 from the recorded
samples and signal feed (tests/fixtures/replay_swaps.json, anonymised).

The baseline replay must reproduce what actually happened — the recorded
swap moment and the recorded switch_expected lead — or the harness is not
replaying the real thing.  The new rules must then announce every one of
those swaps ahead of time, never swap later, and in the Nov 1 blackout
(seq 3997) rotate before the account can have reached 100%."""
import json
import unittest

import _ccroll as T
import replay


class Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = replay.load_fixture()
        if T.load_baseline() is None:
            raise unittest.SkipTest("no baseline (git history or CCROLL_BASELINE)")
        cls.rows = {r["entry"]["seq"]: r for r in replay.table(cls.fx)}

    def test_fixture_is_anonymous(self):
        with open(replay.FIXTURE) as fh:
            self.assertNotIn("@", fh.read())
        for e in self.fx["replayable"] + self.fx["feed_only"]:
            self.assertRegex(e["account"], r"^acct-\d\d$")

    def test_baseline_reproduces_the_record(self):
        for seq, r in self.rows.items():
            e, old = r["entry"], r["old"]
            with self.subTest(seq=seq):
                self.assertIsNotNone(old["swap"])
                self.assertLessEqual(abs(old["swap"] - e["swap_s"]), 2)
                if e["old_lead_s"] is None:
                    self.assertEqual(old["expected"], [])
                else:
                    self.assertLessEqual(abs(old["lead"] - e["old_lead_s"]), 2)

    def test_every_swap_is_announced_and_never_later(self):
        for seq, r in self.rows.items():
            old, new = r["old"], r["new"]
            with self.subTest(seq=seq):
                self.assertIsNotNone(new["swap"])
                self.assertLessEqual(new["swap"], old["swap"])
                self.assertTrue(new["expected"])
                self.assertGreater(new["lead"], old["lead"] or 0)

    def test_fresh_read_swaps_happen_when_they_did(self):
        """Where every poll of the approach answered, E changes nothing."""
        for seq in (3984, 3989):
            r = self.rows[seq]
            self.assertEqual(r["new"]["swap"], r["old"]["swap"], seq)
            self.assertNotIn("conservative", r["new"]["reason"])

    def test_blackout_rotates_before_the_account_can_be_full(self):
        """3997: read at 93% at the last good poll, every read refused after it,
        the old projection swapped unannounced 34 minutes later at a 9%/h fit —
        after the user's sessions had hit the limit — and the first read after
        the swap said 100%.  The earliest the account can have been full is
        what its own fastest recorded burn this stint gives from the top of
        that 93 (computed here with the baseline's fit, not the new code)."""
        r = self.rows[3997]
        e = r["entry"]
        base = T.load_baseline()
        pts = [p for p in e["samples"]["weekly"] if p[0] <= e["swap_s"]]
        start = e["stint_start_s"] + self.fx["grace_s"]
        series = [p for p in pts if p[0] >= start]
        fits = [base.burn_fit(series[:i + 1]) for i in range(len(series))]
        fastest = max(f[0] for f in fits if f and f[1] >= base.BURN_SETTLED_SPAN_S)
        t_last, pct_last = series[-1]
        earliest_full = t_last + (100 - (pct_last + base.USAGE_PCT_STEP)) / fastest * 3600
        new = r["new"]
        self.assertIn("conservative", new["reason"])
        self.assertLess(new["swap"], earliest_full)
        self.assertLess(new["swap"], e["swap_s"])
        self.assertGreaterEqual(new["lead"], replay._ccroll.load().SIGNAL_EXPECT_LEAD_S - 1)
        self.assertEqual(r["old"]["expected"], [])          # the record: unannounced
        after = replay.first_after(e)
        self.assertEqual(after[1], 100)

    def test_feed_only_swaps_are_listed(self):
        """Older swaps have no samples left (24 h retention): kept in the
        fixture with their recorded leads, and said to be not replayable."""
        self.assertTrue(self.fx["feed_only"])
        for e in self.fx["feed_only"]:
            self.assertNotIn("samples", e)


if __name__ == "__main__":
    unittest.main()
