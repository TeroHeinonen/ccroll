"""A small fleet simulator that drives ccroll's real rotation rules.

Only the world is simulated: the clock (`now`), the usage endpoint
(`usage_for`) and the accounts.  Everything that decides — judge_rotation,
verified_target / pick_target, candidates, comfortable, preempt_target,
commit_swap, record_samples, measure_preload, preload_estimate,
landing_view — is ccroll's own code, loaded fresh per run.

World (one tick = the 60 s poll interval): accounts with a weekly and a
Fable scoped window and no 5-hour session window; a window opens on first
use and resets a week later; the endpoint reports whole percents.  Lanes
share the pool, each burning at a share of `rho` × the fleet's capacity
with a day/night cycle and hourly noise.  Every swap costs a preload of
U(1,4) % landing over the two polls after it.  The master polls each lane's
live account every tick and scans the rest every 600 s; a lane's pool
excludes accounts other lanes are on.  Rotation per lane mirrors
Master.maybe_rotate / maybe_preempt / on_preempt_read.

`use_view=False` runs the same code without the landing view, which is the
old ordering (perish rate only).  This is a cut-down copy of the fuller
simulator the rule was designed with (23 accounts, 1-3 lanes, four weeks,
24 seeds); it is sized to run in seconds as a smoke test.
"""
import argparse
import math
import random
import types

WEEK = 7 * 86400
DT = 60.0
SCAN_S = 600.0
HOP_S = 3600.0          # a pre-emptive swap this soon after the last one is a double hop


class Acct:
    __slots__ = ("name", "pct", "fable", "reset", "pending", "side")

    def __init__(self, name):
        self.name, self.pct, self.fable, self.reset, self.pending = name, 0.0, 0.0, None, []
        self.side = []


