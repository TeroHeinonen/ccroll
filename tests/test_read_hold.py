"""D: read pacing through a rate-limit blackout — Retry-After honoured to the
second, doubled only when the first read after it is refused again, keyed
by the account so a token refresh cannot drop it, and the first read after
it sent the moment it ends."""
import json
import os
import tempfile
import unittest

import _ccroll as T


class FakeServer:
    """Stands in for `_send`: answers from a script and counts what was sent."""

    def __init__(self, clock):
        self.clock, self.script, self.sent = clock, [], []

    def answer(self, kind: str, retry_after=None) -> None:
        self.script.append((kind, retry_after))

    def __call__(self, method, url, token, body):
        self.sent.append(self.clock.t)
        kind, ra = self.script.pop(0) if self.script else ("ok", None)
        if kind == "429":
            headers = {"Retry-After": str(ra)} if ra is not None else {}
            blob = {"type": "error", "error": {"type": "rate_limit_error", "message": "Rate limited."}}
            return 429, json.dumps(blob).encode(), None, headers
        blob = {"limits": [{"kind": "weekly_all", "percent": 50,
                            "resets_at": "2099-01-01T00:00:00Z", "is_active": False}]}
        return 200, json.dumps(blob).encode(), None, {}


class Hold(unittest.TestCase):
    def setUp(self):
        self.m = T.load()
        self.clock = T.Clock().attach(self.m)
        self.m.PACER.pace = 0.0
        self.srv = FakeServer(self.clock)
        self.m._send = self.srv
        self.dir = tempfile.mkdtemp(prefix="ccroll_hold_")
        self.path = os.path.join(self.dir, ".credentials.json")
        self._creds("rt-1")

    def _creds(self, refresh: str) -> None:
        with open(self.path, "w") as fh:
            json.dump({"claudeAiOauth": {"accessToken": "at-" + refresh, "refreshToken": refresh,
                                         "expiresAt": (self.clock.t + 86400) * 1000}}, fh)

    def read(self):
        return self.m.usage_for(self.path, account="acct")

    def test_retry_after_honoured_to_the_second(self):
        self.srv.answer("429", 600)
        t0 = self.clock.t
        u = self.read()
        self.assertTrue(u.error.startswith("rate_limit_error"))
        for dt in (1, 60, 300, 599):
            self.clock.t = t0 + dt
            u = self.read()
            self.assertIn("server asked to wait", u.error)
            self.assertEqual(u.held_until, t0 + 600)
        self.assertEqual(len(self.srv.sent), 1)          # nothing sent while held
        self.clock.t = t0 + 600
        u = self.read()
        self.assertIsNone(u.error)
        self.assertEqual(self.srv.sent, [t0, t0 + 600])

    def test_refused_again_after_the_hold_doubles(self):
        t = self.clock.t
        for ra, expect in ((600, 600), (600, 1200), (600, 2400), (5000, 5000)):
            self.srv.answer("429", ra)
            self.clock.t = t
            u = self.read()
            self.assertEqual(u.retry_after, expect)
            self.clock.t = t + expect - 1
            self.assertIsNotNone(self.read().held_until)
            t += expect
        # a good read ends the episode: the next refusal is the server's own again
        self.clock.t = t
        self.assertIsNone(self.read().error)
        self.srv.answer("429", 300)
        self.assertEqual(self.read().retry_after, 300)

    def test_doubled_hold_says_so(self):
        self.srv.answer("429", 60)
        t0 = self.clock.t
        self.read()
        self.clock.t = t0 + 60
        self.srv.answer("429", 60)
        self.read()
        self.clock.t += 1
        self.assertIn("refused again", self.read().error)

    def test_refusal_without_retry_after_keeps_the_cadence(self):
        self.srv.answer("429", None)
        u = self.read()
        self.assertIsNone(u.held_until)
        self.clock.t += 60
        self.read()
        self.assertEqual(len(self.srv.sent), 2)          # asked again at the next poll

    def test_refusal_without_retry_after_after_a_hold_doubles_it(self):
        self.srv.answer("429", 100)
        t0 = self.clock.t
        self.read()
        self.clock.t = t0 + 100
        self.srv.answer("429", None)
        self.assertEqual(self.read().retry_after, 200)

    def test_hold_survives_a_token_refresh(self):
        """The running CLI refreshes the live login whenever it likes; a hold
        keyed by the refresh token was dropped by that, and the next poll
        asked again inside the server's window."""
        self.srv.answer("429", 900)
        t0 = self.clock.t
        self.read()
        self._creds("rt-2")
        self.clock.t = t0 + 120
        u = self.read()
        self.assertIsNotNone(u.held_until)
        self.assertEqual(len(self.srv.sent), 1)

    def test_baseline_dropped_the_hold_on_refresh(self):
        base = T.load_baseline()
        if base is None:
            self.skipTest("no git history")
        clock = T.Clock(self.clock.t).attach(base)
        base.PACER.pace = 0.0
        srv = FakeServer(clock)
        base._send = srv
        srv.answer("429", 900)
        base.usage_for(self.path)
        self._creds("rt-2")
        clock.t += 120
        base.usage_for(self.path)
        self.assertEqual(len(srv.sent), 2)               # read again 13 minutes early

    def test_held_poll_reads_again_when_the_hold_ends(self):
        """The lane's poll lands its first real read on the hold's last second,
        not at whichever poll comes after it."""
        cfg = T.cfg(self.m)
        mm = T.bare_master(self.m, cfg, "acct")
        lr = mm.local
        t0 = self.clock.t
        lr.next_poll = t0 + cfg.interval
        u = self.m.Usage()
        u.error = "rate_limit_error · server asked to wait 25s"
        u.held_until = t0 + 25
        mm.on_polled(lr, "acct", "acct", u, None)
        self.assertEqual(lr.next_poll, t0 + 25)
        # a hold ending after the next poll leaves the cadence alone
        lr.next_poll = t0 + cfg.interval
        u.held_until = t0 + 10 * cfg.interval
        mm.on_polled(lr, "acct", "acct", u, None)
        self.assertEqual(lr.next_poll, t0 + cfg.interval)


if __name__ == "__main__":
    unittest.main()
