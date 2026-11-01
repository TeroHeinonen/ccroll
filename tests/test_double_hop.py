"""Rotation lands where pre-emption would leave it, so no swap is a stopover.

On Nov 1 three rotations in a row went to ops@ (the fastest-perishing
headroom) and, the moment the grace ended, pre-emption moved the lane on to
the earliest reset — a second preload each time.  pick_target now orders by
pre-emption's own rule whenever pre-emption could act after the swap."""
import os
import random
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ccroll as T  # noqa: E402
import fleetsim  # noqa: E402

# the fleet as the dashboard logged it (weekly resets, epoch seconds)
OPS_R, ONC_R, TH_R, SUP_R = 1794031200, 1793833200, 1793844000, 1793847600
NEO_R, ANA_R, CLA_R, TC_R = 1793952000, 1793962800, 1793897999, 1793872799
# the preload rows state.json held: about 2-3 % weekly, under 1 % Fable
PRELOAD = [{"t": 1793400000.0 + i, "name": "x", "session": None, "weekly": w, "scoped": s}
           for i, (w, s) in enumerate([(3.0, 2.0), (3.0, 0.5), (2.0, 0.0), (0.0, 0.0), (0.0, 0.0)])]
LANE_WEEKLY_BURN = 13.9            # %/h, what the lane was burning before each swap
# (t, leaving, ops weekly %, the rest of the fleet, where pre-emption took the lane next)
HOPS = [
    (1793430704.57, "aws", 3, {"aws": (98, 1793764799), "oncall": (45, ONC_R),
                               "tero.heinonen": (77, TH_R, 0), "support": (46, SUP_R, 0)}, "oncall"),
    (1793447428.59, "oncall", 4, {"oncall": (98, ONC_R), "tero.heinonen": (77, TH_R, 0),
                                  "support": (46, SUP_R, 0)}, "tero.heinonen"),
    (1793454195.91, "tero.heinonen", 5, {"tero.heinonen": (98, TH_R, 80),
                                         "support": (46, SUP_R, 0)}, "support"),
]


def usage(mod, t, weekly, reset, session=None):
    u = mod.Usage()
    u.fetched_at = t
    u.weekly_pct = u.scoped_pct = float(weekly)
    u.weekly_reset = u.scoped_reset = reset
    u.session_pct = None if session is None else float(session)
    u.session_reset = None if session is None else t + 3 * 3600
    u.scoped_label = "Fable"
    u.status = "ok"
    return u


def fleet(mod, t, ops_weekly, extra):
    spec = {"ops": (ops_weekly, OPS_R), "neo": (44, NEO_R), "anakin": (47, ANA_R),
            "claude": (53, CLA_R), "tero@cv": (50, TC_R), "spent1": (98, 1793600000)}
    spec.update(extra)
    return {n: usage(mod, t, *v) for n, v in spec.items()}


def leaving_lane(mod, t, leaving):
    """The lane on `leaving` for the last half hour, burning LANE_WEEKLY_BURN."""
    st = T.lane_state(mod, leaving, t - 7200)
    st["preload"] = [dict(p) for p in PRELOAD]
    st["samples"] = {leaving: {
        k: [[t - 1800 + 60 * i, float(int(91 + r * i / 60))] for i in range(31)]
        for k, r in (("weekly", LANE_WEEKLY_BURN), ("scoped", 0.7 * LANE_WEEKLY_BURN))}}
    return st