class Sim:
    def __init__(self, c, seed, n_acc=10, lanes=2, days=10.0, warm_days=3.0, rho=None,
                 side_per_day=0.0, use_view=True):
        self.c, self.use_view = c, use_view
        rng = self.rng = random.Random(seed)
        # separate streams so both orderings see the same demand, side use and
        # preload draws (common random numbers)
        self.wrng = random.Random(seed * 7919 + 1)
        self.prng = random.Random(seed * 104729 + 2)
        self.side_per_day = side_per_day
        self.t0 = self.t = 1_800_000_000.0 + rng.uniform(0, 86400)
        self.end = self.t0 + days * 86400
        self.warm = self.t0 + warm_days * 86400
        c.now = lambda: self.t
        c.usage_for = lambda path, *a, **k: self.read(path)
        c.get_account = lambda cfg, name: types.SimpleNamespace(cred_path=name, name=name)
        c.add_event = lambda state, msg: None
        c.save_state = lambda *a, **k: None
        for fn in ("signal_switch_done", "signal_switch_expected", "signal_rotation_start"):
            if hasattr(c, fn):
                setattr(c, fn, lambda *a, **k: None)
        self.cfg = c.Cfg(argparse.Namespace(by="weekly", no_signal=True))
        self.cfg.signal = False
        self.accts = {}
        for i in range(n_acc):
            a = Acct(f"acct{i:02d}")
            r = rng.random()
            if r >= 0.1:
                a.reset = self.t + rng.uniform(0.02, 1) * WEEK
                a.pct = rng.uniform(0, 6) if r < 0.25 else rng.uniform(20, 98)
                a.fable = 0.9 * a.pct
            self.accts[a.name] = a
        self.rho = rng.uniform(0.5, 1.0) if rho is None else rho
        cap_rate = n_acc * 100.0 / 168.0
        self.root = {"samples": {}, "events": []}
        self.lanes = []
        for k in range(lanes):
            ls = c.Lane(self.root, c.new_lane_data(), f"host{k}")
            self.lanes.append({"ls": ls, "share": rng.uniform(0.6, 1.4), "noise": 1.0,
                               "phase": rng.uniform(-2, 2), "blocked_s": 0.0})
        tot = sum(lane["share"] for lane in self.lanes)
        for lane in self.lanes:
            lane["mean"] = self.rho * cap_rate * lane["share"] / tot
        self.usages = {n: self.read(n) for n in self.accts}
        self.swaps = []          # (t, lane, preemptive)
        self.preload_spent = 0.0
        self.next_scan = self.t + SCAN_S
        self.tick = 0
        for lane in self.lanes:
            pool = self.pool(lane)
            tgt = c.pick_target(pool, self.cfg, exclude=None)
            if tgt:
                self.swap(lane, tgt, "first", pool[tgt], False, count=False)

    # --- world ---------------------------------------------------------------
    def read(self, name):
        a = self.accts[name]
        u = self.c.Usage()
        u.weekly_pct = float(math.floor(min(100.0, a.pct)))
        u.scoped_pct = float(math.floor(min(100.0, a.fable)))
        u.weekly_reset = u.scoped_reset = (math.ceil(a.reset / 60) * 60 if a.reset else None)
        u.scoped_label = "Fable"
        u.status = "ok"
        return u

    def rate(self, lane):
        hour = ((self.t - self.t0) / 3600.0 + lane["phase"]) % 24
        day = math.exp(-((hour - 14) / 5.0) ** 2)
        return lane["mean"] * (0.35 + 1.75 * day) * lane["noise"]

    def spend(self, a, amt):
        if a.reset is None and amt > 0:
            a.reset = self.t + WEEK
        use = max(0.0, min(amt, 100.0 - a.pct))
        a.pct += use
        a.fable = min(100.0, a.fable + 0.9 * amt)
        return use

    def step_world(self):
        measuring = self.t >= self.warm
        for a in self.accts.values():
            if a.reset is not None and a.reset <= self.t:
                a.pct = a.fable = 0.0
                a.reset = None
            if a.pending:
                spent = self.spend(a, a.pending.pop(0))
                if measuring:
                    self.preload_spent += spent
        self.tick += 1
        if self.tick % 60 == 0:
            for lane in self.lanes:
                lane["noise"] = math.exp(self.wrng.gauss(-0.03, 0.25))
        if self.side_per_day:
            p = self.side_per_day * DT / 86400.0
            for a in self.accts.values():
                if self.wrng.random() < p:
                    amt, n = self.wrng.uniform(1, 15), self.wrng.randint(20, 90)
                    a.side += [amt / n] * n
        for a in self.accts.values():
            if a.side:
                self.spend(a, a.side.pop())
        for lane in self.lanes:
            d = self.rate(lane) * DT / 3600.0
            a = self.accts[lane["ls"].get("active")]
            got = self.spend(a, d) if a.pct < 100.0 else 0.0
            if measuring and got < d * 0.999:
                lane["blocked_s"] += DT * (1 - got / d)

    # --- ccroll ----------------------------------------------------------------
    def held(self, lane):
        return {other["ls"].get("active") for other in self.lanes if other is not lane}

    def pool(self, lane):
        h = self.held(lane)
        return {n: u for n, u in self.usages.items() if n not in h}

    def swap(self, lane, target, reason, snap, preemptive, count=True):
        ls = lane["ls"]
        self.c.commit_swap(self.cfg, ls, target, reason, snap, preemptive=preemptive)
        p = self.prng.uniform(1.0, 4.0)
        self.accts[target].pending += [p / 2, p / 2]
        if count and self.t >= self.warm:
            self.swaps.append((self.t, self.lanes.index(lane), preemptive))

    def judge(self, ls, active):
        c, cfg, u = self.c, self.cfg, self.usages[active]
        if hasattr(c, "judge_rotation"):
            return c.judge_rotation(ls, active, u, {active: u}, cfg, polling=True)[1]
        u = c.projected_usage(ls, active, u)
        return c.is_exhausted(u, cfg) or c.about_to_exhaust(ls, active, u, cfg)

    def step_ccroll(self):
        c, cfg = self.c, self.cfg
        if self.t >= self.next_scan:
            self.next_scan = self.t + SCAN_S
            for n in self.accts:
                self.usages[n] = self.read(n)
        order = list(self.lanes)
        self.rng.shuffle(order)
        for lane in order:
            ls = lane["ls"]
            active = ls.get("active")
            self.usages[active] = self.read(active)
            c.record_samples(self.root, {active: self.usages[active]})
            c.measure_preload(ls, self.usages, cfg)
        for lane in order:
            ls = lane["ls"]
            active = ls.get("active")
            pool = self.pool(lane)
            if self.judge(ls, active):
                view = c.landing_view(ls, active) if self.use_view else None
                tgt = c.verified_target(cfg, pool, active, c.preload_estimate(ls), view)
                self.usages.update(pool)
                if tgt and tgt not in self.held(lane):
                    self.swap(lane, tgt, "spent", pool.get(tgt), False)
                continue
            if not cfg.preempt:
                continue
            choice = c.preempt_target(cfg, ls, pool, active)
            if not choice:
                continue
            target = choice[0]
            fresh = self.read(target)
            self.usages[target] = fresh
            if c.is_exhausted(fresh, cfg) or not c.comfortable(fresh, cfg, c.preload_estimate(ls)):
                continue
            if c.preempt_target(cfg, ls, dict(pool, **{target: fresh}), active) != choice:
                continue
            self.swap(lane, target, choice[1], fresh, True)

    def run(self):
        while self.t < self.end:
            self.step_world()
            self.step_ccroll()
            self.t += DT
        return self.metrics()

    def metrics(self):
        days = (self.end - self.warm) / 86400.0
        hops, last = 0, {}
        for t, k, pre in self.swaps:
            if pre and k in last and t - last[k] < HOP_S:
                hops += 1
            last[k] = t
        return {"swaps": len(self.swaps), "hops": hops,
                "preload": self.preload_spent,
                "blocked_h": sum(lane["blocked_s"] for lane in self.lanes) / 3600.0,
                "days": days}
