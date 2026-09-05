#!/usr/bin/env python3
"""ccroll — hands your long-running Claude Code session from one Claude
subscription account to the next, without breaking stride.

Claude Code (>= 2.x) watches its credentials file (`.credentials.json` in the
config dir) by mtime and reloads it live when it changes on disk — it has to,
because concurrent sessions share the file and rotate refresh tokens through
it.  ccroll exploits that deliberately: to switch accounts it atomically
replaces the file with another account's credentials.  The running interactive
session — TUI, context, subagents, everything — never notices anything beyond
"credentials refreshed".  No restart, no /login, no browser automation.

Each account lives in its own Claude config dir under ~/.claude-accounts/,
named by the account's email address — enforced, not chosen: `ccroll add` logs
you in first and then reads the email from the account itself, so the name on
screen is always the identity that is actually live.  `ccroll watch` runs a
live dashboard in its own terminal:
it polls every account's usage through the free OAuth usage endpoint,
highlights the active account, estimates burn rates and time-to-limit from a
rolling time series, and hot-swaps to the account whose weekly headroom would
otherwise expire soonest when the active one approaches a limit.

Stdlib only.  Linux (and any platform where Claude Code keeps credentials in
a plain file rather than a keychain).

Usage:
    ccroll add                   # interactive login(s); each account is named by its email
    ccroll adopt                 # register the current ~/.claude login under its email
    ccroll                       # dashboard + auto-rotation (same as `watch`)
    ccroll status                # one-shot table
    ccroll switch ops@x.com      # manual hot-swap now (a unique prefix works: `switch ops`)
    ccroll list                  # accounts and token expiries
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import copy
import json
import os
import re
import select
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

CCROLL_VERSION = "0.1.0"

# --- Anthropic OAuth constants --------------------------------------------------
# All verified against the Claude Code 2.1.259 binary (strings in the bundle);
# these are the same endpoints and public client id the CLI itself uses.
API_BASE = "https://api.anthropic.com"
USAGE_PATH = "/api/oauth/usage"
PROFILE_PATH = "/api/oauth/profile"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_BETA = "oauth-2025-04-20"
ANTHROPIC_VERSION = "2023-06-01"

CRED_FILE = ".credentials.json"
CONFIG_FILE = ".claude.json"     # Claude Code's global config (identity + history)
MIN_CONFIG_KEYS = 3              # fewer than this and we assume a truncated read
HTTP_TIMEOUT_S = 30
# The token endpoint sits behind an edge rule that only serves the official
# client signature: a default urllib UA gets a Cloudflare 403 (error 1010) and
# a generic UA gets throttled. ccroll performs the same OAuth refresh the CLI
# performs, on the user's own stored credentials, so it identifies the same way.
USER_AGENT = "claude-cli/2.1.259 (external, cli)"
REFRESH_RETRIES = 3             # attempts per refresh when the server throttles
REFRESH_BACKOFF_S = 2.0         # first backoff; doubles per attempt
REFRESH_STAGGER_S = 0.35        # spacing between refreshes of different accounts
MAX_PARALLEL = 8
MAX_TARGET_TRIES = 4            # fresh-read confirmations before giving up on a swap
RESCAN_MIN_S = 30               # floor on any scheduled rescan, so a stale reset time
                                # can never turn the watch loop into a scan storm
LAST_GOOD_MAX_AGE_S = 15 * 60   # how long a cached usage read stands in for a failed one
USAGE_ERROR_COOLDOWN_S = 60     # back off this long on an account the endpoint throttles
PREEMPT_MIN_GAP_S = 15 * 60     # never preempt within this long of any swap
FAST_POLL_BELOW_S = 10 * 60     # poll the active account faster once a limit is this close
FAST_POLL_S = 15
REFRESH_MARGIN_S = 180          # refresh an access token this close to expiry
SWAP_VERIFY_DELAY_S = 2.0       # re-check the live file this long after a swap
SAMPLE_RETENTION_S = 24 * 3600  # keep at most a day of burn-rate samples
BURN_WINDOW_S = 45 * 60         # fit burn rate over the last 45 minutes
BURN_MIN_SAMPLES = 3
BURN_MIN_SPAN_S = 90            # 3 polls at the 60s default: a figure after ~2 min
BURN_SETTLED_SPAN_S = 8 * 60    # shorter fits are shown dimmed as provisional
BURN_MIN_RATE = 0.05            # %/h below this shows as idle
PRELOAD_KEEP = 5                # preload measurements kept for the median
PRELOAD_MEASURE_MAX_S = 300     # after the grace: measure raw if no fit appears within this
EARLY_MIN_PCT = 50              # burn-based early rotation only from here up: below it an
                                # ETA under the lead would need >1000%/h, which is noise
                                # (the post-swap re-prime spike), not a sustained rate
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")  # account emails
LIVE_PSEUDO = "(live login)"  # display row for a live login not in the store


class CcrollError(Exception):
    pass


# --- small utilities ------------------------------------------------------------
def now() -> float:
    return time.time()


def write_json_atomic(path: str, blob: dict) -> None:
    """Write JSON with 0600 perms via rename, so readers never see a torn file
    and the mtime bump is a single atomic event (what Claude Code watches)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ccroll-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(blob, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _dur_parts(seconds: float | None) -> tuple[int, int, int] | None:
    if seconds is None or seconds <= 0:
        return None
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    mins = s // 60
    if days == 0 and hours == 0 and mins == 0:
        mins = 1  # under a minute still reads as "some time left", not zero
    return days, hours, mins


def fmt_dur(seconds: float | None) -> str:
    """Fixed-width plain '0d 00h 00m' ('—' when past or absent)."""
    p = _dur_parts(seconds)
    if p is None:
        return "—"
    d, h, m = p
    return f"{d}d {h:02d}h {m:02d}m"


def fmt_dur3(a: "Ansi", seconds: float | None) -> tuple[str, int]:
    """Colored fixed-width duration: days, hours and minutes each in their own
    color, leading zero units dimmed, so remaining time reads in one glimpse.
    Returns (colored_text, visible_len)."""
    p = _dur_parts(seconds)
    if p is None:
        return a.dim("—"), 1
    d, h, m = p
    dd = a.cyan(f"{d}d") if d else a.dim(f"{d}d")
    hh = a.yellow(f"{h:02d}h") if (d or h) else a.dim(f"{h:02d}h")
    mm = a.green(f"{m:02d}m")
    return f"{dd} {hh} {mm}", len(fmt_dur(seconds))


def fmt_clock(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


# --- ANSI -----------------------------------------------------------------------
class Ansi:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _w(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.enabled else s

    def dim(self, s):    return self._w("2", s)
    def bold(self, s):   return self._w("1", s)
    def green(self, s):  return self._w("32", s)
    def yellow(self, s): return self._w("33", s)
    def red(self, s):    return self._w("31;1", s)
    def orange(self, s): return self._w("38;5;208", s)
    def cyan(self, s):   return self._w("36", s)
    def inverse(self, s): return self._w("7", s)


def sev_color(a: Ansi, pct: float | None):
    if pct is None:
        return a.dim
    if pct >= 95:
        return a.red
    if pct >= 80:
        return a.orange
    if pct >= 50:
        return a.yellow
    return a.green


# --- HTTP -----------------------------------------------------------------------
def _http(method: str, url: str, token: str | None, body: dict | None):
    """Return (status:int|None, body_bytes:bytes, neterr:str|None)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("anthropic-beta", OAUTH_BETA)
        req.add_header("anthropic-version", ANTHROPIC_VERSION)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status, resp.read(), None
    except urllib.error.HTTPError as e:
        return e.code, e.read(), None
    except Exception as e:
        return None, b"", str(getattr(e, "reason", e))


def http_detail(status: int | None, raw: bytes, j: dict | None = None) -> str:
    """Readable reason for a failed call.  Falls back to the response body so a
    non-JSON edge rejection (a Cloudflare block, say) is legible instead of a
    bare status code."""
    if j:
        msg = j.get("error_description") or j.get("error")
        if isinstance(msg, dict):
            msg = msg.get("message") or msg.get("type")
        if msg:
            return f"{msg} (http {status})"
    text = " ".join((raw or b"").decode(errors="replace").split())[:80]
    if text:
        return f"http {status}: {text}"
    return f"http {status}"


# --- credential store -----------------------------------------------------------
class Account:
    def __init__(self, name: str, directory: str):
        self.name = name
        self.dir = directory
        self.cred_path = os.path.join(directory, CRED_FILE)

    def read(self) -> dict | None:
        return read_json(self.cred_path)


class Cfg:
    def __init__(self, args):
        self.root = os.path.expanduser(getattr(args, "root", None) or "~/.claude-accounts")
        explicit_dir = getattr(args, "claude_dir", None) or os.environ.get("CLAUDE_CONFIG_DIR")
        live_dir = explicit_dir or os.path.join(os.path.expanduser("~"), ".claude")
        self.live_dir = os.path.expanduser(live_dir)
        self.live_path = os.path.join(self.live_dir, CRED_FILE)
        # The global config sits INSIDE an explicit config dir, but at
        # ~/.claude.json when none is set — it is *not* ~/.claude/.claude.json.
        self.live_config_path = os.path.expanduser(
            os.path.join(live_dir, CONFIG_FILE) if explicit_dir
            else os.path.join(os.path.expanduser("~"), CONFIG_FILE))
        # opt-in: also point the display identity at the account we swap to.
        self.sync_identity = bool(getattr(args, "sync_identity", False))
        self.state_path = os.path.join(self.root, ".ccroll", "state.json")
        self.threshold = float(getattr(args, "threshold", 95))
        self.scoped_threshold = float(getattr(args, "scoped_threshold", 97))
        self.interval = max(15, int(getattr(args, "interval", 60)))
        self.scan = max(self.interval, int(getattr(args, "scan", 300)))
        self.cooldown = int(getattr(args, "cooldown", 0))
        self.rotate = not getattr(args, "no_rotate", False)
        # burn-based early rotation: the active account counts as spent once
        # its predicted time to a limit drops under this (the static thresholds
        # stay as the latest point).  Covers the poll interval, usage-endpoint
        # lag, the swap itself and requests already in flight.
        lead = getattr(args, "lead", None)
        self.lead = float(lead) if lead is not None else float(max(60, self.interval))
        # pre-emptive rotation: while the active account still has headroom,
        # move to the account whose governing weekly window resets soonest
        # (and, with --touch, open freshly reset windows at once).  Gated on
        # the active account's runway so it never fires when sessions bind.
        self.preempt = not getattr(args, "no_preempt", False)
        self.preempt_runway = float(getattr(args, "preempt_runway", 3.0)) * 3600
        self.touch = bool(getattr(args, "touch", False))
        # post-swap grace: every running agent re-primes its context on the
        # new account in the first minutes after a swap (the *preload*).  For
        # this long burn-based rotation and pre-emption are off and the burn
        # series is ignored; the static thresholds still apply.  The preload
        # is measured at the end of the grace and used to (a) skip candidates
        # it alone would exhaust and (b) refuse pre-emption when a swap costs
        # more than --preempt-max-cost percent of the governing window.
        self.grace = float(getattr(args, "grace", 300))
        self.preempt_max_cost = float(getattr(args, "preempt_max_cost", 5.0))
        # which weekly limit governs exhaustion + next-account choice:
        # "scoped" = the per-model weekly limit (Fable on current Max plans),
        # "weekly" = the all-models weekly limit.
        self.mode = getattr(args, "by", None) or "scoped"


def list_accounts(cfg: Cfg) -> list[Account]:
    accounts = []
    if os.path.isdir(cfg.root):
        for name in sorted(os.listdir(cfg.root)):
            d = os.path.join(cfg.root, name)
            if name.startswith(".") or not os.path.isdir(d):
                continue
            if os.path.isfile(os.path.join(d, CRED_FILE)):
                accounts.append(Account(name, d))
    return accounts


def get_account(cfg: Cfg, name: str) -> Account:
    """Exact match first; otherwise a unique prefix of the email is enough."""
    accounts = list_accounts(cfg)
    for acc in accounts:
        if acc.name == name:
            return acc
    matches = [acc for acc in accounts if acc.name.startswith(name)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise CcrollError(f"{name!r} is ambiguous: " + ", ".join(m.name for m in matches))
    raise CcrollError(f"no such account: {name!r} (run `ccroll list`)")


def oauth_of(creds: dict | None) -> dict | None:
    if not isinstance(creds, dict):
        return None
    oauth = creds.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) and oauth.get("accessToken") else None


def expires_in_s(oauth: dict) -> float | None:
    ms = oauth.get("expiresAt")
    return (ms / 1000.0 - now()) if isinstance(ms, (int, float)) else None


# --- OAuth refresh --------------------------------------------------------------
def oauth_refresh(oauth: dict) -> dict:
    """Exchange the refresh token for a fresh access token (rotating the
    refresh token when the server does).  Returns a new claudeAiOauth dict."""
    refresh = oauth.get("refreshToken")
    if not refresh:
        raise CcrollError("no refresh token stored — re-login with `ccroll add`")
    body = {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": CLIENT_ID}
    delay = REFRESH_BACKOFF_S
    for attempt in range(REFRESH_RETRIES):
        status, data, neterr = _http("POST", TOKEN_URL, None, body)
        if neterr:
            raise CcrollError(f"token refresh network error: {neterr}")
        try:
            j = json.loads(data.decode() or "{}")
        except json.JSONDecodeError:
            j = {}
        if status == 200 and j.get("access_token"):
            break
        # throttling is transient: back off and try again before giving up
        if status == 429 and attempt < REFRESH_RETRIES - 1:
            time.sleep(delay)
            delay *= 2
            continue
        raise CcrollError(f"token refresh failed ({http_detail(status, data, j)})")
    new = dict(oauth)
    new["accessToken"] = j["access_token"]
    if j.get("refresh_token"):
        new["refreshToken"] = j["refresh_token"]
    if isinstance(j.get("expires_in"), (int, float)):
        new["expiresAt"] = int((now() + j["expires_in"]) * 1000)
    if isinstance(j.get("refresh_token_expires_in"), (int, float)):
        new["refreshTokenExpiresAt"] = int((now() + j["refresh_token_expires_in"]) * 1000)
    return new


def fresh_token(cred_path: str, persist: bool = True) -> str:
    """Return a currently-valid access token for the credentials at cred_path,
    refreshing (and persisting the rotated refresh token) when needed."""
    creds = read_json(cred_path)
    oauth = oauth_of(creds)
    if not oauth:
        raise CcrollError(f"no credentials at {cred_path}")
    left = expires_in_s(oauth)
    if left is not None and left > REFRESH_MARGIN_S:
        return oauth["accessToken"]
    new = oauth_refresh(oauth)
    if persist:
        creds["claudeAiOauth"] = new
        write_json_atomic(cred_path, creds)
    return new["accessToken"]


# --- usage + profile ------------------------------------------------------------
def _iso_to_epoch(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


class Usage:
    """One snapshot of an account's rate-limit windows."""
    def __init__(self):
        self.stale_note = None      # set on a cached snapshot shown for a failed read
        self.session_pct = None
        self.session_reset = None
        self.weekly_pct = None
        self.weekly_reset = None
        self.scoped_pct = None      # the per-model weekly limit (Fable on Max plans)
        self.scoped_reset = None
        self.scoped_label = None
        self.status = None
        self.error = None
        self.fetched_at = now()

    def windows(self):
        return {"session": self.session_pct, "weekly": self.weekly_pct, "scoped": self.scoped_pct}


def fetch_usage(token: str) -> Usage:
    u = Usage()
    status, data, neterr = _http("GET", API_BASE + USAGE_PATH, token, None)
    if neterr:
        u.error = f"network: {neterr}"
        return u
    try:
        j = json.loads(data.decode() or "{}")
    except json.JSONDecodeError:
        j = {}
    if not isinstance(j, dict) or j.get("type") == "error" or isinstance(j.get("error"), dict):
        etype = (j.get("error") or {}).get("type") if isinstance(j.get("error"), dict) else None
        if status == 401 or etype == "authentication_error":
            u.error = "auth"
        elif etype == "permission_error":
            u.error = "token lacks user:profile scope — re-login interactively"
        else:
            u.error = etype or f"http {status}"
        return u
    for lim in j.get("limits") or []:
        kind, pct, reset = lim.get("kind"), lim.get("percent"), _iso_to_epoch(lim.get("resets_at"))
        if kind == "session":
            u.session_pct, u.session_reset = pct, reset
        elif kind == "weekly_all":
            u.weekly_pct, u.weekly_reset = pct, reset
        elif kind == "weekly_scoped":
            label = ((lim.get("scope") or {}).get("model") or {}).get("display_name") or "scoped"
            # keep the most-consumed scoped window if there are several
            if u.scoped_pct is None or (pct or 0) > u.scoped_pct:
                u.scoped_pct, u.scoped_reset, u.scoped_label = pct, reset, label
    if u.session_pct is None and isinstance(j.get("five_hour"), dict):
        u.session_pct = j["five_hour"].get("utilization")
        u.session_reset = _iso_to_epoch(j["five_hour"].get("resets_at"))
    if u.weekly_pct is None and isinstance(j.get("seven_day"), dict):
        u.weekly_pct = j["seven_day"].get("utilization")
        u.weekly_reset = _iso_to_epoch(j["seven_day"].get("resets_at"))
    active = [l for l in (j.get("limits") or []) if l.get("is_active")]
    u.status = active[0].get("severity") if active else "ok"
    if u.session_pct is None and u.weekly_pct is None:
        u.error = "usage endpoint returned no limit data"
    return u


_usage_cooldown: dict[str, float] = {}


def _rate_limited(err: str | None) -> bool:
    return bool(err) and ("rate_limit" in err or "429" in err)


def usage_for(cred_path: str) -> Usage:
    """Fetch usage for stored credentials, refreshing the token when needed
    (including one retry when a supposedly-valid token turns out revoked).

    An account the endpoint has just throttled is left alone for
    USAGE_ERROR_COOLDOWN_S: retrying it every poll only deepens the throttle,
    and the caller keeps showing its last good numbers meanwhile."""
    until = _usage_cooldown.get(cred_path)
    if until and now() < until:
        u = Usage()
        u.error = f"rate_limit_error (retrying in {until - now():.0f}s)"
        return u
    try:
        token = fresh_token(cred_path)
    except CcrollError as e:
        u = Usage(); u.error = str(e); return u
    u = fetch_usage(token)
    if u.error == "auth":
        try:
            creds = read_json(cred_path)
            new = oauth_refresh(oauth_of(creds) or {})
            creds["claudeAiOauth"] = new
            write_json_atomic(cred_path, creds)
            u = fetch_usage(new["accessToken"])
        except CcrollError as e:
            u = Usage(); u.error = str(e)
    if _rate_limited(u.error):
        _usage_cooldown[cred_path] = now() + USAGE_ERROR_COOLDOWN_S
    else:
        _usage_cooldown.pop(cred_path, None)
    return u


def _find_email(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "email" in k.lower() and isinstance(v, str) and "@" in v:
                return v
        for v in obj.values():
            found = _find_email(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_email(v)
            if found:
                return found
    return None


def fetch_email(token: str) -> str | None:
    status, data, neterr = _http("GET", API_BASE + PROFILE_PATH, token, None)
    if neterr or status != 200:
        return None
    try:
        return _find_email(json.loads(data.decode() or "{}"))
    except json.JSONDecodeError:
        return None


# --- persistent state -----------------------------------------------------------
def load_state(cfg: Cfg) -> dict:
    state = read_json(cfg.state_path) or {}
    state.setdefault("active", None)
    state.setdefault("emails", {})
    state.setdefault("samples", {})
    state.setdefault("events", [])
    state.setdefault("last_swap", 0)
    state.setdefault("grace_until", 0)
    state.setdefault("preload", [])
    return state


def save_state(cfg: Cfg, state: dict) -> None:
    write_json_atomic(cfg.state_path, state)


def add_event(state: dict, msg: str) -> None:
    state["events"] = (state["events"] + [[now(), msg]])[-100:]


def append_sample(state: dict, name: str, window: str, pct: float | None) -> None:
    if pct is None:
        return
    series = state["samples"].setdefault(name, {}).setdefault(window, [])
    # a drop of more than a few points means the window reset — restart the fit
    if series and pct < series[-1][1] - 3:
        series.clear()
    t = now()
    series.append([round(t, 1), round(pct, 3)])
    cutoff = t - SAMPLE_RETENTION_S
    while series and series[0][0] < cutoff:
        series.pop(0)


def reset_samples(state: dict, name: str) -> None:
    """Start the burn series of `name` afresh.  Called at every swap: samples
    taken while the account was idle in the fleet scan say nothing about how
    fast it will burn now, and the first post-swap poll shows a one-time
    jump (every running agent re-primes its context) that is not a rate."""
    state.setdefault("samples", {}).pop(name, None)


def burn_series(state: dict, name: str, key: str) -> list:
    """The samples the burn estimate may use.  For the active account,
    samples taken during the post-swap grace are left out: they record the
    preload (every agent re-priming its context), not a rate."""
    series = state.get("samples", {}).get(name, {}).get(key, [])
    if name == state.get("active"):
        cutoff = state.get("grace_until", 0)
        series = [s for s in series if s[0] >= cutoff]
    return series


def in_grace(state: dict, t: float | None = None) -> bool:
    return (now() if t is None else t) < state.get("grace_until", 0)


def burn_fit(series: list) -> tuple[float, float] | None:
    """Least-squares slope in %/hour over the recent sample window, plus the
    span in seconds the fit covers (short spans are provisional)."""
    if not series:
        return None
    latest = series[-1][0]
    pts = [(t, p) for t, p in series if t >= latest - BURN_WINDOW_S]
    if len(pts) < BURN_MIN_SAMPLES or pts[-1][0] - pts[0][0] < BURN_MIN_SPAN_S:
        return None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mp = sum(p for _, p in pts) / n
    denom = sum((t - mt) ** 2 for t, _ in pts)
    if denom == 0:
        return None
    return (sum((t - mt) * (p - mp) for t, p in pts) / denom) * 3600, pts[-1][0] - pts[0][0]


def eta_to_limit(pct: float | None, rate: float | None) -> float | None:
    if pct is None or rate is None or rate < BURN_MIN_RATE:
        return None
    return (100.0 - pct) / rate * 3600


# --- swap engine ----------------------------------------------------------------
def _mtime_ns(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def swap_identity(cfg: Cfg, target: Account) -> str | None:
    """Point Claude Code's *display* identity at the account we swapped to.

    `/status` reads the email and organization from the `oauthAccount` object
    in the global config, not from the credentials file, so after a swap it
    keeps naming the previous account.  Auth is unaffected — that follows the
    token — but the same object also feeds the organization header used by
    cloud sessions and remote control, plus telemetry and org-scoped gating.

    Only that one key is ever copied.  The live config also holds every
    project's session history, so the file itself is never replaced with the
    store's copy, and a read that looks truncated or that another session
    rewrote underneath us is skipped rather than written.

    Returns a line for the event log, or None when there was nothing to do."""
    ident = (read_json(os.path.join(cfg.root, target.name, CONFIG_FILE)) or {}).get("oauthAccount")
    if not isinstance(ident, dict) or not ident:
        return f"identity left as-is: no stored oauthAccount for {target.name}"
    path = cfg.live_config_path
    for _ in range(2):
        before = _mtime_ns(path)
        live = read_json(path)
        if not isinstance(live, dict) or len(live) < MIN_CONFIG_KEYS:
            return f"identity left as-is: {path} is missing, unreadable or truncated"
        if live.get("oauthAccount") == ident:
            return None
        patched = dict(live)
        patched["oauthAccount"] = ident
        if not set(patched) >= set(live):     # never lose a top-level key
            return "identity left as-is: live config changed shape mid-patch"
        if _mtime_ns(path) != before:
            continue                          # a session wrote in between: re-read
        write_json_atomic(path, patched)
        return f"identity synced to {target.name}"
    return "identity left as-is: live config is being written by another session"


def harvest(cfg: Cfg, state: dict) -> None:
    """Copy the live credentials back into the active account's store dir.
    Refresh tokens rotate, so the store must always hold the newest one."""
    name = state.get("active")
    live = read_json(cfg.live_path)
    if not name or not oauth_of(live):
        return
    write_json_atomic(os.path.join(cfg.root, name, CRED_FILE), live)


def do_swap(cfg: Cfg, state: dict, target: Account, reason: str,
            snapshot: "Usage | None" = None) -> str:
    """Swap the live credentials to `target`. Returns the human-readable detail
    ("left <prev>: <reason>") so callers can show the same text in notices.
    `snapshot` is the target's usage as last read: the preload is measured
    against it at the end of the grace period."""
    old = oauth_of(read_json(cfg.live_path)) or {}
    prev = state.get("active")
    harvest(cfg, state)

    creds = target.read()
    oauth = oauth_of(creds)
    if not oauth:
        raise CcrollError(f"account {target.name!r} has no credentials")
    left = expires_in_s(oauth)
    if left is None or left < REFRESH_MARGIN_S:
        oauth = oauth_refresh(oauth)
        creds["claudeAiOauth"] = oauth
        write_json_atomic(target.cred_path, creds)

    write_json_atomic(cfg.live_path, creds)
    state["active"] = target.name
    state["last_swap"] = now()
    state["grace_until"] = state["last_swap"] + cfg.grace
    reset_samples(state, target.name)
    state["swap_snapshot"] = ({"name": target.name, "t": state["last_swap"],
                               "session": snapshot.session_pct, "weekly": snapshot.weekly_pct,
                               "scoped": snapshot.scoped_pct}
                              if snapshot and not snapshot.error else None)
    detail = f"left {prev}: {reason}" if prev and prev != target.name else reason
    add_event(state, f"→ {target.name} ({detail})")
    if cfg.sync_identity:
        note = swap_identity(cfg, target)
        if note:
            add_event(state, note)
    save_state(cfg, state)

    # Guard against the one narrow race: the running CLI finishing a token
    # refresh of the OLD account and writing it back over our swap.
    time.sleep(SWAP_VERIFY_DELAY_S)
    seen = oauth_of(read_json(cfg.live_path)) or {}
    if seen.get("accessToken") != oauth["accessToken"] and (
        seen.get("refreshToken") == old.get("refreshToken")
        or seen.get("accessToken") == old.get("accessToken")
    ):
        write_json_atomic(cfg.live_path, creds)
        add_event(state, "re-asserted swap over a concurrent write")
        if cfg.sync_identity:
            swap_identity(cfg, target)        # re-assert the identity too, quietly
        save_state(cfg, state)
    return detail


def measure_preload(state: dict, usages: dict, cfg: Cfg) -> bool:
    """Once per swap, after the grace: how much of each window the swap itself
    cost — the jump since the swap minus the sustained burn over that time
    (raw deltas when no fit has appeared within PRELOAD_MEASURE_MAX_S of the
    grace ending).  Skipped if a window reset in between.  Returns True when
    a measurement was recorded."""
    snap = state.get("swap_snapshot")
    name = state.get("active")
    if not snap or snap.get("name") != name or in_grace(state):
        return False
    u = usages.get(name)
    if u is None or u.error:
        return False
    t = now()
    fit = burn_fit(burn_series(state, name, "session"))
    if fit is None and t < state.get("grace_until", 0) + PRELOAD_MEASURE_MAX_S:
        return False                      # give the post-grace fit a chance to settle
    elapsed_h = (t - snap["t"]) / 3600.0
    out = {"t": t, "name": name}
    for key, pct in (("session", u.session_pct), ("weekly", u.weekly_pct), ("scoped", u.scoped_pct)):
        before = snap.get(key)
        if pct is None or before is None:
            out[key] = None
            continue
        delta = pct - before
        if delta < -1:                    # the window reset since the swap
            state["swap_snapshot"] = None
            return False
        rate = active_burn(state, name, key) or 0.0
        out[key] = round(max(0.0, delta - rate * elapsed_h), 1)
    state["swap_snapshot"] = None
    state["preload"] = (state.get("preload", []) + [out])[-PRELOAD_KEEP:]
    label = (u.scoped_label or "scoped").lower()
    parts = [f"session +{out['session']:.0f}%"] if out.get("session") is not None else []
    if out.get("scoped") is not None:
        parts.append(f"{label} +{out['scoped']:.0f}%")
    add_event(state, f"preload on {name}: " + " · ".join(parts))
    return True


def preload_estimate(state: dict) -> dict | None:
    """Median of the recent preload measurements per window ({session,
    weekly, scoped, n}), or None until one exists."""
    rows = state.get("preload") or []
    if not rows:
        return None
    est = {"n": len(rows)}
    for key in ("session", "weekly", "scoped"):
        vals = sorted(r[key] for r in rows if r.get(key) is not None)
        est[key] = statistics.median(vals) if vals else None
    return est


def preload_cost(preload: dict | None, cfg: Cfg) -> float | None:
    """What a swap costs on the governing weekly window, in percent."""
    if not preload:
        return None
    return preload.get("weekly" if cfg.mode == "weekly" else "scoped")


# --- rotation policy ------------------------------------------------------------
def window_spent(pct: float | None, reset: float | None, threshold: float,
                 t: float | None = None) -> bool:
    """Is this window over its threshold *and* still in force?

    A percentage whose reset time has already passed is stale data: the
    window has rolled over and the endpoint simply has not caught up (an
    account reported at 100% with an expired reset reads 0% a minute later).
    Treating that as spent strands the account outside the candidate pool
    exactly when it has become the best one available."""
    if (pct or 0) < threshold:
        return False
    return not reset or reset > (now() if t is None else t)


def is_exhausted(u: Usage | None, cfg: Cfg) -> str | None:
    """Return the reason the account counts as spent, or None.

    A failed read yields None — missing data never rotates.  The watch loop
    handles the one case where that is not enough (the *active* account
    going unreadable) by falling back to its last good snapshot."""
    if u is None or u.error:
        return None  # never rotate on missing data
    if (u.status or "").lower() in ("rejected", "exceeded", "blocked"):
        return f"status {u.status}"
    if window_spent(u.session_pct, u.session_reset, cfg.threshold):
        return f"session {u.session_pct:.0f}%"
    if cfg.mode == "scoped" and window_spent(u.scoped_pct, u.scoped_reset, cfg.scoped_threshold):
        return f"{(u.scoped_label or 'scoped').lower()} weekly {u.scoped_pct:.0f}%"
    if window_spent(u.weekly_pct, u.weekly_reset, 99.5):
        return f"weekly {u.weekly_pct:.0f}%"
    return None


def rotation_usage(last_good: dict, name: str, u: Usage | None) -> tuple[Usage | None, float | None]:
    """The usage to base the ACTIVE account's rotation decision on.

    A usage read that fails is exactly what a throttled or rate-limited
    account does, and refusing to act on it strands the live session on a
    dead account.  A window's percentage only ever rises until it resets, so
    a recent error-free snapshot is still a sound basis for "this account is
    spent" — and `is_exhausted` ignores any window whose reset has since
    passed.  Returns (usage, age of the snapshot in seconds), age None when
    the current read is good."""
    if u is not None and not u.error:
        return u, None
    snap = last_good.get(name)
    if snap is None:
        return None, None
    age = now() - snap.fetched_at
    if age > LAST_GOOD_MAX_AGE_S:
        return None, None
    return snap, age


def active_burn(state: dict, name: str, key: str) -> float | None:
    """Conservative burn estimate for one window of the active account, %/h:
    the larger of the least-squares slope and the *sustained* recent slope,
    so a burst that just started is caught even though the 45-minute fit
    lags.  "Sustained" means the smaller of the last two step slopes: a
    single jump between two polls (the re-prime after a swap) does not count
    until the next poll confirms it.  None until there is a fit at all;
    the series restarts at every swap and the post-swap grace is skipped,
    so a fit exists about 2 minutes after the grace ends."""
    series = burn_series(state, name, key)
    fit = burn_fit(series)
    if fit is None:
        return None
    rate = fit[0]
    if len(series) >= 3:
        steps = []
        for (t0, p0), (t1, p1) in (series[-3:-1], series[-2:]):
            if t1 > t0:
                steps.append((p1 - p0) / (t1 - t0) * 3600)
        if len(steps) == 2:
            rate = max(rate, min(steps))
    return rate


def window_eta(state: dict, name: str, key: str, pct: float | None):
    """The one place a window's burn is turned into a decision: (rate, fit,
    eta, provisional).  `rate` is the conservative estimate ccroll acts on
    (active_burn), `fit` the plain least-squares slope for reference, `eta`
    the seconds to 100% at `rate` (None: no estimate or negligible burn),
    `provisional` whether the fit still covers a short span.  The dashboard,
    the early-rotation rule and the runway gate all read this, so what is
    shown is always what is acted on."""
    series = burn_series(state, name, key)
    fit = burn_fit(series)
    if fit is None:
        return None, None, None, False
    rate = active_burn(state, name, key)
    return rate, fit[0], eta_to_limit(pct, rate), fit[1] < BURN_SETTLED_SPAN_S


def limit_etas(state: dict, name: str, u: Usage, cfg: Cfg) -> list:
    """(key, label, pct, rate, eta) for every window that can trigger
    rotation on the active account, in display order; `rate` is None when
    the window has no burn estimate yet."""
    windows = [("session", "session", u.session_pct), ("weekly", "weekly", u.weekly_pct)]
    if cfg.mode == "scoped":
        windows.append(("scoped", (u.scoped_label or "scoped").lower() + " weekly", u.scoped_pct))
    out = []
    for key, label, pct in windows:
        if pct is None:
            continue
        rate, _, eta, _ = window_eta(state, name, key, pct)
        out.append((key, label, pct, rate, eta))
    return out


def nearest_limit(state: dict, name: str, u: Usage, cfg: Cfg):
    """The window closest to its limit at the current burn: (label, pct,
    rate, eta), or None when no window has an estimate and a finite ETA."""
    best = None
    for _, label, pct, rate, eta in limit_etas(state, name, u, cfg):
        if eta is not None and (best is None or eta < best[3]):
            best = (label, pct, rate, eta)
    return best


def _fmt_eta_short(secs: float) -> str:
    return "<1m" if secs < 60 else f"{secs / 60:.0f}m"


def about_to_exhaust(state: dict, name: str, u: Usage | None, cfg: Cfg) -> str | None:
    """Burn-based early exhaustion for the active account: the reason it is
    about to hit a limit within cfg.lead seconds at its current burn, or None.
    Only the active account has a burn series; missing data never rotates."""
    if u is None or u.error or not name or name == LIVE_PSEUDO or in_grace(state):
        return None
    for _, label, pct, rate, eta in limit_etas(state, name, u, cfg):
        if pct < EARLY_MIN_PCT:
            continue
        if eta is not None and eta <= cfg.lead:
            return f"{label} {pct:.0f}% · ≈{_fmt_eta_short(eta)} to limit at {rate:.0f}%/h"
    return None


def governing_window(u: Usage, cfg: Cfg) -> tuple[float | None, float | None]:
    """(pct, reset) of the weekly window that governs rotation in this mode."""
    if cfg.mode == "weekly":
        return u.weekly_pct, u.weekly_reset
    return u.scoped_pct, u.scoped_reset


def perish_rate(pct: float | None, reset: float | None, t: float | None = None) -> float | None:
    """How fast the unused part of a weekly window is being lost, in %/h.

    Weekly windows roll: they open on first use and reset a week later, so
    whatever headroom is still unused at the reset simply vanishes.  Spending
    from an account whose reset is near costs nothing in the long run, while
    spending from one that resets in six days eats reserve for six days.

    None means the window is not open (no reset pending, or a stale reset in
    the past): the account is fresh, and using it costs nothing either — it
    only starts the next week's clock, which the sooner the better."""
    t = now() if t is None else t
    if not reset or reset <= t:
        return None
    hours = max((reset - t) / 3600.0, 1.0 / 60)
    return (100.0 - (pct or 0)) / hours


def pick_target(usages: dict, cfg: Cfg, exclude: str | None, t: float | None = None,
                preload: dict | None = None) -> str | None:
    """The account whose governing weekly headroom is most perishable.

    Order: accounts whose weekly window is not open come first (spending
    there is free and starts their clock at once); then, by how fast the
    remaining headroom is being lost — headroom over hours until reset — so
    the account that resets soonest with the most unused quota is drained
    first, and accounts with a distant reset are kept as reserve.  Least
    loaded is the tie-break, and the key ends in the account name so equal
    usage always resolves the same way, whatever order the scan returned."""
    t = now() if t is None else t

    def key(item):
        name, u = item
        pct, reset = governing_window(u, cfg)
        rate = perish_rate(pct, reset, t)
        return (rate is not None, -(rate or 0), pct or 0, u.session_pct or 0, name)

    strict, ok = candidates(usages, cfg, exclude, preload)
    pool = strict or ok
    return min(pool, key=key)[0] if pool else None


def candidates(usages: dict, cfg: Cfg, exclude: str | None,
               preload: dict | None = None) -> tuple[list, list]:
    """(strict, ok): accounts that could take over.  `ok` is anyone not spent;
    `strict` is the subset comfortably clear of every threshold (hysteresis)
    and, when the preload is known, of what the swap itself will cost."""
    ok = [(n, u) for n, u in usages.items()
          if n != exclude and n != LIVE_PSEUDO
          and u and not u.error and u.session_pct is not None
          and not is_exhausted(u, cfg)]
    strict = [(n, u) for n, u in ok if comfortable(u, cfg, preload)]
    return strict, ok


def comfortable(u: Usage, cfg: Cfg, preload: dict | None = None) -> bool:
    """Comfortably clear of every threshold — and, once the preload has been
    measured, with enough headroom that the swap itself (every agent
    re-priming its context on the new account) leaves room to work."""
    if not ((u.session_pct or 0) < cfg.threshold - 15
            and (u.weekly_pct or 0) < 95
            and (cfg.mode != "scoped" or (u.scoped_pct or 0) < 90)):
        return False
    if preload:
        need_s = preload.get("session")
        if need_s is not None and 100 - (u.session_pct or 0) <= 1.2 * need_s + 5:
            return False
        gov_pct = u.weekly_pct if cfg.mode == "weekly" else u.scoped_pct
        need_g = preload_cost(preload, cfg)
        if need_g is not None and 100 - (gov_pct or 0) <= 1.2 * need_g + 3:
            return False
    return True


def runway_s(state: dict, name: str, u: Usage, cfg: Cfg) -> float | None:
    """Seconds until the active account is expected to hit a limit at its
    current burn, across every window that can trigger rotation.  None when
    there is no burn estimate yet; inf when the burn is negligible."""
    etas, fitted = [], False
    for _, _, _, rate, eta in limit_etas(state, name, u, cfg):
        if rate is None:
            continue
        fitted = True
        if eta is not None:
            etas.append(eta)
    if not fitted:
        return None
    return min(etas) if etas else float("inf")


def preempt_target(cfg: Cfg, state: dict, usages: dict, active: str,
                   t: float | None = None) -> tuple[str, str] | None:
    """A better account to be on *before* the active one is spent, or None.

    In a Fable-bound fleet the account to sit on is the one whose governing
    window resets next: its unused headroom is the first to vanish, and being
    on it when it resets reopens its next week with no idle gap.  Reset times
    never move once a window is open, so this target is stable — no flapping.
    With --touch, an account whose window is not open at all is taken first,
    for one request, so its next week starts now rather than hours later.

    Gates: a burn estimate must exist and give the active account more than
    --preempt-runway of headroom (when sessions bind, runway is short and a
    pre-emptive swap would only add a cache re-prime); the active window must
    be open (a touch or reset is still taking effect); no swap in the last
    PREEMPT_MIN_GAP_S and the post-swap grace over; and the measured preload
    on the governing window must not exceed --preempt-max-cost."""
    t = now() if t is None else t
    u = usages.get(active)
    if u is None or u.error or active == LIVE_PSEUDO:
        return None
    if t - state.get("last_swap", 0) < PREEMPT_MIN_GAP_S or in_grace(state, t):
        return None
    preload = preload_estimate(state)
    cost = preload_cost(preload, cfg)
    if cost is not None and cost > cfg.preempt_max_cost:
        return None                      # a swap costs more than it can save
    a_pct, a_reset = governing_window(u, cfg)
    if perish_rate(a_pct, a_reset, t) is None:
        return None                      # our own window is not open yet
    runway = runway_s(state, active, u, cfg)
    if runway is None or runway <= cfg.preempt_runway:
        return None
    strict, _ = candidates(usages, cfg, exclude=active, preload=preload)
    label = scoped_label_of(usages).lower() if cfg.mode == "scoped" else "weekly"
    if cfg.touch:
        fresh = [n for n, c in strict if perish_rate(*governing_window(c, cfg), t) is None]
        if fresh:
            return min(fresh), f"opens a fresh {label} week"
    opened = [(governing_window(c, cfg)[1], n) for n, c in strict
              if perish_rate(*governing_window(c, cfg), t) is not None]
    if not opened:
        return None
    reset, name = min(opened)
    if reset >= a_reset:
        return None
    return name, f"{label} resets {fmt_dur(a_reset - reset)} sooner, in {fmt_dur(reset - t)}"


def target_reason(u: Usage, cfg: Cfg, label: str) -> str:
    """Why this account is next, for the dashboard."""
    pct, reset = governing_window(u, cfg)
    name = "weekly" if cfg.mode == "weekly" else label.lower()
    rate = perish_rate(pct, reset)
    if rate is None:
        return f"{name} window not open — fresh week starts on use"
    return (f"{100 - (pct or 0):.0f}% {name} headroom expires in {fmt_dur(reset - now())} "
            f"· {u.session_pct or 0:.0f}% session")


def verified_target(cfg: Cfg, usages: dict, active: str | None,
                    preload: dict | None = None) -> str | None:
    """pick_target, but with the choice confirmed against a fresh read before we
    commit to it.  Fleet data can be up to `--scan` seconds old, and an account
    may have been consumed elsewhere in that time; never swap onto one that is
    already spent.  Rejected candidates stay rejected for this pass."""
    skip: set[str] = set()
    for _ in range(MAX_TARGET_TRIES):
        pool = {n: u for n, u in usages.items() if n not in skip}
        target = pick_target(pool, cfg, exclude=active, preload=preload)
        if not target:
            return None
        fresh = usage_for(get_account(cfg, target).cred_path)
        if fresh.error:
            skip.add(target)          # unreadable now; try the next best
            continue
        usages[target] = fresh        # keep the dashboard honest either way
        if not is_exhausted(fresh, cfg):
            return target
        skip.add(target)
    return None


def earliest_recovery(usages: dict, cfg: Cfg) -> tuple[float, str] | None:
    """When every account is spent: the first moment any of them frees up.
    An account recovers when ALL of its binding limits have reset (max of its
    resets); the fleet recovers at the min of that across accounts.

    Only resets still in the future count.  A reset already in the past has
    happened, so the moment it names is not a moment to wait for — returning
    it would schedule a rescan in the past and spin the watch loop."""
    t = now()
    best = None
    for name, u in usages.items():
        if name == LIVE_PSEUDO or u is None or u.error or not is_exhausted(u, cfg):
            continue
        resets = []
        if window_spent(u.session_pct, u.session_reset, cfg.threshold, t) and u.session_reset:
            resets.append(u.session_reset)
        if cfg.mode == "scoped" and window_spent(u.scoped_pct, u.scoped_reset,
                                                 cfg.scoped_threshold, t) and u.scoped_reset:
            resets.append(u.scoped_reset)
        if window_spent(u.weekly_pct, u.weekly_reset, 99.5, t) and u.weekly_reset:
            resets.append(u.weekly_reset)
        resets = [r for r in resets if r > t]
        if resets and (best is None or max(resets) < best[0]):
            best = (max(resets), name)
    return best


def merged_view(usages: dict, last_good: dict) -> dict:
    """Usages for display: an account whose read just failed keeps showing its
    last good numbers, with the error noted, rather than blanking its row."""
    view = {}
    for name, u in usages.items():
        snap = last_good.get(name)
        if u is not None and u.error and snap is not None \
                and now() - snap.fetched_at <= LAST_GOOD_MAX_AGE_S:
            shown = copy.copy(snap)
            shown.stale_note = u.error
            view[name] = shown
        else:
            view[name] = u
    return view


# --- rendering ------------------------------------------------------------------
def _pct_cell(a: Ansi, pct, reset):
    if pct is None:
        return a.dim("—"), 1
    plain = f"{pct:.0f}%"
    dur, dur_len = fmt_dur3(a, (reset - now()) if reset else None)
    return sev_color(a, pct)(f"{plain:>4}") + a.dim(" ↺") + dur, max(len(plain), 4) + 2 + dur_len


def _status_cell(a: Ansi, u: Usage):
    if u.stale_note:
        msg = u.stale_note if len(u.stale_note) <= 44 else u.stale_note[:41] + "…"
        return a.yellow(msg), len(msg)
    if u.error:
        msg = u.error if len(u.error) <= 44 else u.error[:41] + "…"
        return a.red(msg), len(msg)
    s = (u.status or "—").lower()
    if s in ("ok", "allowed", "normal"):
        return a.green("ok"), 2
    if s in ("rejected", "exceeded", "blocked"):
        return a.red("BLOCKED"), 7
    return a.yellow(s), len(s)


def render_table(a: Ansi, rows: list, scoped_label: str) -> list[str]:
    headers = ["", "Account", "Session (5h)", "Weekly · all", f"Weekly · {scoped_label}", "Status"]
    cells = [[(a.bold(h), len(h)) for h in headers]]
    for name, u, active in rows:
        mark = ("►", 1) if active else ("", 0)
        label = (a.cyan(a.bold(name)) if active else name, len(name))
        if u is None:
            row = [mark, label, (a.dim("…"), 1), ("", 0), ("", 0), (a.dim("fetching"), 8)]
        else:
            row = [mark, label,
                   _pct_cell(a, u.session_pct, u.session_reset),
                   _pct_cell(a, u.weekly_pct, u.weekly_reset),
                   _pct_cell(a, u.scoped_pct, u.scoped_reset),
                   _status_cell(a, u)]
        cells.append(row)
    widths = [max(w for _, w in col) for col in zip(*cells)]
    lines = []
    for i, row in enumerate(cells):
        line = "  ".join(text + " " * (widths[c] - w) for c, (text, w) in enumerate(row))
        lines.append(line.rstrip())
        if i == 0:
            lines.append(a.dim("  ".join("─" * w for w in widths)))
    return lines


def render_burn(a: Ansi, state: dict, name: str, u: Usage | None, scoped_label: str) -> list[str]:
    if not u or u.error:
        return []
    lines = [a.bold(f"burn · {name}")]
    windows = [("session", u.session_pct, u.session_reset),
               ("weekly·all", u.weekly_pct, u.weekly_reset),
               (f"weekly·{scoped_label.lower()}", u.scoped_pct, u.scoped_reset)]
    keys = ["session", "weekly", "scoped"]
    for (label, pct, reset), key in zip(windows, keys):
        if pct is None:
            continue
        # the same conservative rate the rotation rules act on, so the ETA
        # shown is the ETA ccroll will move on
        rate, fit_rate, eta, provisional = window_eta(state, name, key, pct)
        reset_in = (reset - now()) if reset else None
        parts = [f"{pct:5.1f}%"]
        # fixed-width burn cell ("0000.0%/h") so rows stay aligned as the
        # rate swings from single digits to hundreds or thousands
        if rate is None:
            burn = "—"
        elif rate < BURN_MIN_RATE:
            burn = "~0%/h"
        else:
            burn = f"{rate:.1f}%/h"
        parts.append(a.dim(f"burn {burn:>9}") if provisional else f"burn {burn:>9}")
        # duration cells are padded to "0d 00h 00m" so a "—" (no burn data,
        # or already at the limit) doesn't shift the columns after it
        def dur_cell(secs: float | None) -> str:
            text, width = fmt_dur3(a, secs)
            return text + " " * max(0, 10 - width)
        parts.append("limit in ≈" + dur_cell(eta))
        if reset_in is not None and reset_in > 0:
            parts.append("resets in " + dur_cell(reset_in))
        verdict = ""
        if eta is not None and reset_in is not None and reset_in > 0:
            verdict = a.green("✓ reset first") if reset_in < eta else a.red("⚠ limit first")
        # when the sustained recent slope is what drives the figure, keep the
        # plain fit visible so the smoothing is not a mystery
        note = ""
        if rate is not None and fit_rate is not None and rate > max(fit_rate, BURN_MIN_RATE) * 1.2:
            note = a.dim(f"(fit {fit_rate:.1f}%/h)")
        tail = "  ".join(x for x in (verdict, note) if x)
        lines.append(f"  {label:<14}" + "  ·  ".join(parts) + ("  " + tail if tail else ""))
    if in_grace(state):
        lines.append(a.dim(f"  post-swap grace: {fmt_dur(state['grace_until'] - now())} left — "
                           "burn estimate and early rotation paused while agents re-prime"))
    est = preload_estimate(state)
    if est:
        bits = []
        if est.get("session") is not None:
            bits.append(f"session {est['session']:.0f}%")
        if est.get("scoped") is not None:
            bits.append(f"{scoped_label.lower()} {est['scoped']:.0f}%")
        elif est.get("weekly") is not None:
            bits.append(f"weekly {est['weekly']:.0f}%")
        lines.append(a.dim("  swap cost ≈ " + " · ".join(bits) + f" (n={est['n']})"))
    return lines


# --- scanning -------------------------------------------------------------------
def scan_accounts(cfg: Cfg, accounts: list[Account], active: str | None) -> dict:
    """Fetch usage for every account in parallel.  The active account is read
    through the LIVE credentials file, which the running CLI keeps freshest."""
    # A fleet-wide scan can need a token refresh for every account at once.
    # Firing those simultaneously trips the endpoint's rate limiter, so each
    # worker waits out a slot before starting.
    def one(item: tuple[int, Account]) -> tuple[str, Usage]:
        idx, acc = item
        if idx:
            time.sleep(REFRESH_STAGGER_S * idx)
        path = cfg.live_path if acc.name == active else acc.cred_path
        return acc.name, usage_for(path)

    results: dict[str, Usage] = {}
    if not accounts:
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(accounts))) as ex:
        for name, usage in ex.map(one, enumerate(accounts)):
            results[name] = usage
    return results


def record_samples(state: dict, usages: dict) -> None:
    for name, u in usages.items():
        if u and not u.error:
            for window, pct in u.windows().items():
                append_sample(state, name, window, pct)


def scoped_label_of(usages: dict) -> str:
    for u in usages.values():
        if u and u.scoped_label:
            return u.scoped_label
    return "scoped"


# --- live-file change monitor ---------------------------------------------------
class LiveMonitor:
    """Detects external changes to the live credentials file and classifies
    them: a token refresh by the CLI (harvest it), a manual /login to another
    known account (follow it), or an unknown login (pause rotation)."""

    def __init__(self, cfg: Cfg, state: dict):
        self.cfg = cfg
        self.state = state
        self.mtime = self._stat()
        self.expected_access = (oauth_of(read_json(cfg.live_path)) or {}).get("accessToken")

    def _stat(self) -> float:
        try:
            return os.stat(self.cfg.live_path).st_mtime
        except OSError:
            return 0.0

    def note_own_write(self) -> None:
        self.mtime = self._stat()
        self.expected_access = (oauth_of(read_json(self.cfg.live_path)) or {}).get("accessToken")

    def check(self) -> str | None:
        """Returns an event message when something noteworthy happened."""
        m = self._stat()
        if m == self.mtime:
            return None
        self.mtime = m
        oauth = oauth_of(read_json(self.cfg.live_path))
        if not oauth or oauth.get("accessToken") == self.expected_access:
            return None
        self.expected_access = oauth.get("accessToken")
        state, cfg = self.state, self.cfg
        email = fetch_email(oauth["accessToken"])
        active = state.get("active")
        if email and active and email == state["emails"].get(active):
            harvest(cfg, state)   # the CLI refreshed its token: keep the store current
            return None
        if email:
            existing = {acc.name for acc in list_accounts(cfg)}
            for name, known in state["emails"].items():
                if known == email and name in existing:
                    state["active"] = name
                    add_event(state, f"external login detected → {name}")
                    save_state(cfg, state)
                    return f"external login detected → {name}"
        state["active"] = None
        add_event(state, "unknown login in live config — rotation paused (run `ccroll adopt`)")
        save_state(cfg, state)
        return "unknown login — rotation paused"


# --- keyboard -------------------------------------------------------------------
class Keyboard:
    def __init__(self):
        self.enabled = sys.stdin.isatty()
        self._saved = None

    def __enter__(self):
        if self.enabled:
            import termios, tty
            self._saved = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)

    def read(self, timeout: float) -> str | None:
        if not self.enabled:
            time.sleep(timeout)
            return None
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if r:
            return sys.stdin.read(1)
        return None


# --- commands -------------------------------------------------------------------
def build_rows(cfg: Cfg, state: dict, accounts: list[Account], usages: dict):
    """Rows for the table; when the live login matches no store, show it as a
    pseudo-account so the current session is always visible."""
    active = state.get("active")
    rows = [(acc.name, usages.get(acc.name), acc.name == active) for acc in accounts]
    if active is None and oauth_of(read_json(cfg.live_path)):
        rows.insert(0, (LIVE_PSEUDO, usages.get(LIVE_PSEUDO), True))
    return rows


def cmd_status(cfg: Cfg, a: Ansi) -> int:
    state = load_state(cfg)
    accounts = list_accounts(cfg)
    usages = scan_accounts(cfg, accounts, state.get("active"))
    if state.get("active") is None and oauth_of(read_json(cfg.live_path)):
        usages[LIVE_PSEUDO] = usage_for(cfg.live_path)
    record_samples(state, usages)
    save_state(cfg, state)
    label = scoped_label_of(usages)
    print(a.bold(f"ccroll {CCROLL_VERSION} · {len(accounts)} account(s) · {datetime.now().strftime('%Y-%m-%d %H:%M %Z')}"))
    print()
    for line in render_table(a, build_rows(cfg, state, accounts, usages), label):
        print(line)
    active = state.get("active")
    if active and usages.get(active):
        print()
        for line in render_burn(a, state, active, usages[active], label):
            print(line)
    target = pick_target(usages, cfg, exclude=active, preload=preload_estimate(state))
    if target:
        u = usages[target]
        print()
        print(a.green(f"→ best fallback: {target} ({target_reason(u, cfg, label)})"))
    elif accounts:
        recovery = earliest_recovery(usages, cfg)
        if recovery:
            when, who = recovery
            print()
            print(a.red(f"→ no account has headroom — {who} recovers first, in {fmt_dur(when - now())}"))
    if not accounts:
        print()
        print(a.yellow("No accounts yet — add them with:  ccroll add"))
    return 0


def cmd_watch(cfg: Cfg, a: Ansi) -> int:
    state = load_state(cfg)
    accounts = list_accounts(cfg)
    if not accounts:
        print(a.yellow("No accounts under " + cfg.root))
        print("Add each subscription account with:  ccroll add")
        return 1
    monitor = LiveMonitor(cfg, state)
    usages: dict[str, Usage] = {}
    last_good: dict[str, Usage] = {}   # newest error-free read per account
    next_scan = 0.0
    next_active_poll = 0.0
    paused = not cfg.rotate
    notice = ""
    tty_out = sys.stdout.isatty()

    def rescan():
        nonlocal usages, next_scan, next_active_poll
        accounts[:] = list_accounts(cfg)
        fresh = scan_accounts(cfg, accounts, state.get("active"))
        if state.get("active") is None and oauth_of(read_json(cfg.live_path)):
            fresh[LIVE_PSEUDO] = usage_for(cfg.live_path)
        usages = fresh
        remember_good(usages)
        record_samples(state, usages)
        save_state(cfg, state)
        next_scan = now() + cfg.scan
        next_active_poll = now() + poll_interval()

    def remember_good(fresh: dict) -> None:
        for name, u in fresh.items():
            if u is not None and not u.error and u.session_pct is not None:
                last_good[name] = u

    def poll_interval() -> float:
        """Poll the active account every 15 s while a limit is under ten
        minutes away at the current burn, else at --interval."""
        name = state.get("active")
        u = usages.get(name) if name else None
        if u and not u.error:
            r = runway_s(state, name, u, cfg)
            if r is not None and r < FAST_POLL_BELOW_S:
                return FAST_POLL_S
        return cfg.interval

    def poll_active():
        nonlocal next_active_poll
        name = state.get("active")
        key = name or LIVE_PSEUDO
        if oauth_of(read_json(cfg.live_path)):
            usages[key] = usage_for(cfg.live_path)
            remember_good({key: usages[key]})
            if name:
                record_samples(state, {name: usages[key]})
                measure_preload(state, usages, cfg)
                save_state(cfg, state)
        next_active_poll = now() + poll_interval()

    def maybe_rotate():
        nonlocal notice, next_scan
        if paused or state.get("active") is None:
            return
        if now() - state.get("last_swap", 0) < cfg.cooldown:
            return
        active = state["active"]
        live = usages.get(active)
        # A read that fails is what a throttled account does; fall back to the
        # last good snapshot rather than sitting on an account we cannot see.
        u_act, age = rotation_usage(last_good, active, live)
        reason = is_exhausted(u_act, cfg)
        if reason is None and age is None:
            reason = about_to_exhaust(state, active, u_act, cfg)
        if reason and age is not None:
            reason += (f" (last good read {fmt_dur(age)} ago) · "
                       f"usage read: {live.error if live else 'unavailable'}")
        if not reason:
            clear_hold_notice()
            if cfg.preempt:
                maybe_preempt(active)
            return
        target = verified_target(cfg, usages, active, preload_estimate(state))
        if not target:
            # everyone is spent: hold position and rescan the moment the first
            # account's binding limits have reset (plus a small settle buffer)
            recovery = earliest_recovery(usages, cfg)
            if recovery:
                when, who = recovery
                notice = (f"all accounts exhausted — waiting for {who} "
                          f"(recovers in {fmt_dur(when - now())})")
                next_scan = min(next_scan, max(when + 30, now() + RESCAN_MIN_S))
            else:
                notice = "active account exhausted but no fallback has headroom"
                next_scan = min(next_scan, now() + max(RESCAN_MIN_S, cfg.interval))
            return
        clear_hold_notice()
        try:
            detail = do_swap(cfg, state, get_account(cfg, target), reason, usages.get(target))
            monitor.note_own_write()
            notice = f"rotated to {target} ({detail})"
            poll_active()
        except CcrollError as e:
            notice = f"rotation failed: {e}"
            add_event(state, notice)
            save_state(cfg, state)

    def clear_hold_notice():
        """Drop a stale "nothing has headroom" banner once that stops being
        true — it outlived its condition and read as a live state."""
        nonlocal notice
        if notice.startswith(("all accounts exhausted", "active account exhausted")):
            notice = ""

    def maybe_preempt(active: str):
        """Pre-emptive move while the active account still has headroom; the
        target is confirmed with a fresh read like any other swap."""
        nonlocal notice
        choice = preempt_target(cfg, state, usages, active)
        if not choice:
            return
        target, why = choice
        fresh = usage_for(get_account(cfg, target).cred_path)
        if fresh.error:
            return
        usages[target] = fresh
        if is_exhausted(fresh, cfg) or not comfortable(fresh, cfg, preload_estimate(state)):
            return
        if preempt_target(cfg, state, usages, active) != choice:
            return                        # the fresh read changed the picture
        try:
            detail = do_swap(cfg, state, get_account(cfg, target), why, fresh)
            monitor.note_own_write()
            notice = f"moved early to {target} ({detail})"
            poll_active()
        except CcrollError as e:
            notice = f"rotation failed: {e}"
            add_event(state, notice)
            save_state(cfg, state)

    def render() -> str:
        label = scoped_label_of(usages)
        active = state.get("active")
        govern = (f"{label.lower()}≥{cfg.scoped_threshold:.0f}%" if cfg.mode == "scoped"
                  else "weekly·all≥99%")
        early = (f" · early to next reset when runway>{cfg.preempt_runway / 3600:g}h"
                 + (" +touch" if cfg.touch else "")) if cfg.preempt else ""
        mode = (a.red("rotation PAUSED") if paused
                else a.green(f"auto-rotate at session≥{cfg.threshold:.0f}% / {govern} "
                             f"or ≤{cfg.lead:.0f}s from a limit{early}"))
        head = a.bold(f"ccroll {CCROLL_VERSION}") + a.dim("  ·  ") + mode + a.dim("  ·  ") + fmt_clock(now())
        lines = [head, ""]
        lines += render_table(a, build_rows(cfg, state, accounts,
                                            merged_view(usages, last_good)), label)
        if active and usages.get(active):
            lines += [""] + render_burn(a, state, active, usages[active], label)
            soon = about_to_exhaust(state, active, usages[active], cfg)
            if soon:
                lines.append(a.red(f"  ⚠ rotating early: {soon}"))
            elif poll_interval() != cfg.interval:
                near = nearest_limit(state, active, usages[active], cfg)
                what = (f"{near[0]} limit in ≈{fmt_dur(near[3])} at {near[2]:.0f}%/h"
                        if near else f"limit under {FAST_POLL_BELOW_S // 60} min away")
                lines.append(a.yellow(f"  ⚠ {what} — polling every {FAST_POLL_S} s"))
        target = pick_target(usages, cfg, exclude=active, preload=preload_estimate(state))
        if target:
            lines += ["", a.dim("next in line: ") + a.green(target)
                      + a.dim(f"  ({target_reason(usages[target], cfg, label)})")]
        if notice:
            lines += ["", a.yellow(notice)]
        events = state.get("events", [])[-4:]
        if events:
            lines += ["", a.bold("events")]
            lines += [a.dim(f"  {fmt_clock(t)}  {msg}") for t, msg in events]
        lines += ["", a.dim("[q]uit  [r]otate now  [s]can  [p]ause auto-rotate")]
        return "\n".join(lines)

    if tty_out:
        sys.stdout.write("\033[?1049h\033[?25l")
        sys.stdout.flush()
    try:
        with Keyboard() as kb:
            running = True
            while running:
                if now() >= next_scan:
                    rescan()
                elif now() >= next_active_poll:
                    poll_active()
                changed = monitor.check()
                if changed:
                    notice = changed
                maybe_rotate()
                if tty_out:
                    frame = render().replace("\n", "\033[K\n")
                    sys.stdout.write("\033[H" + frame + "\033[K\033[J")
                    sys.stdout.flush()
                else:
                    act = state.get("active") or LIVE_PSEUDO
                    u = usages.get(act)
                    if u and not u.error:
                        print(f"{fmt_clock(now())} active={act} session={u.session_pct or 0:.0f}% "
                              f"weekly={u.weekly_pct or 0:.0f}% scoped={u.scoped_pct or 0:.0f}%"
                              + (f" · {notice}" if notice else ""))
                    elif u:
                        print(f"{fmt_clock(now())} active={act} error: {u.error}")
                    time.sleep(max(cfg.interval - 1, 1))
                key = kb.read(1.0)
                if key in ("q", "\x03"):
                    running = False
                elif key == "s":
                    next_scan = 0
                elif key == "p":
                    paused = not paused
                    notice = "auto-rotation paused" if paused else "auto-rotation resumed"
                elif key == "r":
                    target = verified_target(cfg, usages, state.get("active"), preload_estimate(state))
                    if target:
                        try:
                            detail = do_swap(cfg, state, get_account(cfg, target), "manual (keypress)",
                                             usages.get(target))
                            monitor.note_own_write()
                            notice = f"rotated to {target} ({detail})"
                            poll_active()
                        except CcrollError as e:
                            notice = f"rotation failed: {e}"
                    else:
                        notice = "no fallback with headroom"
    finally:
        if tty_out:
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()
        save_state(cfg, state)
    return 0


LOGIN_HELP = """\
Claude Code will now open with a fresh, isolated profile.
  1. Complete the login it offers (choose the Claude subscription account).
  2. When the normal prompt appears, quit immediately: /exit (or Ctrl+C twice).
Nothing you do in that window affects your real sessions.  The account is
named by its own email address, read from the account after login.
"""


def _email_of_creds(oauth: dict, retries: int = 3) -> str | None:
    for i in range(retries):
        email = fetch_email(oauth["accessToken"])
        if email:
            return email
        if i < retries - 1:
            time.sleep(2)
    return None


def _register(cfg: Cfg, state: dict, email: str, creds: dict) -> None:
    """File credentials under the account's email (the enforced identity)."""
    if not NAME_RE.match(email):
        raise CcrollError(f"account email {email!r} is not usable as a directory name")
    d = os.path.join(cfg.root, email)
    os.makedirs(d, mode=0o700, exist_ok=True)
    write_json_atomic(os.path.join(d, CRED_FILE), creds)
    state["emails"][email] = email


def cmd_add(cfg: Cfg, a: Ansi) -> int:
    state = load_state(cfg)
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise CcrollError("`claude` not found on PATH")
    os.makedirs(cfg.root, mode=0o700, exist_ok=True)
    while True:
        # log in into a temp profile first — the account's email, read from the
        # account itself, then becomes the name (no chance of mislabeling)
        tmp = tempfile.mkdtemp(prefix=".login-", dir=cfg.root)
        write_json_atomic(os.path.join(tmp, ".claude.json"), {"hasCompletedOnboarding": True})
        print(a.bold("── add account ") + a.dim("─" * 45))
        print(LOGIN_HELP)
        env = dict(os.environ, CLAUDE_CONFIG_DIR=tmp)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)  # must not shadow the file login
        subprocess.run([claude_bin], env=env)
        creds = read_json(os.path.join(tmp, CRED_FILE))
        oauth = oauth_of(creds)
        if not oauth:
            shutil.rmtree(tmp, ignore_errors=True)
            print(a.red("✗ no credentials were stored — login not completed?"))
        else:
            if "user:profile" not in (oauth.get("scopes") or []):
                print(a.yellow("⚠ token lacks user:profile scope (console login?) — "
                               "usage cannot be read; use the Claude-account login instead"))
            email = _email_of_creds(oauth)
            if not email:
                shutil.rmtree(tmp, ignore_errors=True)
                print(a.red("✗ could not read the account's email (network?) — nothing saved, try again"))
            else:
                dest = os.path.join(cfg.root, email)
                if os.path.isdir(dest):
                    _register(cfg, state, email, creds)
                    shutil.rmtree(tmp, ignore_errors=True)
                    print(a.green(f"↻ {email} — credentials updated (account already existed)"))
                else:
                    if not NAME_RE.match(email):
                        shutil.rmtree(tmp, ignore_errors=True)
                        raise CcrollError(f"account email {email!r} is not usable as a directory name")
                    os.rename(tmp, dest)
                    state["emails"][email] = email
                    print(a.green(f"✓ {email} added"))
                add_event(state, f"account added: {email}")
                save_state(cfg, state)
        try:
            again = input("Add another account? [y/N] ").strip().lower()
        except EOFError:
            again = ""
        if again not in ("y", "yes"):
            break
    print()
    print("Run `ccroll` for the dashboard, or `ccroll adopt` to register the live login.")
    return 0


def cmd_adopt(cfg: Cfg, a: Ansi) -> int:
    state = load_state(cfg)
    live = read_json(cfg.live_path)
    oauth = oauth_of(live)
    if not oauth:
        raise CcrollError(f"no live login found at {cfg.live_path}")
    email = _email_of_creds(oauth)
    if not email:
        raise CcrollError("could not read the live account's email — check network and retry")
    _register(cfg, state, email, live)
    state["active"] = email
    add_event(state, f"adopted live login: {email}")
    save_state(cfg, state)
    print(a.green(f"✓ live login saved as {email} and marked active"))
    return 0


def cmd_switch(cfg: Cfg, a: Ansi, name: str) -> int:
    state = load_state(cfg)
    target = get_account(cfg, name)
    do_swap(cfg, state, target, "manual")
    print(a.green(f"✓ live credentials now {target.name} — running sessions pick this up automatically"))
    if cfg.sync_identity:
        for t, msg in state.get("events", [])[-2:]:
            if msg.startswith("identity "):
                print(a.dim(f"  {msg}"))
    return 0


def cmd_list(cfg: Cfg, a: Ansi) -> int:
    state = load_state(cfg)
    accounts = list_accounts(cfg)
    if not accounts:
        print("no accounts — add with `ccroll add`")
        return 0
    width = max(len(acc.name) for acc in accounts) + 2
    for acc in accounts:
        oauth = oauth_of(acc.read()) or {}
        mark = "► " if state.get("active") == acc.name else "  "
        access, alen = fmt_dur3(a, expires_in_s(oauth))
        rms = oauth.get("refreshTokenExpiresAt")
        refresh, _ = fmt_dur3(a, rms / 1000.0 - now() if isinstance(rms, (int, float)) else None)
        print(f"{mark}{acc.name:<{width}} access ↺" + access
              + " " * max(1, 13 - alen) + "refresh ↺" + refresh)
    return 0


# --- entry ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv) or ["watch"]
    p = argparse.ArgumentParser(prog="ccroll", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"ccroll {CCROLL_VERSION}")
    p.add_argument("--root", help="account store dir (default ~/.claude-accounts)")
    p.add_argument("--claude-dir", help="live Claude config dir (default $CLAUDE_CONFIG_DIR or ~/.claude)")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--by", choices=("scoped", "weekly"), default="scoped",
                   help="which weekly limit governs rotation and next-account choice: "
                        "'scoped' = the per-model weekly limit (Fable on current Max plans), "
                        "'weekly' = the all-models weekly limit (default: scoped)")
    sub = p.add_subparsers(dest="cmd")

    w = sub.add_parser("watch", help="live dashboard + auto-rotation (default)")
    w.add_argument("--threshold", type=float, default=95, help="rotate when session %% reaches this (default 95)")
    w.add_argument("--scoped-threshold", type=float, default=97,
                   help="rotate when the per-model weekly %% reaches this (default 97)")
    w.add_argument("--interval", type=int, default=60, help="active-account poll seconds (default 60)")
    w.add_argument("--lead", type=float, default=None, metavar="SECONDS",
                   help="rotate early once the active account's predicted time to any limit at its "
                        "current burn drops under this (default 60, or one poll interval if longer); the static "
                        "thresholds remain the latest point")
    w.add_argument("--scan", type=int, default=300, help="all-accounts scan seconds (default 300)")
    w.add_argument("--cooldown", type=int, default=0,
                   help="min seconds between swaps (default 0: none needed, a swap "
                        "requires the source to be spent and the target not to be)")
    w.add_argument("--no-rotate", action="store_true", help="dashboard only, never swap")
    w.add_argument("--no-preempt", action="store_true",
                   help="only rotate when the active account is spent; never move early "
                        "to the account whose weekly window resets next")
    w.add_argument("--preempt-runway", type=float, default=3.0, metavar="HOURS",
                   help="move early only while the active account's estimated time to any "
                        "limit exceeds this (default 3; keeps pre-emption off when sessions bind)")
    w.add_argument("--touch", action="store_true",
                   help="also open freshly reset weekly windows at once (one request there, "
                        "then move on); worth it only when a swap costs little quota")
    w.add_argument("--grace", type=float, default=300, metavar="SECONDS",
                   help="after a swap, ignore the burn and never rotate early or pre-empt for "
                        "this long while every agent re-primes its context (default 300); "
                        "the static thresholds still apply")
    w.add_argument("--preempt-max-cost", type=float, default=5.0, metavar="PERCENT",
                   help="skip pre-emption when the measured swap cost (preload) on the governing "
                        "weekly window exceeds this (default 5)")
    w.add_argument("--sync-identity", action="store_true", help="also point Claude Code's displayed identity (the `oauthAccount` block in its global config) at the account swapped to, so /status stops naming the previous one; auth already follows the swap without this. Off by default: running sessions cache that config in memory, so the correction usually shows up only in newly started sessions, and a session that rewrites the file from memory undoes it")

    sub.add_parser("status", help="one-shot usage table for all accounts")
    sub.add_parser("add", help="log account(s) in interactively; each is named by its own email",
                   aliases=["login"])
    sub.add_parser("adopt", help="save the current live login into the store under its email")
    sp = sub.add_parser("switch", help="hot-swap the live credentials now")
    sp.add_argument("name", help="account email (a unique prefix is enough)")
    sp.add_argument("--sync-identity", action="store_true", help="also point Claude Code's displayed identity (the `oauthAccount` block in its global config) at the account swapped to, so /status stops naming the previous one; auth already follows the swap without this. Off by default: running sessions cache that config in memory, so the correction usually shows up only in newly started sessions, and a session that rewrites the file from memory undoes it")
    sub.add_parser("list", help="accounts and token expiries")

    args = p.parse_args(argv)
    cfg = Cfg(args)
    a = Ansi(enabled=sys.stdout.isatty() and not args.no_color)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        cmd = args.cmd or "watch"
        if cmd == "watch":
            return cmd_watch(cfg, a)
        if cmd == "status":
            return cmd_status(cfg, a)
        if cmd in ("add", "login"):
            return cmd_add(cfg, a)
        if cmd == "adopt":
            return cmd_adopt(cfg, a)
        if cmd == "switch":
            return cmd_switch(cfg, a, args.name)
        if cmd == "list":
            return cmd_list(cfg, a)
        p.error(f"unknown command {cmd!r}")
    except CcrollError as e:
        print(a.red(f"error: {e}"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