class NovemberFirst(unittest.TestCase):
    def setUp(self):
        self.c = T.load()

    def choose(self, cfg, st, us, leaving, t, view=True):
        T.Clock(t).attach(self.c)
        return self.c.pick_target(us, cfg, exclude=leaving, t=t,
                                  preload=self.c.preload_estimate(st),
                                  view=self.c.landing_view(st, leaving, t) if view else None)

    def straight_to_second(self, hops):
        c = self.c
        cfg = T.cfg(c, no_signal=True)
        for t, leaving, ops_w, extra, second in hops:
            us = fleet(c, t, ops_w, extra)
            st = leaving_lane(c, t, leaving)
            # what happened: perish rate picked ops@, pre-emption then left it
            self.assertEqual(self.choose(cfg, st, us, leaving, t, view=False), "ops")
            landed = c.landing_view(st, leaving, t)
            after = t + cfg.grace + 1
            T.Clock(after).attach(c)
            hop = c.preempt_target(cfg, dict(landed, active="ops"), us, "ops", t=after)
            self.assertEqual(hop and hop[0], second, leaving)
            # now: the second-hop account directly, and pre-emption stays put there
            got = self.choose(cfg, st, us, leaving, t)
            self.assertEqual(got, second, leaving)
            T.Clock(after).attach(c)
            self.assertIsNone(c.preempt_target(cfg, dict(landed, active=got), us, got, t=after))

    def test_first_and_third_double_hop_now_go_straight_to_the_second_account(self):
        self.straight_to_second([HOPS[0], HOPS[2]])

    def test_second_double_hop_now_goes_straight_to_tero_heinonen(self):
        """tero.heinonen@ resets first but at 77 % has only ~1.7 h of runway
        at the lane's 13.9 %/h, under --preempt-runway.  Pre-emption gates on
        the account it leaves, so from support@ (~4 h) it would move there
        after the grace; the earliest reset is chosen straight away."""
        self.straight_to_second([HOPS[1]])

    def test_verified_target_and_landing_view_carry_the_lane_burn(self):
        c = self.c
        cfg = T.cfg(c, no_signal=True)
        t, leaving, ops_w, extra, second = HOPS[0]
        us = fleet(c, t, ops_w, extra)
        st = leaving_lane(c, t, leaving)
        T.Clock(t).attach(c)
        view = c.landing_view(st, leaving, t)
        self.assertAlmostEqual(view["lane_burn"]["weekly"], LANE_WEEKLY_BURN, delta=1.0)
        self.assertIsNone(view["active"])
        self.assertEqual(view["samples"], {})
        self.assertEqual(st["samples"][leaving]["weekly"][0][0], t - 1800)   # state untouched
        self.assertIsNone(st["lane_burn"])
        c.usage_for = lambda path, *a, **k: us[path]
        c.get_account = lambda cfg, name: type("A", (), {"cred_path": name})()
        self.assertEqual(c.verified_target(cfg, dict(us), leaving, c.preload_estimate(st), view),
                         second)

    def test_no_preempt_keeps_the_perish_rate_choice(self):
        cfg = T.cfg(self.c, no_signal=True, no_preempt=True)
        for t, leaving, ops_w, extra, _ in HOPS:
            us = fleet(self.c, t, ops_w, extra)
            st = leaving_lane(self.c, t, leaving)
            self.assertEqual(self.choose(cfg, st, us, leaving, t), "ops", leaving)

    def test_preload_over_the_preempt_cap_keeps_the_perish_rate_choice(self):
        cfg = T.cfg(self.c, no_signal=True, preempt_max_cost=1)
        t, leaving, ops_w, extra, _ = HOPS[0]
        st = leaving_lane(self.c, t, leaving)
        self.assertEqual(self.choose(cfg, st, fleet(self.c, t, ops_w, extra), leaving, t), "ops")


