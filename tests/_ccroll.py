"""Shared test plumbing: a fresh copy of ccroll.py per test (its module
state — holds, the pacer — must not leak between tests), a clock the test
owns, and a signal feed that is just a list."""
import importlib.util
import itertools
import os
import subprocess
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CCROLL = os.path.join(REPO, "ccroll.py")
# The commit the A/B/D/E changes were made on: the reference the
# differential and replay tests compare the new rules against.
BASELINE_COMMIT = "4014927"
_seq = itertools.count()


def load(path: str = CCROLL, name: str | None = None) -> types.ModuleType:
    name = name or f"ccroll_t{next(_seq)}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod             # dataclass-free, but keep pickling/inspect sane
    spec.loader.exec_module(mod)
    return mod


_baseline_path = None


def load_baseline() -> types.ModuleType | None:
    """ccroll.py as it was before these changes, from git (or the file
    CCROLL_BASELINE names); None when neither is available."""
    global _baseline_path
    if _baseline_path is None and os.environ.get("CCROLL_BASELINE"):
        _baseline_path = os.environ["CCROLL_BASELINE"]      # a saved copy instead of git
    if _baseline_path is None:
        try:
            src = subprocess.run(["git", "-C", REPO, "show", f"{BASELINE_COMMIT}:ccroll.py"],
                                 check=True, capture_output=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return None
        fd, _baseline_path = tempfile.mkstemp(prefix="ccroll_baseline_", suffix=".py")
        with os.fdopen(fd, "wb") as fh:
            fh.write(src)
    return load(_baseline_path)


class Clock:
    """Stands in for ccroll.now() in every module it is attached to."""

    def __init__(self, t: float = 1_800_000_000.0):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def attach(self, *mods) -> "Clock":
        for m in mods:
            m.now = self
        return self


def capture_signals(mod) -> list:
    """Every switch_expected the module sends, as (sent_at, eta, usage_pct)."""
    sent = []
    mod.signal_switch_expected = lambda cfg, state, eta, pct: sent.append((mod.now(), eta, pct))
    return sent


def cfg(mod, **kw):
    args = types.SimpleNamespace(**kw)
    tmp = tempfile.mkdtemp(prefix="ccroll_test_")
    args.root = getattr(args, "root", None) or os.path.join(tmp, "accounts")
    args.claude_dir = getattr(args, "claude_dir", None) or os.path.join(tmp, "claude")
    return mod.Cfg(args)


def lane_state(mod, name: str, swapped: float, grace: float = 300.0) -> dict:
    """A lane on `name` since `swapped`, its grace over at swapped + grace."""
    return {"active": name, "samples": {}, "last_swap": swapped, "active_since": swapped,
            "grace_until": swapped + grace, "preload": [], "lane_burn": None,
            "signal_expect_eta": None, "signal_crossed": []}


def reading(mod, t: float, weekly=None, scoped=None, session=None,
            weekly_reset=None, scoped_reset=None, session_reset=None, error=None):
    u = mod.Usage()
    u.fetched_at = t
    u.weekly_pct, u.scoped_pct, u.session_pct = weekly, scoped, session
    u.weekly_reset = weekly_reset if weekly_reset is not None else (t + 4 * 86400 if weekly is not None else None)
    u.scoped_reset = scoped_reset if scoped_reset is not None else (t + 4 * 86400 if scoped is not None else None)
    u.session_reset = session_reset if session_reset is not None else (t + 3 * 3600 if session is not None else None)
    u.status = "ok"
    u.error = error
    return u


def refused(mod, t: float, msg="rate_limit_error · Rate limited"):
    u = mod.Usage()
    u.fetched_at = t
    u.error = msg
    return u


def bare_master(mod, cfg, active: str):
    """A Master with just what its per-tick rotation path touches — no
    sockets, no keyboard, no threads.  start_rotation and maybe_preempt are
    recorded instead of run."""
    m = object.__new__(mod.Master)
    m.cfg, m.a = cfg, None
    m.state = mod.load_state(cfg)
    m.state["active"] = active
    m.usages, m.last_good, m.seed_view = {}, {}, {}
    m.paused = False
    m.local = mod.LaneRt(mod.Lane(m.state, m.state, "host"), cfg, local=True)
    m.remote, m.tasks, m.claims, m.timers = {}, {}, {}, []
    m.reassert = None
    m.started = []
    m.start_rotation = lambda lr, reason, auto, first=False: m.started.append((mod.now(), reason))
    m.maybe_preempt = lambda lr, active: None
    return m


def feed_events(cfg) -> list:
    import json
    path = os.path.join(cfg.signal_dir, "events.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def baseline_with_scoped_rotation() -> types.ModuleType | None:
    """The baseline with the one rule this tree adds laid over its own
    primitives: the per-model (scoped) weekly window rotates, blocks and
    counts in EITHER --by mode, where the baseline consulted it under
    --by scoped only.  The differential tests judge against this, so every
    other decision must still match the baseline exactly.  `load_baseline`
    stays pristine for the replay, which reproduces the record as it was."""
    import copy
    base = load_baseline()
    if base is None:
        return None

    def as_scoped(cfg):
        c = copy.copy(cfg)
        c.mode = "scoped"
        return c

    def is_exhausted(u, cfg, t=None):
        # session first, then the governing weekly window, then the other —
        # the order the new rule judges in
        if u is None or u.error:
            return None
        if (u.status or "").lower() in ("rejected", "exceeded", "blocked"):
            return f"status {u.status}"
        weekly = (("weekly", "weekly", u.weekly_pct, u.weekly_reset, cfg.weekly_threshold),
                  ("scoped", (u.scoped_label or "scoped").lower() + " weekly",
                   u.scoped_pct, u.scoped_reset, cfg.scoped_threshold))
        windows = (("session", "session", u.session_pct, u.session_reset, cfg.threshold),) + (
            weekly if cfg.mode == "weekly" else weekly[::-1])
        for key, label, pct, reset, thr in windows:
            if base.window_spent(pct, reset, thr, t):
                return f"{label} " + base.pct_text(u, key, pct)
        return None

    blocked, recovery, utilisation = base.blocked_until, base.recovery_moment, base.rotation_utilisation
    limit, threshold, comfortable = base.limit_etas, base.threshold_etas, base.comfortable
    base.is_exhausted = is_exhausted
    base.blocked_until = lambda u, cfg, t=None: blocked(u, as_scoped(cfg), t)
    base.recovery_moment = lambda u, cfg, t: recovery(u, as_scoped(cfg), t)
    base.rotation_utilisation = lambda u, cfg: utilisation(u, as_scoped(cfg))
    base.limit_etas = lambda state, name, u, cfg: limit(state, name, u, as_scoped(cfg))
    base.threshold_etas = lambda state, name, u, cfg: threshold(state, name, u, as_scoped(cfg))
    base.comfortable = lambda u, cfg, preload=None, t=None: (
        comfortable(u, cfg, preload, t)
        and base.effective_pct(u.scoped_pct, u.scoped_reset, t) < 90)
    return base