class TargetChoiceDifferential(unittest.TestCase):
    """Against the baseline's pick_target: with no view, or with pre-emption
    unable to act, the choice is identical; otherwise it is exactly the
    earliest-reset open candidate whose landed runway clears the gate."""

    def test_choice_differs_only_where_preemption_could_act(self):
        base = T.load_baseline()
        if base is None:
            self.skipTest("no baseline (git history or CCROLL_BASELINE)")
        new = T.load()
        rng = random.Random(20261101)
        n = int(os.environ.get("CCROLL_DIFF_N", "4000")) // 2
        branch = differs = 0
        for i in range(n):
            t = 1_800_000_000.0 + rng.uniform(0, 1e6)
            T.Clock(t).attach(base, new)
            args = dict(no_signal=True, no_preempt=rng.random() < 0.2,
                        preempt_max_cost=rng.choice([1, 5, 5, 5]),
                        preempt_runway=rng.choice([1, 3, 3, 6]),
                        by=rng.choice(["weekly", "scoped"]))
            cfg_b, cfg_n = T.cfg(base, **args), T.cfg(new, **args)
            fleet_spec = []
            for k in range(rng.randint(1, 8)):
                r = rng.random()
                reset = None if r < 0.15 else t - 60 if r < 0.2 else t + rng.uniform(600, 7 * 86400)
                fleet_spec.append((f"a{k}", rng.uniform(0, 100), reset,
                                   rng.choice([None, rng.uniform(0, 100)])))
            leaving = rng.choice(["a0", "gone"])
            rate = rng.choice([0.5, 3, 10, 14, 30, 60])
            lane_burn = rng.choice([None, {"weekly": rng.uniform(0, 40), "scoped": rng.uniform(0, 40),
                                           "session": rng.uniform(0, 80)}])
            preload = [{"t": t - 1000, "name": "x", "session": rng.choice([None, 5.0]),
                        "weekly": rng.uniform(0, 6), "scoped": rng.uniform(0, 6)}
                       for _ in range(rng.randint(0, 2))]

            def build(mod):
                us = {}
                for name, w, reset, s in fleet_spec:
                    u = usage(mod, t, int(w), reset, None if s is None else int(s))
                    us[name] = u
                st = T.lane_state(mod, leaving, t - 7200)
                st["lane_burn"] = None if lane_burn is None else dict(lane_burn)
                st["preload"] = [dict(p) for p in preload]
                st["samples"] = {leaving: {k: [[t - 1800 + 60 * j, float(int(50 + rate * j / 60))]
                                               for j in range(31)] for k in ("weekly", "scoped")}}
                return us, st

            us_b, st_b = build(base)
            us_n, st_n = build(new)
            pre_b, pre_n = base.preload_estimate(st_b), new.preload_estimate(st_n)
            old = base.pick_target(us_b, cfg_b, exclude=leaving, t=t, preload=pre_b)
            self.assertEqual(new.pick_target(us_n, cfg_n, exclude=leaving, t=t, preload=pre_n),
                             old, f"no view, scenario {i}")
            view = new.landing_view(st_n, leaving, t)
            got = new.pick_target(us_n, cfg_n, exclude=leaving, t=t, preload=pre_n, view=view)
            # when could pre-emption act on the lane once landed?
            strict, _ = new.candidates(us_n, cfg_n, leaving, pre_n, t)
            cost = new.preload_cost(pre_n, cfg_n)
            opened, ready = [], False
            if cfg_n.preempt and strict and (cost is None or cost <= cfg_n.preempt_max_cost):
                for name, u in strict:
                    pct, reset = new.governing_window(u, cfg_n)
                    if new.perish_rate(pct, reset, t) is None:
                        continue
                    opened.append((reset, name))
                    run = new.runway_s(dict(view, active=name), name, u, cfg_n)
                    ready = ready or (run is not None and run > cfg_n.preempt_runway)
            # any open candidate with runway lets pre-emption act; from there it
            # would move to the earliest reset, so that is where the lane rests
            if ready:
                branch += 1
                self.assertEqual(got, min(opened)[1], f"scenario {i}")
                differs += got != old
            else:
                self.assertEqual(got, old, f"scenario {i}: pre-emption could not act")
        # both kinds of case must actually occur
        self.assertGreater(branch, n // 20)
        self.assertGreater(n - branch, n // 20)
        self.assertGreater(differs, 0)


class FleetSmoke(unittest.TestCase):
    """A few days of a small fleet, both orderings on the same world: the
    old one double-hops on these seeds, the new one never does after the
    first day, and the lanes are not blocked any longer for it."""
    SEEDS = (6, 11, 18)

    def test_no_double_hops_and_no_more_blocking(self):
        start = time.time()
        old = new = None
        tot = {False: [0, 0.0, 0.0], True: [0, 0.0, 0.0]}     # hops, blocked h, preload %
        for seed in self.SEEDS:
            for use_view in (False, True):
                m = fleetsim.Sim(T.load(), seed, n_acc=6, lanes=2, days=4, warm_days=1,
                                 rho=0.9, side_per_day=0.3, use_view=use_view).run()
                tot[use_view][0] += m["hops"]
                tot[use_view][1] += m["blocked_h"]
                tot[use_view][2] += m["preload"]
                if use_view:
                    self.assertEqual(m["hops"], 0, f"seed {seed}: {m}")
        old, new = tot[False], tot[True]
        self.assertGreater(old[0], 0, "the old ordering should double-hop on these seeds")
        self.assertLessEqual(new[1], old[1] + 1e-9, (old, new))
        self.assertLess(new[2], old[2], (old, new))
        self.assertLess(time.time() - start, 60)


if __name__ == "__main__":
    unittest.main()
