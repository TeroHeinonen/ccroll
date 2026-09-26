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
otherwise expire soonest when the active one approaches a limit.  With
--peak-hold, a daily clock range (the peak hours, 05:00-11:00
America/Los_Angeles) is sat out by parking on an account that is already
refusing requests, so every session waits out a usage limit as it would
anyway, and rotating on at the end.

Stdlib only.  Linux (and any platform where Claude Code keeps credentials in
a plain file rather than a keychain).

Usage:
    ccroll add                   # interactive login(s); each account is named by its email
    ccroll adopt                 # register the current ~/.claude login under its email
    ccroll                       # dashboard + auto-rotation (same as `watch`)
    ccroll status                # one-shot table
    ccroll switch ops@x.com      # manual hot-swap now (a unique prefix works: `switch ops`)
    ccroll list                  # accounts and token expiries
    ccroll client --master HOST  # on another machine: let HOST's ccroll rotate this one
    ccroll release HOST          # free the account an offline client host holds
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
import shlex
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zoneinfo
from datetime import datetime, timedelta, timezone

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
ROLLED_MARK = "rolled"          # reset column for a window whose reset has passed
RESCAN_MIN_S = 30               # floor on any scheduled rescan, so a stale reset time
                                # can never turn the watch loop into a scan storm
USAGE_ERROR_MSG_CHARS = 26      # how much of a 429's own message the status column carries
FRESH_PERISH_RATE = 97 / 168    # %/h an unopened weekly window "loses" by not starting
                                # its 7-day clock: one full window per week
TOUCH_REASON = "opens a fresh"  # prefix of the reason a --touch move carries
REFRESH_MARGIN_S = 180          # refresh an access token this close to expiry
SWAP_VERIFY_DELAY_S = 2.0       # re-check the live file this long after a swap
SAMPLE_RETENTION_S = 24 * 3600  # keep at most a day of burn-rate samples
BURN_WINDOW_S = 45 * 60         # fit burn rate over the last 45 minutes
BURN_MIN_SAMPLES = 3
BURN_MIN_SPAN_S = 90            # 3 polls at the 60s default: a figure after ~2 min
BURN_SETTLED_SPAN_S = 8 * 60    # shorter fits are shown dimmed as provisional
BURN_MIN_RATE = 0.05            # %/h below this shows as idle
USAGE_PCT_STEP = 1.0            # the usage endpoint reports whole percents: a swap cost
                                # measured as 0 is below this step, not zero
SESSION_WINDOW_H = 5.0          # the 5-hour session window, for the fleet forecast
WEEK_H = 168.0
PRELOAD_KEEP = 5                # preload measurements kept for the median
PRELOAD_MEASURE_MAX_S = 300     # after the grace: measure raw if no fit appears within this
EARLY_MIN_PCT = 50              # burn-based early rotation only from here up: below it an
                                # ETA under the lead would need >1000%/h, which is noise
                                # (the post-swap re-prime spike), not a sustained rate
FULL_PCT = 99.5                 # a window the endpoint reports as 100%: the account is
                                # actually being refused there, not merely past a threshold
PEAK_HOLD_EXAMPLE = "05:00-11:00"   # the peak range --peak-hold is meant for (off unless given)
PEAK_TZ_DEFAULT = "America/Los_Angeles"
SIGNAL_DIRNAME = "account-switch"   # under the live Claude config dir
SIGNAL_EVENTS_FILE = "events.jsonl"
SIGNAL_STATE_FILE = "state.json"
SIGNAL_EXPECT_LEAD_S = 180      # announce a switch about this long before it
SIGNAL_ETA_DRIFT_S = 180        # re-announce only when the ETA moves this much
SIGNAL_MARKS = (75, 90)         # the only utilisation crossings that are announced
SIGNAL_TAIL_BYTES = 65536       # how much of events.jsonl to scan for the highest seq
SETTINGS_FILE = "settings.json"

# master–client: one master keeps the store and decides every rotation; each
# client executes the swaps on its own machine.  Newline-delimited JSON over a
# Unix socket in the store, reached from other machines through `ssh HOST
# ccroll relay`, so SSH does the authentication and the encryption.
PROTO_V = 1
MAX_MSG_BYTES = 1 << 20             # protocol sanity bound: the largest message is a
                                    # frame or a set of credentials, far below this
MASTER_SOCK = "master.sock"         # under <root>/.ccroll
CLIENT_STATE_FILE = "client.json"   # a client's own small state, under <root>/.ccroll
CLIENT_CHROME_LINES = 4             # lines the client wraps around the frame it is sent

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


def fmt_age(seconds: float) -> str:
    """A short age: 40s, 12m, 3h05m, 2d04h."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86400}d{s % 86400 // 3600:02d}h"


def fmt_clock(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


def parse_clock_range(text: str) -> tuple[int, int]:
    """'05:00-11:00' -> minutes after local midnight for start and end.  An
    end at or before the start means the range crosses midnight."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not m:
        raise CcrollError(f"--peak-hold wants HH:MM-HH:MM, got {text!r}")
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if not (h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60):
        raise CcrollError(f"--peak-hold has an impossible clock time: {text!r}")
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if start == end:
        raise CcrollError("--peak-hold cannot be empty")
    return start, end


def peak_window(cfg: "Cfg", t: float | None = None) -> tuple[float, float] | None:
    """The peak-hour range in force at `t`, or the next one to come: (start,
    end) as epoch seconds.  Computed on the wall clock of --peak-tz, so it
    lands on the same local time either side of a DST change."""
    if not cfg.peak:
        return None
    t = now() if t is None else t
    start_m, end_m = cfg.peak
    if end_m <= start_m:
        end_m += 24 * 60                  # crosses midnight
    midnight = datetime.fromtimestamp(t, cfg.peak_tz).replace(hour=0, minute=0,
                                                              second=0, microsecond=0)
    for days in (-1, 0, 1):
        base = midnight + timedelta(days=days)    # wall-clock arithmetic: tz re-resolved
        start = (base + timedelta(minutes=start_m)).timestamp()
        end = (base + timedelta(minutes=end_m)).timestamp()
        if end > t:
            return start, end
    return None                           # unreachable: the +1 day window always lies ahead


def fmt_local(cfg: "Cfg", t: float) -> str:
    """'11:00 PDT' — a moment on the wall clock the hold is defined on."""
    return datetime.fromtimestamp(t, cfg.peak_tz).strftime("%H:%M %Z")


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
    """Return (status:int|None, body_bytes:bytes, neterr:str|None, headers)."""
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
            return resp.status, resp.read(), None, resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), None, e.headers
    except Exception as e:
        return None, b"", str(getattr(e, "reason", e)), None


def retry_after_s(headers) -> float | None:
    """Seconds the server asked us to wait, from a Retry-After header given
    either as a delay or as an HTTP date.  None when there is no such ask."""
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return max(0.0, parsedate_to_datetime(raw).timestamp() - now())
    except (TypeError, ValueError):
        return None


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
        self.threshold = float(getattr(args, "threshold", 99))
        self.scoped_threshold = float(getattr(args, "scoped_threshold", 97))
        self.interval = max(15, int(getattr(args, "interval", 60)))
        self.scan = max(self.interval, int(getattr(args, "scan", 600)))
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
        # account-switch signals: a small append-only feed that every other
        # Claude Code session on this machine can tail, so a session can avoid
        # spawning expensive work moments before a swap and relaunch cheaply
        # after one.  ccroll is the only writer; sessions only read.
        self.signal = not getattr(args, "no_signal", False)
        self.signal_dir = os.path.expanduser(
            getattr(args, "signal_dir", None) or os.path.join(self.live_dir, SIGNAL_DIRNAME))
        self.settings_path = os.path.join(self.live_dir, SETTINGS_FILE)
        # peak-hour hold: a daily clock range during which the fleet sits on
        # an account that is already refusing requests, so the running
        # sessions wait out "a usage limit" exactly as they do for any other
        # spent account, and resume by an ordinary rotation at its end.
        self.peak = None
        self.peak_tz = None
        if getattr(args, "peak_hold", None):
            self.peak = parse_clock_range(args.peak_hold)
            tz = getattr(args, "peak_tz", None) or PEAK_TZ_DEFAULT
            try:
                self.peak_tz = zoneinfo.ZoneInfo(tz)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError) as e:
                raise CcrollError(f"unknown time zone {tz!r} for --peak-tz: {e}")
        # which weekly limit governs exhaustion + next-account choice:
        # "scoped" = the per-model weekly limit (Fable on current Max plans),
        # "weekly" = the all-models weekly limit.
        self.mode = getattr(args, "by", None) or "weekly"


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
        status, data, neterr, _ = _http("POST", TOKEN_URL, None, body)
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
        self.retry_after = None     # seconds a 429 asked us to wait, when it said
        self.projected_from = None  # {session, weekly, scoped}: the raw reading, when projected
        self.projected_rate = None  # {key: %/h} the projection advanced each window by
        self.projected_age = None   # seconds between that reading and the projection
        self.fetched_at = now()

    def windows(self):
        return {"session": self.session_pct, "weekly": self.weekly_pct, "scoped": self.scoped_pct}


def fetch_usage(token: str) -> Usage:
    u = Usage()
    status, data, neterr, headers = _http("GET", API_BASE + USAGE_PATH, token, None)
    if neterr:
        u.error = f"network: {neterr}"
        return u
    try:
        j = json.loads(data.decode() or "{}")
    except json.JSONDecodeError:
        j = {}
    if not isinstance(j, dict) or j.get("type") == "error" or isinstance(j.get("error"), dict):
        err = j.get("error") if isinstance(j.get("error"), dict) else {}
        etype = err.get("type")
        if status == 401 or etype == "authentication_error":
            u.error = "auth"
        elif etype == "permission_error":
            u.error = "token lacks user:profile scope — re-login interactively"
        elif status == 429 or etype == "rate_limit_error":
            # Keep what the server said: which limiter answered is the one
            # thing that tells a throttle we caused from one we are caught in.
            msg = " ".join(str(err.get("message") or "").split())
            u.error = "rate_limit_error" + (f" · {msg[:USAGE_ERROR_MSG_CHARS]}" if msg else "")
            u.retry_after = retry_after_s(headers)
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


_usage_hold: dict[str, float] = {}   # cred_path -> when a Retry-After lets us ask again


def _rate_limited(err: str | None) -> bool:
    return bool(err) and ("rate_limit" in err or "429" in err)


def held_token(cred_path: str) -> str:
    """The stored access token as it is, never refreshed: for an account a
    client machine is live on.  Its refresh token belongs to that machine's
    Claude Code while it holds the account — a refresh here would rotate it
    out from under the running session."""
    oauth = oauth_of(read_json(cred_path))
    if not oauth:
        raise CcrollError(f"no credentials at {cred_path}")
    left = expires_in_s(oauth)
    if left is not None and left <= 0:
        raise CcrollError("access token expired — waiting for the holder's refresh")
    return oauth["accessToken"]


def usage_for(cred_path: str, refresh: bool = True) -> Usage:
    """Fetch usage for stored credentials, refreshing the token when needed
    (including one retry when a supposedly-valid token turns out revoked).
    With refresh=False the stored token is used as it is and never renewed:
    the account is live on a client, whose own Claude Code owns the refresh.

    A 429 from the usage endpoint is not a penalty this poll earned.  The
    endpoint's per-account limiter is shared with the running CLI, which
    fetches usage itself whenever its requests are being held at a limit —
    exactly the minutes an account is spent or burning hard.  The samples
    show it: mrsmith@ answered every other read with a 429 for the two hours
    it sat at 100%, and aws@ read cleanly once a minute for twenty minutes
    and then failed the moment its agents were being held, with nothing
    changed on this side.  Backing off only blinds ccroll for the minutes it
    most needs to see, and the doubling retry it replaced kept the same
    accounts just as dark.  So a throttled account is asked again at the
    normal cadence — never faster, and that cadence is what the endpoint
    accepts at idle — unless the 429 carried a Retry-After, which is
    honoured to the second."""
    until = _usage_hold.get(cred_path, 0.0)
    if now() < until:
        u = Usage()
        u.error = f"rate_limit_error · server asked to wait {until - now():.0f}s"
        return u
    try:
        token = fresh_token(cred_path) if refresh else held_token(cred_path)
    except CcrollError as e:
        u = Usage(); u.error = str(e); return u
    u = fetch_usage(token)
    if u.error == "auth" and not refresh:
        u.error = "auth — waiting for the holder's refresh"
    elif u.error == "auth":
        try:
            creds = read_json(cred_path)
            new = oauth_refresh(oauth_of(creds) or {})
            creds["claudeAiOauth"] = new
            write_json_atomic(cred_path, creds)
            u = fetch_usage(new["accessToken"])
        except CcrollError as e:
            u = Usage(); u.error = str(e)
    if u.retry_after:
        _usage_hold[cred_path] = now() + u.retry_after
    else:
        _usage_hold.pop(cred_path, None)
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
    status, data, neterr, _ = _http("GET", API_BASE + PROFILE_PATH, token, None)
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
    state.setdefault("touch_pending", False)   # a --touch happened; no more until a natural swap
    state.setdefault("preload", [])
    state.setdefault("signal_labels", {})
    state.setdefault("signal_crossed", [])
    state.setdefault("peak_hold", None)       # start of the peak range being held out
    state.setdefault("peak_released", None)   # start of the range [r] ended early
    state.setdefault("lanes", {})             # client host -> that lane's own keys
    return state


def save_state(cfg: Cfg, state: "dict | Lane") -> None:
    write_json_atomic(cfg.state_path, state.root if isinstance(state, Lane) else state)


def add_event(state: "dict | Lane", msg: str) -> None:
    """Log a line.  Entries stay [time, text], the shape every ccroll
    version reads; a client lane's line is tagged with its host in the
    separate `event_hosts` map (keyed by the entry's time), so a fleet reads
    as one log with each line saying where it happened, and an older ccroll
    can still open the state."""
    t = now()
    state["events"] = (state["events"] + [[t, msg]])[-100:]
    if isinstance(state, Lane) and state.data is not state.root:
        kept = {repr(e[0]) for e in state["events"]}
        tags = {k: v for k, v in (state.get("event_hosts") or {}).items() if k in kept}
        tags[repr(t)] = state.host
        state["event_hosts"] = tags


def event_host(state: dict, entry: list) -> str | None:
    """The client host an event happened on; None for this machine's."""
    if len(entry) > 2:                    # written by a development build
        return entry[2]
    return (state.get("event_hosts") or {}).get(repr(entry[0]))


# --- lanes ----------------------------------------------------------------------
# A lane is one live credentials file that rotation drives: the master's own
# (~/.claude on this machine) or a client machine's.  Everything that belongs
# to "the active account" — which it is, its grace, its swap cost, its hold,
# what its signal feed has been told — is per lane; the burn samples, the
# event log, the account names and the feed labels are the fleet's.  The
# master's own lane keeps its keys at the top level of state.json, exactly
# where a single-machine ccroll has always kept them.
LANE_SHARED = frozenset(("samples", "events", "event_hosts", "emails", "signal_labels", "lanes"))


def new_lane_data() -> dict:
    return {"active": None, "active_since": 0, "last_swap": 0, "grace_until": 0,
            "touch_pending": False, "preload": [], "signal_crossed": [],
            "peak_hold": None, "peak_released": None, "pending": None,
            "bare": False, "offline_since": None, "signal": True}


class Lane:
    """The state as one lane sees it: its own keys from `data`, the fleet's
    from `root`.  Every rotation rule takes this where it took the state, so
    the same code drives every lane.  `sink`, when set, receives the lane's
    account-switch signals instead of this machine's feed: a client's
    sessions tail the feed on the client."""

    def __init__(self, root: dict, data: dict, host: str, sink=None):
        self.root, self.data, self.host, self.sink = root, data, host, sink

    def _d(self, key):
        return self.root if key in LANE_SHARED else self.data

    def get(self, key, default=None):
        return self._d(key).get(key, default)

    def __getitem__(self, key):
        return self._d(key)[key]

    def __setitem__(self, key, value):
        self._d(key)[key] = value

    def __contains__(self, key):
        return key in self._d(key)

    def setdefault(self, key, default=None):
        return self._d(key).setdefault(key, default)

    def pop(self, key, *default):
        return self._d(key).pop(key, *default)


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
    return patch_identity(cfg, target.name, stored_identity(cfg, target.name))


def patch_identity(cfg: Cfg, name: str, ident: dict | None) -> str | None:
    """swap_identity with the identity given: a client has no store and is
    sent the account's `oauthAccount` along with its credentials."""
    if not ident:
        return (f"identity left as-is: no stored identity for {name} — "
                f"re-run `ccroll add` for it")
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
        return f"identity synced to {name}"
    return "identity left as-is: live config is being written by another session"


def stored_identity(cfg: Cfg, email: str) -> dict | None:
    """The account's own `oauthAccount`, as kept beside its credentials."""
    ident = (read_json(os.path.join(cfg.root, email, CONFIG_FILE)) or {}).get("oauthAccount")
    return ident if isinstance(ident, dict) and ident else None


def store_identity(cfg: Cfg, email: str, ident: dict | None) -> str | None:
    """Keep an account's `oauthAccount` next to its credentials.

    `--sync-identity` copies exactly this object into the live config on each
    swap, so an account registered without it can never have its displayed
    identity corrected.  A fresh login writes one into its profile; this
    carries it over.  Only that key is written — anything else the stored
    config holds is preserved.

    Returns a warning for the caller to print, or None on success."""
    if not isinstance(ident, dict) or not ident:
        return ("no identity in this login profile — `/status` will keep naming "
                "the previous account until it is re-added")
    blob = read_json(os.path.join(cfg.root, email, CONFIG_FILE))
    blob = dict(blob) if isinstance(blob, dict) else {}
    if blob.get("oauthAccount") == ident:
        return None
    blob["oauthAccount"] = ident
    write_json_atomic(os.path.join(cfg.root, email, CONFIG_FILE), blob)
    return None


def harvest(cfg: Cfg, state: dict) -> None:
    """Copy the live credentials back into the active account's store dir.
    Refresh tokens rotate, so the store must always hold the newest one."""
    name = state.get("active")
    live = read_json(cfg.live_path)
    if not name or not oauth_of(live):
        return
    write_json_atomic(os.path.join(cfg.root, name, CRED_FILE), live)


def prepare_creds(cfg: Cfg, target: Account) -> dict:
    """The target's stored credentials, refreshed first when they are about
    to expire.  Only ever called for an account no lane is on: its refresh
    token is the store's to rotate."""
    creds = target.read()
    oauth = oauth_of(creds)
    if not oauth:
        raise CcrollError(f"account {target.name!r} has no credentials")
    left = expires_in_s(oauth)
    if left is None or left < REFRESH_MARGIN_S:
        creds["claudeAiOauth"] = oauth_refresh(oauth)
        write_json_atomic(target.cred_path, creds)
    return creds


def commit_swap(cfg: Cfg, state: "dict | Lane", name: str, reason: str,
                snapshot: "Usage | None" = None, preemptive: bool = False) -> str:
    """Record that the lane's live credentials are now `name`'s, once they
    are: the bookkeeping every swap does wherever it was carried out, and the
    switch_done signal.  Returns the detail line ("left <prev>: <reason>")."""
    prev = state.get("active")
    state["active"] = name
    state["last_swap"] = now()
    state["active_since"] = state["last_swap"]
    state["grace_until"] = state["last_swap"] + cfg.grace
    if not preemptive:
        state["touch_pending"] = False          # a natural swap starts a new cycle
    elif reason.startswith(TOUCH_REASON):
        state["touch_pending"] = True           # one touch per cycle
    reset_samples(state, name)
    state["swap_snapshot"] = ({"name": name, "t": state["last_swap"],
                               "session": snapshot.session_pct, "weekly": snapshot.weekly_pct,
                               "scoped": snapshot.scoped_pct}
                              if snapshot and not snapshot.error else None)
    signal_after_swap(state)
    detail = f"left {prev}: {reason}" if prev and prev != name else reason
    add_event(state, f"→ {name} ({detail})")
    with signal_guard(state, "switch_done"):
        signal_switch_done(cfg, state, name,
                           snapshot.session_reset if snapshot and not snapshot.error else None)
    return detail


def reassert_swap(cfg: Cfg, creds: dict, old: dict) -> bool:
    """Guard against the one narrow race: the running CLI finishing a token
    refresh of the OLD account and writing it back over our swap.  Returns
    True when the swap had to be written again."""
    time.sleep(SWAP_VERIFY_DELAY_S)
    new = oauth_of(creds) or {}
    seen = oauth_of(read_json(cfg.live_path)) or {}
    if seen.get("accessToken") != new.get("accessToken") and (
        seen.get("refreshToken") == old.get("refreshToken")
        or seen.get("accessToken") == old.get("accessToken")
    ):
        write_json_atomic(cfg.live_path, creds)
        return True
    return False


def do_swap(cfg: Cfg, state: "dict | Lane", target: Account, reason: str,
            snapshot: "Usage | None" = None, preemptive: bool = False, run=None) -> str:
    """Swap the live credentials to `target`. Returns the human-readable detail
    ("left <prev>: <reason>") so callers can show the same text in notices.
    `snapshot` is the target's usage as last read: the preload is measured
    against it at the end of the grace period.  `preemptive` marks a move made
    while the previous account still had headroom; a natural (exhaustion or
    manual) swap ends the current cycle and re-arms --touch.  `run(fn, *args)`
    carries out the two slow steps — a token refresh and the re-check a
    moment after the write — where the caller wants them (the master keeps
    its links served meanwhile); they touch files, never `state`."""
    run = run or (lambda fn, *args: fn(*args))
    old = oauth_of(read_json(cfg.live_path)) or {}
    harvest(cfg, state)
    creds = run(prepare_creds, cfg, target)
    write_json_atomic(cfg.live_path, creds)
    detail = commit_swap(cfg, state, target.name, reason, snapshot, preemptive)
    if cfg.sync_identity:
        note = swap_identity(cfg, target)
        if note:
            add_event(state, note)
    save_state(cfg, state)
    if run(reassert_swap, cfg, creds, old):
        add_event(state, "re-asserted swap over a concurrent write")
        if cfg.sync_identity:
            swap_identity(cfg, target)        # re-assert the identity too, quietly
        save_state(cfg, state)
    return detail


def client_swap(cfg: Cfg, to: str, creds: dict, ident: dict | None) -> tuple[dict | None, str | None, bool]:
    """A client carrying out the swap the master sent: the same live write
    as `do_swap`, on this machine.  Returns (the credentials that were live —
    the previous account's newest tokens, for the master's store —, the
    identity note, whether the swap had to be re-asserted)."""
    if not oauth_of(creds):
        raise CcrollError(f"the master sent no usable credentials for {to}")
    old_creds = read_json(cfg.live_path)
    old = oauth_of(old_creds) or {}
    write_json_atomic(cfg.live_path, creds)
    # From here the swap has happened: this machine is on `to`, whatever
    # else fails, and the result must say so — a failure reported now would
    # have the master hand `to` to another machine while this one is on it.
    note, again = None, False
    try:
        note = patch_identity(cfg, to, ident) if cfg.sync_identity else None
        again = reassert_swap(cfg, creds, old)
        if again and cfg.sync_identity:
            patch_identity(cfg, to, ident)
    except OSError as e:
        note = f"swapped, but a follow-up step failed: {e.strerror or e}"
    return (old_creds if oauth_of(old_creds) else None), note, again


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
    if out.get("weekly") is not None:
        parts.append(f"weekly +{out['weekly']:.0f}%")
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


# --- fleet forecast -------------------------------------------------------------
def fleet_forecast(state: dict, name: str, u: Usage, cfg: Cfg, n_accounts: int) -> dict | None:
    """Constant-load projection for the whole fleet: if the active account's
    current burn carried on around the clock, what share of the fleet's
    weekly capacity would it consume, handover overhead included.  This is
    the number that says whether the level of parallelism fits the number
    of accounts.  None when there is no burn fit at all.

    Deliberately uses the plain least-squares fit, not the spike-inclusive
    rate the rotation rules act on: this is a steady-state projection, and
    a rate that includes the last burst would overstate it.

    Per weekly window:  work = burn × 168 h;  handover = swaps/week × the
    measured preload on that window;  capacity = accounts × the window's
    rotation threshold;  load = (work + handover) / capacity.  Swaps/week
    follow from the cycle between swaps: (session threshold − session
    preload) / session burn, unless that outlasts the 5-hour window — then
    the session resets first and the cycle is set by the governing weekly
    window instead."""
    fits = {}
    for key in ("session", "weekly", "scoped"):
        f = burn_fit(burn_series(state, name, key))
        if f is not None:
            fits[key] = f
    if not fits:
        return None
    provisional = any(span < BURN_SETTLED_SPAN_S for _, span in fits.values())
    est = preload_estimate(state) or {}
    measured = bool(est) and est.get("session") is not None
    pre = {k: (est.get(k) or 0.0) for k in ("session", "weekly", "scoped")}

    def rate(key):
        r = fits.get(key)
        return None if r is None or r[0] < BURN_MIN_RATE else r[0]

    govern = "weekly" if cfg.mode == "weekly" else "scoped"
    govern_thr = 99.5 if cfg.mode == "weekly" else cfg.scoped_threshold
    cycle_h, kind = None, "idle"
    sr = rate("session")
    if sr is not None:
        cycle_h, kind = max(cfg.threshold - pre["session"], 0.0) / sr, "session"
    if cycle_h is None or cycle_h > SESSION_WINDOW_H:
        gr = rate(govern)
        if gr is not None:
            cycle_h, kind = max(govern_thr - pre[govern], 0.0) / gr, govern
        else:
            cycle_h, kind = None, "idle"
    swaps_wk = (WEEK_H / cycle_h) if cycle_h else 0.0

    windows = []
    for key, thr in ((govern, govern_thr),) + ((("weekly", 99.5),) if govern != "weekly" else ()):
        r = rate(key)
        cap = n_accounts * thr
        if r is None:
            windows.append({"key": key, "rate": None, "capacity": cap})
            continue
        work = r * WEEK_H
        hand = swaps_wk * pre[key] if measured else 0.0
        load = (work + hand) / cap if cap else float("inf")
        windows.append({"key": key, "rate": r, "work": work, "handover": hand, "capacity": cap,
                        "load": load, "sustainable_h": 24.0 / load if load > 1 else 24.0,
                        "blocked": 1 - 1 / load if load > 1 else 0.0})
    g = windows[0]
    share = None
    if g.get("rate") is not None and measured and (g["work"] + g["handover"]) > 0:
        share = g["handover"] / (g["work"] + g["handover"])
    return {"windows": windows, "cycle_h": cycle_h, "cycle_kind": kind,
            "swaps_per_day": swaps_wk / 7, "handover_share": share,
            "preload_measured": measured, "provisional": provisional, "accounts": n_accounts}


def combine_forecasts(fcs: list) -> dict | None:
    """Several lanes' forecasts as one fleet's: each lane burns its own
    account, so the demands add — work and handover on every weekly window,
    and the swaps per day — against the same capacity.  The cycle shown is
    the shortest lane's, the one that swaps most often."""
    fcs = [f for f in fcs if f]
    if len(fcs) <= 1:
        return fcs[0] if fcs else None
    windows = []
    for w0 in fcs[0]["windows"]:
        key, cap = w0["key"], w0["capacity"]
        rated = [w for f in fcs for w in f["windows"] if w["key"] == key and w.get("rate") is not None]
        if not rated:
            windows.append({"key": key, "rate": None, "capacity": cap})
            continue
        work = sum(w["work"] for w in rated)
        hand = sum(w["handover"] for w in rated)
        load = (work + hand) / cap if cap else float("inf")
        windows.append({"key": key, "rate": sum(w["rate"] for w in rated), "work": work,
                        "handover": hand, "capacity": cap, "load": load,
                        "sustainable_h": 24.0 / load if load > 1 else 24.0,
                        "blocked": 1 - 1 / load if load > 1 else 0.0})
    measured = any(f["preload_measured"] for f in fcs)
    g = windows[0]
    share = None
    if g.get("rate") is not None and measured and (g["work"] + g["handover"]) > 0:
        share = g["handover"] / (g["work"] + g["handover"])
    cycles = [(f["cycle_h"], f["cycle_kind"]) for f in fcs if f["cycle_h"]]
    cycle_h, kind = min(cycles) if cycles else (None, "idle")
    return {"windows": windows, "cycle_h": cycle_h, "cycle_kind": kind,
            "swaps_per_day": sum(f["swaps_per_day"] for f in fcs), "handover_share": share,
            "preload_measured": measured, "provisional": any(f["provisional"] for f in fcs),
            "accounts": fcs[0]["accounts"], "lanes": len(fcs)}


def fleet_runway(fc: dict, usages: dict, cfg: Cfg, t: float | None = None) -> dict | None:
    """How long the current burn can go on before the whole fleet is spent:
    the complement of the forecast's rate comparison.  The forecast says
    whether this level of parallelism fits the accounts in steady state;
    the runway says how many hours of it are left right now.

    Per weekly window the demand rate is the forecast's own work plus
    handover, per hour, so the two lines always agree.  Supply starts as
    the headroom that exists now — every readable account's threshold minus
    its effective utilisation, so a rolled-over or unopened window counts
    in full — and grows at each known reset, in time order, by the share
    that account had used: if the demand has not drained the supply by the
    time a reset arrives, that account refreshes and the count goes on;
    the first reset the demand beats is where the fleet runs dry.  Beyond
    the last known reset nothing more is known, so a runway that outlasts
    it is reported as "beyond" that horizon rather than as a number; and
    when the steady-state load is at or under 100% the fleet replenishes
    faster than it drains, which is "sustained".

    Session windows are deliberately left out.  They bind the rate of a
    single account, not the fleet's total: readable accounts times one
    session every five hours is far more than any plausible burn draws, so
    only the weekly windows can run the fleet dry."""
    t = now() if t is None else t
    accts = [(n, u) for n, u in usages.items()
             if n != LIVE_PSEUDO and u and not u.error and u.session_pct is not None]
    if not accts:
        return None
    out = {"windows": {}}
    for w in fc["windows"]:
        if w.get("rate") is None:
            continue
        key = w["key"]
        thr = 99.5 if key == "weekly" else cfg.scoped_threshold
        demand = (w["work"] + w["handover"]) / WEEK_H          # %/h on this window
        supply, resets = 0.0, []
        for _, u in accts:
            pct, reset = (u.weekly_pct, u.weekly_reset) if key == "weekly" else (u.scoped_pct, u.scoped_reset)
            used = effective_pct(pct, reset, t)
            supply += max(0.0, thr - used)
            if reset and reset > t:
                resets.append(((reset - t) / 3600.0, used))
        now_h = supply
        resets.sort()
        res = {"now": now_h, "refreshed": 0.0, "hours": float("inf"), "beyond": False,
               "horizon_h": resets[-1][0] if resets else 0.0, "sustained": False, "demand": demand}
        if demand <= 0:
            res["sustained"] = True
            out["windows"][key] = res
            continue
        exhausted = False
        for r_h, used in resets:
            if demand * r_h >= supply:
                exhausted = True
                break
            supply += used
        res["hours"] = supply / demand
        res["refreshed"] = supply - now_h
        if not exhausted and resets and res["hours"] > res["horizon_h"]:
            res["beyond"] = True
            res["sustained"] = w["load"] <= 1.0
        out["windows"][key] = res
    if not out["windows"]:
        return None
    binds = min(out["windows"], key=lambda k: out["windows"][k]["hours"])
    out.update(out["windows"][binds])
    out["binds"] = binds
    return out


# --- account-switch signals -----------------------------------------------------
# A tiny protocol other Claude Code sessions on this machine tail so they can
# hold off spawning expensive work moments before a swap, and relaunch cheaply
# after one.  ccroll is the only writer; sessions only read.  Two files under
# the live config dir: an append-only `events.jsonl` (never rewritten, never
# truncated) and a `state.json` snapshot replaced atomically.  Three events
# only — no heartbeats, no countdowns: every line costs each reader a turn.
def _utc(t: float | None) -> str | None:
    """The protocol's time format: ISO-8601 UTC, second resolution, literal Z."""
    if t is None:
        return None
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def signal_paths(cfg: Cfg) -> tuple[str, str]:
    return (os.path.join(cfg.signal_dir, SIGNAL_EVENTS_FILE),
            os.path.join(cfg.signal_dir, SIGNAL_STATE_FILE))


def _max_seq_in_events(path: str) -> int:
    """Highest seq already in the log.  seq only ever increases, so the tail
    carries the maximum; the partial line the seek lands in is dropped."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > SIGNAL_TAIL_BYTES:
                fh.seek(-SIGNAL_TAIL_BYTES, os.SEEK_END)
                fh.readline()
            blob = fh.read()
    except OSError:
        return 0
    best = 0
    for line in blob.splitlines():
        try:
            seq = json.loads(line).get("seq")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            continue                      # a malformed line must not stop us
        if isinstance(seq, int) and seq > best:
            best = seq
    return best


def signal_init(cfg: Cfg, state: dict) -> None:
    """Fix the counter at or above every seq anyone has already seen, so a lost
    ccroll state can never rewind the feed for a session that is still tailing."""
    if not cfg.signal:
        return
    events_path, state_path = signal_paths(cfg)
    seen = [int(state.get("signal_seq") or 0), _max_seq_in_events(events_path)]
    snap = read_json(state_path) or {}
    if isinstance(snap.get("seq"), int):
        seen.append(snap["seq"])
    state["signal_seq"] = max(seen)


def signal_label(state: dict, name: str) -> str:
    """A stable opaque label per account (`acct-07`).  The feed is readable by
    every session on the machine, so it never carries an email.  Labels are
    persisted, so they do not shift when accounts are added or removed."""
    labels = state.setdefault("signal_labels", {})
    if name not in labels:
        used = set(labels.values())
        n = 1
        while f"acct-{n:02d}" in used:
            n += 1
        labels[name] = f"acct-{n:02d}"
    return labels[name]


def _signal_check_opaque(fields: dict) -> None:
    """Last line of defence: nothing address-shaped reaches the feed."""
    for key, value in fields.items():
        if isinstance(value, str) and "@" in value:
            raise CcrollError(f"refusing to write {key}={value!r} to the signal feed")


def signal_write(cfg: Cfg, state: dict, event: str, fields: dict,
                 snapshot: dict | None = None) -> None:
    """Append one protocol event, then replace the snapshot atomically."""
    if not cfg.signal:
        return
    _signal_check_opaque(fields)
    if isinstance(state, Lane) and state.sink:
        # a client's lane: its own machine writes the line, with its own seq
        _signal_mirror(state, snapshot or {})
        state.sink({"op": "write", "event": event, "fields": fields, "snapshot": snapshot})
        return
    if "signal_seq" not in state:
        signal_init(cfg, state)
    os.makedirs(cfg.signal_dir, mode=0o700, exist_ok=True)
    seq = int(state.get("signal_seq") or 0) + 1
    state["signal_seq"] = seq
    line = {"seq": seq, "ts": _utc(now()), "event": event}
    line.update(fields)
    events_path, _ = signal_paths(cfg)
    with open(events_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())            # a reader must never miss a written line
    _signal_snapshot(cfg, state, snapshot or {}, seq=seq)


def _signal_mirror(state, updates: dict) -> dict:
    snap = state.setdefault("signal_snapshot",
                            {"account": None, "next_switch_eta_utc": None,
                             "window_resets_utc": None})
    snap.update(updates)
    _signal_check_opaque(snap)
    return snap


def _signal_snapshot(cfg: Cfg, state: dict, updates: dict, seq: int | None = None) -> None:
    """Rewrite `state.json` — the latest view, atomically replaced."""
    if isinstance(state, Lane) and state.sink:
        _signal_mirror(state, updates)
        state.sink({"op": "snapshot", "updates": updates})
        return
    snap = _signal_mirror(state, updates)
    blob = {"seq": seq if seq is not None else int(state.get("signal_seq") or 0),
            "account": snap.get("account"),
            "next_switch_eta_utc": snap.get("next_switch_eta_utc"),
            "window_resets_utc": snap.get("window_resets_utc")}
    os.makedirs(cfg.signal_dir, mode=0o700, exist_ok=True)
    _, state_path = signal_paths(cfg)
    write_json_atomic(state_path, blob)


@contextlib.contextmanager
def signal_guard(state: dict, what: str):
    """A signal must never break or delay a swap: a full disk or a read-only
    home is logged and swallowed."""
    try:
        yield
    except Exception as e:                # noqa: BLE001 — deliberately broad
        with contextlib.suppress(Exception):
            add_event(state, f"signal {what} failed: {e}")


def signal_switch_done(cfg: Cfg, state: dict, name: str, session_reset: float | None) -> None:
    """Sent once, immediately after the new account is live.  The reset is the
    new account's 5-hour session window; null when it has not opened yet."""
    label = signal_label(state, name)
    resets = _utc(session_reset) if session_reset and session_reset > now() else None
    signal_write(cfg, state, "switch_done", {"account": label, "window_resets_utc": resets},
                 snapshot={"account": label, "window_resets_utc": resets,
                           "next_switch_eta_utc": None})


def signal_switch_expected(cfg: Cfg, state: dict, eta_abs: float, usage_pct: int) -> None:
    """Sent once about three minutes ahead; re-sent only if the ETA moves."""
    eta = _utc(eta_abs)
    signal_write(cfg, state, "switch_expected", {"eta_utc": eta, "usage_pct": usage_pct},
                 snapshot={"next_switch_eta_utc": eta})


def signal_usage_threshold(cfg: Cfg, state: dict, usage_pct: int) -> None:
    signal_write(cfg, state, "usage_threshold", {"usage_pct": usage_pct})


def signal_after_swap(state: dict) -> None:
    """A swap resets what has been announced: the next approach signals afresh."""
    state["signal_expect_eta"] = None
    state["signal_crossed"] = []


def rotation_utilisation(u: "Usage | None", cfg: Cfg) -> float | None:
    """The highest effective utilisation across the windows that can trigger
    rotation — the one figure the protocol's `usage_pct` carries."""
    if u is None or u.error:
        return None
    pcts = [(u.session_pct, u.session_reset), (u.weekly_pct, u.weekly_reset)]
    if cfg.mode == "scoped":
        pcts.append((u.scoped_pct, u.scoped_reset))
    live = [effective_pct(p, r) for p, r in pcts if p is not None]
    return max(live) if live else None


def maybe_signal_expected(cfg: Cfg, state: dict, name: str, u: "Usage | None",
                          rotating: bool, scheduled: float | None = None,
                          holding: bool = False) -> None:
    """Announce the coming switch once its predicted moment is about three
    minutes out.  The burn prediction needs an estimate, so that part stays
    quiet through the post-swap grace — and through a peak-hour hold, where
    the account is meant to be spent and no burn-driven swap will follow;
    `scheduled` is a moment ccroll already knows it will swap at (the hold
    starting, re-parking, or ending), which needs no estimate.  Whichever
    comes first is announced.  Stays quiet while rotation is paused."""
    if not cfg.signal or not rotating or not name or name == LIVE_PSEUDO:
        return
    eta_abs, pct = None, None
    if u is not None and not u.error and not in_grace(state) and not holding:
        predicted = predicted_switch_eta(state, name, u, cfg)
        if predicted is not None and predicted[0] <= SIGNAL_EXPECT_LEAD_S:
            eta_abs, pct = now() + predicted[0], predicted[1]
    if scheduled is not None and scheduled - now() <= SIGNAL_EXPECT_LEAD_S and (
            eta_abs is None or scheduled < eta_abs):
        eta_abs, pct = scheduled, rotation_utilisation(u, cfg) or 0.0
    if eta_abs is None:
        return
    last = state.get("signal_expect_eta")
    if last is not None and abs(eta_abs - last) <= SIGNAL_ETA_DRIFT_S:
        return                            # same prediction, already announced
    state["signal_expect_eta"] = eta_abs
    with signal_guard(state, "switch_expected"):
        signal_switch_expected(cfg, state, eta_abs, int(round(pct)))


def maybe_signal_threshold(cfg: Cfg, state: dict, name: str, u: "Usage | None") -> None:
    """Announce crossing 75% and 90%, once each per account.  The protocol's
    event carries only a utilisation, so the figure is the highest across the
    windows that can trigger rotation; emitting one line per window would be
    indistinguishable noise."""
    if not cfg.signal or not name or name == LIVE_PSEUDO:
        return
    pct = rotation_utilisation(u, cfg)
    if pct is None:
        return
    # utilisation falling below a mark means a window reset: let it fire again
    crossed = {m for m in (state.get("signal_crossed") or []) if m <= pct}
    for mark in SIGNAL_MARKS:
        if pct >= mark and mark not in crossed:
            crossed.add(mark)
            with signal_guard(state, "usage_threshold"):
                signal_usage_threshold(cfg, state, int(round(pct)))
    state["signal_crossed"] = sorted(crossed)


def maybe_signal_reset(cfg: Cfg, state: dict, name: str, u: "Usage | None") -> None:
    """A freshly swapped-to account may not have opened its session window
    yet; fill the reset into the snapshot once a poll learns it."""
    if not cfg.signal or u is None or u.error or not name or name == LIVE_PSEUDO:
        return
    snap = state.get("signal_snapshot") or {}
    if snap.get("window_resets_utc") is not None or snap.get("account") is None:
        return
    if not u.session_reset or u.session_reset <= now():
        return
    with signal_guard(state, "snapshot"):
        _signal_snapshot(cfg, state, {"window_resets_utc": _utc(u.session_reset)})


def check_auto_continue(cfg: Cfg) -> str | None:
    """The protocol leans on the CLI waiting out a usage limit rather than
    prompting.  Report the setting; never edit it — it is the user's file."""
    blob = read_json(cfg.settings_path)
    if blob is None:
        return (f"{cfg.settings_path} is missing or unreadable — add "
                '"autoContinueAtUsageLimit": true to it')
    if blob.get("autoContinueAtUsageLimit") is not True:
        return (f'"autoContinueAtUsageLimit" is not true in {cfg.settings_path} — '
                "set it so the CLI waits out a usage limit instead of asking")
    return None


# --- rotation policy ------------------------------------------------------------
def window_rolled(reset: float | None, t: float | None = None) -> bool:
    """Has this window's reset already passed?  Then whatever percentage the
    endpoint still reports belongs to a window that no longer exists.

    This is the single predicate behind both the decisions and the display,
    so the two can never disagree about which windows have rolled."""
    return bool(reset) and reset <= (now() if t is None else t)


def effective_pct(pct: float | None, reset: float | None,
                  t: float | None = None) -> float:
    """The utilisation to act on, which is 0 once the window has rolled over.

    A percentage whose reset time has already passed is stale data: the
    window has reset and the endpoint simply has not caught up (an account
    reported at 100% with an expired reset reads 0% a minute later).  Every
    rule that compares a utilisation against a threshold must go through
    here, or a rolled-over account is judged on a number that no longer
    exists — which is exactly how ops@ was dropped from the candidate pool
    85 seconds after its session window reset, while it was the single most
    perishable account in the fleet.

    The dashboard goes through it too, and marks the result as inferred, so
    what is displayed is what rotation acts on.  The 0 is a deduction rather
    than a reading, but never an unchecked one: `verified_target` re-reads
    whichever account it picks before any swap commits."""
    if pct is None:
        return 0.0
    return 0.0 if window_rolled(reset, t) else pct


def window_spent(pct: float | None, reset: float | None, threshold: float,
                 t: float | None = None) -> bool:
    """Is this window over its threshold *and* still in force?"""
    return effective_pct(pct, reset, t) >= threshold


def pct_text(u: Usage, key: str, pct: float | None) -> str:
    """A window's utilisation as the rotation reason states it.  When the
    figure is a projection, say so and show what it was projected from:
    `≈96% (91% read 6m ago + 80%/h)`."""
    if pct is None:
        return "—"
    raw = (u.projected_from or {}).get(key)
    rate = (u.projected_rate or {}).get(key)
    if raw is None or rate is None or round(pct) == round(raw):
        return f"{pct:.0f}%"
    return (f"≈{pct:.0f}% ({raw:.0f}% read {_fmt_eta_short(u.projected_age or 0)} ago"
            f" + {rate:.0f}%/h)")


def is_exhausted(u: Usage | None, cfg: Cfg, t: float | None = None) -> str | None:
    """Return the reason the account counts as spent, or None.

    A failed read yields None — missing data never rotates.  The watch loop
    handles the one case where that is not enough (the *active* account
    going unreadable) by falling back to its last good snapshot, however
    old, projected forward by its age (`projected_usage`).

    `t` is the moment to judge at (default now): a window whose reset has
    passed by then no longer counts, so the same reading can be asked what
    it will look like once the fleet has recovered."""
    if u is None or u.error:
        return None  # never rotate on missing data
    if (u.status or "").lower() in ("rejected", "exceeded", "blocked"):
        return f"status {u.status}"
    if window_spent(u.session_pct, u.session_reset, cfg.threshold, t):
        return "session " + pct_text(u, "session", u.session_pct)
    if cfg.mode == "scoped" and window_spent(u.scoped_pct, u.scoped_reset, cfg.scoped_threshold, t):
        return (f"{(u.scoped_label or 'scoped').lower()} weekly "
                + pct_text(u, "scoped", u.scoped_pct))
    if window_spent(u.weekly_pct, u.weekly_reset, FULL_PCT, t):
        return "weekly " + pct_text(u, "weekly", u.weekly_pct)
    return None


def blocked_until(u: Usage | None, cfg: Cfg, t: float | None = None) -> float | None:
    """The moment an account that is *refusing requests now* stops doing so,
    or None if it is not refusing.  A window over ccroll's rotation
    threshold is spent for rotation's purposes but still serves requests;
    what holds a session is a window the endpoint reports full (FULL_PCT),
    and it holds until the latest such window resets.  The scoped window
    counts only in scoped mode, as everywhere else: it blocks that model's
    requests, which is what the fleet is being run for.  Judged at `t`, so
    the same reading can be asked whether the account will still be
    refusing a lead time from now."""
    if u is None or u.error:
        return None
    t = now() if t is None else t
    resets = []
    for pct, reset in ((u.session_pct, u.session_reset), (u.weekly_pct, u.weekly_reset)) + (
            ((u.scoped_pct, u.scoped_reset),) if cfg.mode == "scoped" else ()):
        if window_spent(pct, reset, FULL_PCT, t) and reset and reset > t:
            resets.append(reset)
    return max(resets) if resets else None


def parking_target(usages: dict, cfg: Cfg, exclude: str | None,
                   t: float | None = None) -> str | None:
    """The account to park on for a peak-hour hold: any that is refusing
    requests now.  Among several, the one that stays refused longest, so the
    hold needs the fewest re-parks; the name breaks ties so the same fleet
    always resolves the same way."""
    t = now() if t is None else t
    pool = [(blocked_until(u, cfg, t), n) for n, u in usages.items()
            if n != exclude and n != LIVE_PSEUDO and u and not u.error]
    pool = [(-until, n) for until, n in pool if until is not None]
    return min(pool)[1] if pool else None


def verified_parking(cfg: Cfg, usages: dict, active: str | None) -> str | None:
    """parking_target, confirmed by a fresh read: fleet data can be a scan
    old, and an account that has since reset would not hold anyone."""
    skip: set[str] = set()
    for _ in range(MAX_TARGET_TRIES):
        pool = {n: u for n, u in usages.items() if n not in skip}
        target = parking_target(pool, cfg, exclude=active)
        if not target:
            return None
        fresh = usage_for(get_account(cfg, target).cred_path)
        if not fresh.error:
            usages[target] = fresh
            if blocked_until(fresh, cfg, now() + cfg.lead) is not None:
                return target
        skip.add(target)


def rotation_usage(last_good: dict, name: str, u: Usage | None) -> tuple[Usage | None, float | None]:
    """The usage to base the ACTIVE account's rotation decision on.

    A usage read that fails is exactly what a throttled or rate-limited
    account does, and refusing to act on it strands the live session on a
    dead account.  A window's percentage only ever rises until it resets, so
    a recent error-free snapshot is a *lower bound* on where the account is
    now — sound for "this account is spent", never for "it still has room".
    The caller projects it forward by its age (`projected_usage`) before
    judging it; `is_exhausted` ignores any window whose reset has since
    passed.  Returns (usage, age of the snapshot in seconds), age None when
    the current read is good.

    The snapshot stands in for as long as the blackout lasts: there is no
    age past which it stops counting.  Its floor property does not decay —
    a window still in force can only be at or above what was read, and one
    whose reset has passed is dropped per window by the same predicate the
    rules use (`window_rolled`), not by the snapshot's age.  An earlier
    version gave up after a fixed 15 minutes and returned nothing, which
    `is_exhausted` rightly never rotates on: zetor@ was read at 82% weekly,
    its reads failed for an hour while it burned at ~24%/h, and ccroll sat
    on it through 100% with requests hanging.  Dropping the floor could only
    ever make rotation *later* than the data allows; keeping it, projected
    at the measured burn, fires when a real reading would have."""
    if u is not None and not u.error:
        return u, None
    snap = last_good.get(name)
    if snap is None:
        return None, None
    return snap, now() - snap.fetched_at


def projected_usage(state: dict, name: str, u: Usage, t: float | None = None) -> Usage:
    """The active account's reading advanced to `t` at its measured burn.

    Every reading is a lower bound the moment it is taken: the agents keep
    consuming while ccroll waits for the next poll, and for as long as the
    endpoint refuses to answer.  A frozen 91% can never cross a 95%
    threshold, however long the blackout — which is how privacy@ was held at
    "91%" for eleven minutes while it burned through to 100%.  So each window
    is advanced by rate × age, using the same conservative rate the burn
    rules act on, and capped at 100.  The age is unbounded: the rate is the
    one measured before the blackout (the fit is anchored on the newest
    sample, which a failed read does not add), and a window keeps rising at
    it until its reset.  Windows without a burn estimate, or whose reset has
    passed by `t`, are left as read — the first stays at its lower bound,
    the second is judged as rolled over (0%) by `effective_pct`, never as
    the stale figure plus a projection; nothing is projected during the
    post-swap grace, when no rate exists.  The copy records the raw reading,
    the rate and the age so a reason or a dashboard line can show its
    working."""
    t = now() if t is None else t
    age = max(0.0, t - u.fetched_at)
    out = copy.copy(u)
    if u.error or not name or name == LIVE_PSEUDO or in_grace(state, t):
        return out
    raw, rates, moved = {}, {}, False
    for key, pct, reset in (("session", u.session_pct, u.session_reset),
                            ("weekly", u.weekly_pct, u.weekly_reset),
                            ("scoped", u.scoped_pct, u.scoped_reset)):
        raw[key] = pct
        if pct is None or window_rolled(reset, t):
            continue
        rate = active_burn(state, name, key)
        if rate is None or rate < BURN_MIN_RATE:
            continue
        rates[key] = rate
        setattr(out, f"{key}_pct", min(100.0, pct + rate * age / 3600.0))
        moved = True
    if moved:
        out.projected_from = raw
        out.projected_rate = rates
        out.projected_age = age
    return out


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
    windows = [("session", "session", u.session_pct, u.session_reset),
               ("weekly", "weekly", u.weekly_pct, u.weekly_reset)]
    if cfg.mode == "scoped":
        windows.append(("scoped", (u.scoped_label or "scoped").lower() + " weekly",
                        u.scoped_pct, u.scoped_reset))
    out = []
    for key, label, pct, reset in windows:
        if pct is None:
            continue
        pct = effective_pct(pct, reset)
        rate, _, eta, _ = window_eta(state, name, key, pct)
        out.append((key, label, pct, rate, eta))
    return out


def eta_to_threshold(pct: float | None, rate: float | None, threshold: float) -> float | None:
    """Seconds until a window reaches the value that *triggers rotation*, as
    opposed to `eta_to_limit`, which counts to 100%."""
    if pct is None or rate is None or rate < BURN_MIN_RATE:
        return None
    return max(0.0, threshold - pct) / rate * 3600


def threshold_etas(state: dict, name: str, u: Usage, cfg: Cfg) -> list:
    """(key, label, pct, rate, eta-to-threshold) per rotating window."""
    windows = [("session", "session", u.session_pct, u.session_reset, cfg.threshold),
               ("weekly", "weekly", u.weekly_pct, u.weekly_reset, 99.5)]
    if cfg.mode == "scoped":
        windows.append(("scoped", (u.scoped_label or "scoped").lower() + " weekly",
                        u.scoped_pct, u.scoped_reset, cfg.scoped_threshold))
    out = []
    for key, label, pct, reset, thr in windows:
        if pct is None:
            continue
        pct = effective_pct(pct, reset)
        rate = active_burn(state, name, key)
        out.append((key, label, pct, rate, eta_to_threshold(pct, rate, thr)))
    return out


def predicted_switch_eta(state: dict, name: str, u: Usage, cfg: Cfg):
    """(seconds until ccroll would actually rotate, utilisation of the window
    driving it), or None.  Rotation fires at the static threshold *or*
    `--lead` before 100%, whichever comes first, so the prediction honours
    both — otherwise the signal would disagree with the swap it announces."""
    best = None
    for _, _, pct, rate, eta_thr in threshold_etas(state, name, u, cfg):
        if rate is None:
            continue
        etas = [] if eta_thr is None else [eta_thr]
        if pct >= EARLY_MIN_PCT:
            hard = eta_to_limit(pct, rate)
            if hard is not None:
                etas.append(max(0.0, hard - cfg.lead))
        if not etas:
            continue
        eta = min(etas)
        if best is None or eta < best[0]:
            best = (eta, pct)
    return best


def _fmt_eta_short(secs: float) -> str:
    return "<1m" if secs < 60 else f"{secs / 60:.0f}m"


def about_to_exhaust(state: dict, name: str, u: Usage | None, cfg: Cfg) -> str | None:
    """Burn-based early exhaustion for the active account: the reason it is
    about to hit a limit within cfg.lead seconds at its current burn, or None.
    Only the active account has a burn series; missing data never rotates."""
    if u is None or u.error or not name or name == LIVE_PSEUDO or in_grace(state):
        return None
    for key, label, pct, rate, eta in limit_etas(state, name, u, cfg):
        if pct < EARLY_MIN_PCT:
            continue
        if eta is not None and eta <= cfg.lead:
            return (f"{label} {pct_text(u, key, pct)} · ≈{_fmt_eta_short(eta)} "
                    f"to limit at {rate:.0f}%/h")
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

    Every account is ranked by one number: how fast its unused headroom is
    being lost, in % per hour.  For an open window that is headroom over
    hours until the reset, so the account that resets soonest with the most
    unused quota is drained first and distant resets are kept as reserve.
    A window that is not open has no deadline; what it loses by waiting is
    only the later start of its next week, worth a full window per seven
    days, so it is valued at FRESH_PERISH_RATE.  That puts it below any
    account with real quota about to expire and above a nearly spent one
    with a distant reset — with no special case.  Least loaded is the
    tie-break, and the key ends in the account name so equal usage always
    resolves the same way, whatever order the scan returned."""
    t = now() if t is None else t

    def key(item):
        name, u = item
        pct, reset = governing_window(u, cfg)
        rate = perish_rate(pct, reset, t)
        if rate is None:
            rate = FRESH_PERISH_RATE
        return (-rate, effective_pct(pct, reset, t),
                effective_pct(u.session_pct, u.session_reset, t), name)

    strict, ok = candidates(usages, cfg, exclude, preload, t)
    pool = strict or ok
    return min(pool, key=key)[0] if pool else None


def candidates(usages: dict, cfg: Cfg, exclude: str | None,
               preload: dict | None = None, t: float | None = None) -> tuple[list, list]:
    """(strict, ok): accounts that could take over.  `ok` is anyone not spent;
    `strict` is the subset comfortably clear of every threshold (hysteresis)
    and, when the preload is known, of what the swap itself will cost."""
    ok = [(n, u) for n, u in usages.items()
          if n != exclude and n != LIVE_PSEUDO
          and u and not u.error and u.session_pct is not None
          and not is_exhausted(u, cfg, t)]
    strict = [(n, u) for n, u in ok if comfortable(u, cfg, preload, t)]
    return strict, ok


def comfortable(u: Usage, cfg: Cfg, preload: dict | None = None,
                t: float | None = None) -> bool:
    """Comfortably clear of every threshold — and, once the preload has been
    measured, with enough headroom that the swap itself (every agent
    re-priming its context on the new account) leaves room to work.

    Every figure here is the *effective* utilisation, so a window that has
    rolled over counts as free rather than as whatever the last scan saw.
    Promoting a stale account is safe because `verified_target` re-reads the
    account it picks and rejects it if the fresh read says it is spent."""
    sess = effective_pct(u.session_pct, u.session_reset, t)
    week = effective_pct(u.weekly_pct, u.weekly_reset, t)
    scoped = effective_pct(u.scoped_pct, u.scoped_reset, t)
    if not (sess < cfg.threshold - 15 and week < 95
            and (cfg.mode != "scoped" or scoped < 90)):
        return False
    if preload:
        need_s = preload.get("session")
        if need_s is not None and 100 - sess <= 1.2 * need_s + 5:
            return False
        gov_pct = week if cfg.mode == "weekly" else scoped
        need_g = preload_cost(preload, cfg)
        if need_g is not None and 100 - gov_pct <= 1.2 * need_g + 3:
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
    on it when it resets reopens its next week with no idle gap.  With
    --touch, an account whose window is not open at all is taken first, for
    one request, so its next week starts now rather than hours later.

    Gates: a burn estimate must exist and give the active account more than
    --preempt-runway of headroom (when sessions bind, runway is short and a
    pre-emptive swap would only add a cache re-prime); the active window must
    be open (a touch or reset is still taking effect); the post-swap grace
    must be over; the measured preload on the governing window must not
    exceed --preempt-max-cost; and the move must be worth its cost.

    Worth: a lead of `lead` hours on the reset can rescue at most burn × lead
    percent of headroom — the two accounts differ only in the order they are
    drained, and that order matters only during the interval where one window
    is still open and the other has already reset.  The swap costs the
    measured preload on that window.  So the move is made only when
    burn × lead exceeds the preload by at least the step the endpoint reports
    percentages in (a preload measured as 0 is below 1%, not free).  Both
    sides are measured, so the bar scales with the burn: at 5 %/h a lead
    pays from about twelve minutes, at 0.5 %/h from two hours.

    This is also what keeps the target from flapping.  The endpoint computes
    each reset relative to the read and rounds it to the minute, so two
    windows that truly reset within the same minute read as "one minute
    sooner" in either direction depending on the second they were polled
    at — the old strict comparison ping-ponged between such a pair every
    few minutes.  A lead worth a swap is many minutes at any burn the runway
    gate lets through (it caps the burn at 100 % / --preempt-runway), far
    beyond the rounding, so the order it acts on is stable; and every move
    still goes to a strictly earlier reset, so targets descend and run out.
    The one path that could chain is --touch, since touching a window opens
    it and the next fresh account inherits the target; that is held to one
    touch per natural cycle (state["touch_pending"], cleared by the next
    exhaustion-driven swap), which scales with the cycle instead of the clock."""
    t = now() if t is None else t
    u = usages.get(active)
    if u is None or u.error or active == LIVE_PSEUDO:
        return None
    if in_grace(state, t):
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
    if cfg.touch and not state.get("touch_pending"):
        fresh = [n for n, c in strict if perish_rate(*governing_window(c, cfg), t) is None]
        if fresh:
            return min(fresh), f"{TOUCH_REASON} {label} week"
    opened = [(governing_window(c, cfg)[1], n) for n, c in strict
              if perish_rate(*governing_window(c, cfg), t) is not None]
    if not opened:
        return None
    reset, name = min(opened)
    if reset >= a_reset:
        return None
    lead_h = (a_reset - reset) / 3600.0
    rate = active_burn(state, active, "weekly" if cfg.mode == "weekly" else "scoped")
    gain = (rate or 0.0) * lead_h          # the most headroom the lead can rescue
    if gain <= (cost or 0.0) + USAGE_PCT_STEP:
        return None                      # the lead is not worth a swap at this burn
    return name, (f"{label} resets {fmt_dur(a_reset - reset)} sooner, in {fmt_dur(reset - t)}; "
                  f"up to {gain:.1f}% at stake for a {cost or 0:.0f}% swap")


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


def recovery_moment(u: Usage, cfg: Cfg, t: float) -> float | None:
    """When this spent account frees up: the moment ALL of its binding limits
    have reset (max of its resets).  Only resets still in the future count.
    A reset already in the past has happened, so the moment it names is not
    a moment to wait for — returning it would schedule a rescan in the past
    and spin the watch loop."""
    resets = []
    if window_spent(u.session_pct, u.session_reset, cfg.threshold, t) and u.session_reset:
        resets.append(u.session_reset)
    if cfg.mode == "scoped" and window_spent(u.scoped_pct, u.scoped_reset,
                                             cfg.scoped_threshold, t) and u.scoped_reset:
        resets.append(u.scoped_reset)
    if window_spent(u.weekly_pct, u.weekly_reset, 99.5, t) and u.weekly_reset:
        resets.append(u.weekly_reset)
    resets = [r for r in resets if r > t]
    return max(resets) if resets else None


def earliest_recovery(usages: dict, cfg: Cfg, active: str | None = None,
                      preload: dict | None = None) -> tuple[float, str] | None:
    """When every account is spent: the first moment any of them frees up,
    and the account the fleet will actually be on once it has.

    The fleet recovers at the min of `recovery_moment` across accounts, and
    the watch loop rescans a settle buffer after that.  The account named
    here is the one that rescan will land on — judged by the same rules it
    uses, at the moment it runs — not merely whichever reset ticks first.
    Several windows often reset within the same minute; when a spent session
    and a fresh weekly window free up together, `pick_target` takes the
    fresh weekly one, and the notice must say so or it names an account the
    swap then never goes to.  If the active account itself is clear by then,
    the loop stays put, so it is the one waited for."""
    t = now()
    moments = {}
    for name, u in usages.items():
        if name == LIVE_PSEUDO or u is None or u.error or not is_exhausted(u, cfg):
            continue
        when = recovery_moment(u, cfg, t)
        if when is not None:
            moments[name] = when
    if not moments:
        return None
    first = min(moments.values())
    at = first + RESCAN_MIN_S
    if active in moments and moments[active] <= at:
        who = active
    else:
        who = pick_target(usages, cfg, exclude=active, t=at, preload=preload)
    if who is None or who not in moments:
        who = min(moments, key=lambda n: (moments[n], n))
    return moments[who], who


def merged_view(usages: dict, last_good: dict, state=None) -> dict:
    """Usages for display: an account whose read just failed keeps showing its
    last good numbers, with the error noted, rather than blanking its row.
    The active account is shown as rotation judges it — its last reading
    projected forward by its age — so a blackout shows the figure ccroll is
    acting on, not the one the endpoint last managed to serve.  `state` may
    be a list of lanes: each lane's active account is projected by its own
    burn."""
    view = {}
    lanes = state if isinstance(state, list) else ([state] if state is not None else [])
    owner = {ls.get("active"): ls for ls in lanes if ls.get("active")}
    for name, u in usages.items():
        snap = last_good.get(name)
        shown = u
        # For display a cached reading stands in for as long as there is one:
        # a window's figure only rises until it resets, and a window whose
        # reset has passed is drawn as rolled over anyway, so the reading is
        # never shown as more than it is — and its age is on the row.
        # Rotation follows the same rule for the active account
        # (`rotation_usage`): no age limit, rolled windows dropped.
        if u is not None and u.error and snap is not None:
            shown = copy.copy(snap)
            shown.stale_note = u.error
        if name in owner and shown is not None and not shown.error:
            note = shown.stale_note
            shown = projected_usage(owner[name], name, shown)
            shown.stale_note = note
        view[name] = shown
    return view


# --- rendering ------------------------------------------------------------------
def _pct_cell(a: Ansi, pct, reset, estimated: bool = False):
    """One utilisation cell: the figure ccroll acts on, then its reset.

    A window whose reset has passed shows the inferred 0% and `↺rolled`
    rather than the stale reading the endpoint is still serving — dimmed,
    because it is a deduction awaiting the next read.  That is distinct
    from `↺—`, a window that was never opened and is genuinely idle.
    `estimated`: the figure is a cached reading projected at the measured
    burn through a read blackout, marked `≈` so it never passes for a
    reading."""
    if pct is None:
        return a.dim("—"), 1
    if window_rolled(reset):
        plain = f"{effective_pct(pct, reset):.0f}%"
        return (a.dim(f"{plain:>4}") + a.dim(" ↺") + a.dim(ROLLED_MARK),
                max(len(plain), 4) + 2 + len(ROLLED_MARK))
    plain = ("≈" if estimated else "") + f"{pct:.0f}%"
    dur, dur_len = fmt_dur3(a, (reset - now()) if reset else None)
    return sev_color(a, pct)(f"{plain:>4}") + a.dim(" ↺") + dur, max(len(plain), 4) + 2 + dur_len


def _read_trouble(err: str) -> str:
    """A failed read in a word or two, for a row that shows its cached figures."""
    if _rate_limited(err):
        return "throttled"
    if err.startswith("network"):
        return "network"
    if err.startswith("auth") or "holder's refresh" in err:
        return "awaiting holder's refresh" if "holder" in err else "auth"
    return err if len(err) <= 20 else err[:19] + "…"


def _status_cell(a: Ansi, u: Usage):
    if u.stale_note:
        # the figures on the row are the last good reading: say how old, and
        # why the newer read failed — the numbers are sound, not an error
        msg = f"cached {fmt_age(now() - u.fetched_at)} · {_read_trouble(u.stale_note)}"
        return a.dim(msg), len(msg)
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
    """Rows are (name, usage, is-the-viewer's-live-account) and, in a fleet,
    a fourth (text, width) cell naming the host that is on the account."""
    hosts = any(len(r) > 3 for r in rows)
    headers = ["", "Account", "Session (5h)", "Weekly · all", f"Weekly · {scoped_label}", "Status"]
    if hosts:
        headers.insert(2, "Host")
    cells = [[(a.bold(h), len(h)) for h in headers]]
    for row_in in rows:
        name, u, active = row_in[:3]
        host = row_in[3] if len(row_in) > 3 else ("", 0)
        mark = ("►", 1) if active else ("", 0)
        label = (a.cyan(a.bold(name)) if active else name, len(name))
        if u is None:
            row = [mark, label, (a.dim("…"), 1), ("", 0), ("", 0), (a.dim("fetching"), 8)]
        else:
            def est(key: str) -> bool:
                raw = (u.projected_from or {}).get(key)
                cur = getattr(u, f"{key}_pct")
                return bool(u.stale_note) and raw is not None and cur is not None \
                    and round(cur) != round(raw)
            row = [mark, label,
                   _pct_cell(a, u.session_pct, u.session_reset, est("session")),
                   _pct_cell(a, u.weekly_pct, u.weekly_reset, est("weekly")),
                   _pct_cell(a, u.scoped_pct, u.scoped_reset, est("scoped")),
                   _status_cell(a, u)]
        if hosts:
            row.insert(2, host)
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
        # the same conservative rate and the same utilisation the rotation
        # rules act on, so the ETA shown is the ETA ccroll will move on and a
        # rolled-over window reads as the 0% it is being judged as
        rolled = window_rolled(reset)
        pct = effective_pct(pct, reset)
        rate, fit_rate, eta, provisional = window_eta(state, name, key, pct)
        provisional = provisional or rolled
        reset_in = (reset - now()) if reset else None
        parts = [a.dim(f"{pct:5.1f}%") if rolled else f"{pct:5.1f}%"]
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
        if rolled:
            note = a.dim("↺ rolled over — 0% inferred, awaiting a fresh read")
        elif rate is not None and fit_rate is not None and rate > max(fit_rate, BURN_MIN_RATE) * 1.2:
            note = a.dim(f"(fit {fit_rate:.1f}%/h)")
        tail = "  ".join(x for x in (verdict, note) if x)
        lines.append(f"  {label:<14}" + "  ·  ".join(parts) + ("  " + tail if tail else ""))
    if u.projected_from and u.stale_note:
        bits = []
        for key, label in (("session", "session"), ("weekly", "weekly·all"),
                           ("scoped", f"weekly·{scoped_label.lower()}")):
            raw = u.projected_from.get(key)
            rate = (u.projected_rate or {}).get(key)
            cur = getattr(u, f"{key}_pct")
            if raw is None or rate is None or cur is None or round(cur) == round(raw):
                continue
            bits.append(f"{label} {raw:.0f}% read, ≈{cur:.0f}% projected at {rate:.0f}%/h")
        if bits:
            lines.append(a.yellow(f"  reading is {fmt_dur(u.projected_age)} old — "
                                  + " · ".join(bits)))
    if in_grace(state):
        lines.append(a.dim(f"  post-swap grace: {fmt_dur(state['grace_until'] - now())} left — "
                           "burn estimate and early rotation paused while agents re-prime"))
    est = preload_estimate(state)
    if est:
        bits = []
        # one decimal: the fleet forecast's handover term uses the unrounded
        # median, and "7%" next to a figure computed from 7.4% reads as wrong
        if est.get("session") is not None:
            bits.append(f"session {est['session']:.1f}%")
        if est.get("weekly") is not None:
            bits.append(f"weekly {est['weekly']:.1f}%")
        if est.get("scoped") is not None:
            bits.append(f"{scoped_label.lower()} {est['scoped']:.1f}%")
        lines.append(a.dim("  swap cost ≈ " + " · ".join(bits) + f" (n={est['n']})"))
    return lines


def render_fleet(a: Ansi, state: dict, name: str, u: Usage | None, cfg: Cfg,
                 n_accounts: int, scoped_label: str, usages: dict | None = None) -> list[str]:
    """The fleet forecast block: per weekly window, the load the current burn
    would put on the whole fleet around the clock, and the handover share
    of that demand — the figure that says whether there are too many lanes
    for the accounts.  With the fleet's usages to hand it ends with the
    runway: how long this burn can carry on before every account is spent."""
    if not u or u.error:
        return []
    return render_fleet_fc(a, fleet_forecast(state, name, u, cfg, n_accounts), cfg,
                           n_accounts, scoped_label, usages)


def render_fleet_fc(a: Ansi, fc: dict | None, cfg: Cfg, n_accounts: int, scoped_label: str,
                    usages: dict | None = None, lanes: int = 1) -> list[str]:
    """render_fleet from a forecast already made — one lane's, or several
    lanes' combined (`combine_forecasts`)."""
    head = a.bold(f"fleet · {n_accounts} accounts · if this burn ran around the clock" if lanes <= 1
                  else f"fleet · {n_accounts} accounts · {lanes} lanes · if these burns ran around the clock")
    if fc is None:
        return [head, a.dim("  — no burn estimate yet (a fit needs ~2 min of samples after the grace)")]
    dim = a.dim if fc["provisional"] else (lambda s: s)
    lines = [head]
    for w in fc["windows"]:
        label = f"weekly·{scoped_label.lower()}" if w["key"] == "scoped" else "weekly·all"
        if w.get("rate") is None:
            lines.append(dim(f"  {label:<14}load     —  ·  no burn on this window  ·  capacity {w['capacity']:5.0f}%/wk"))
            continue
        hand = (f"{w['handover']:5.0f}%/wk" if fc["preload_measured"] else "    —/wk")
        if w["load"] > 1:
            verdict = a.red(f"sustainable ≈{w['sustainable_h']:4.1f}h/day")
        else:
            verdict = a.green(f"slack {100 * (1 - w['load']):3.0f}%")
        lines.append(dim(f"  {label:<14}load {100 * w['load']:4.0f}%  ·  work {w['work']:5.0f}%/wk"
                         f" + handover {hand}  ·  capacity {w['capacity']:5.0f}%/wk  ·  ") + verdict)
    if fc["cycle_h"]:
        kind = {"session": "session-limited", "scoped": f"{scoped_label.lower()}-limited",
                "weekly": "weekly-limited"}[fc["cycle_kind"]]
        share = (f"handover {100 * fc['handover_share']:.0f}% of demand" if fc["handover_share"] is not None
                 else "handover share unknown — swap cost not measured yet")
        lines.append(a.dim(f"  {share}  ·  cycle ≈ {fmt_dur(fc['cycle_h'] * 3600)} {kind}"
                           f"  ·  {fc['swaps_per_day']:.1f} swaps/day"))
    else:
        lines.append(a.dim("  burn is idle on every window — no swaps at this rate"))
    if usages is not None:
        rw = fleet_runway(fc, usages, cfg)
        if rw:
            lines.append(render_runway(a, rw, fc, scoped_label))
    return lines


def render_runway(a: Ansi, rw: dict, fc: dict, scoped_label: str) -> str:
    """One line: the fleet runway, which window binds, and what it is made of."""
    wname = f"{scoped_label.lower()}" if rw["binds"] == "scoped" else "weekly·all"
    stock = f"{rw['now']:.0f}% now"
    if rw["refreshed"] > 0:
        stock += f" + {rw['refreshed']:.0f}% refreshed before then"
    tail = "" if fc["preload_measured"] else "  ·  work only, swap cost not measured yet"
    if rw["sustained"]:
        text = f"  runway: sustained at this burn  ·  {wname} load ≤ 100%  ·  {stock}{tail}"
        return a.dim(text) if fc["provisional"] else a.green(text)
    if rw["beyond"]:
        text = (f"  runway > {fmt_dur(rw['horizon_h'] * 3600)} at this burn — beyond the resets we know"
                f"  ·  {wname} binds  ·  {stock}{tail}")
        return a.dim(text) if fc["provisional"] else text
    text = f"  runway ≈ {fmt_dur(rw['hours'] * 3600)} at this burn  ·  {wname} binds  ·  {stock}{tail}"
    if fc["provisional"]:
        return a.dim(text)
    return a.red(text) if rw["hours"] < 24 else text


# --- scanning -------------------------------------------------------------------
def scan_accounts(cfg: Cfg, accounts: list[Account], active: str | None,
                  held: "set | dict | None" = None) -> dict:
    """Fetch usage for every account in parallel.  The active account is read
    through the LIVE credentials file, which the running CLI keeps freshest.
    Accounts in `held` are live on a client machine: read with the token it
    last reported, never refreshed here."""
    held = held or ()
    # A fleet-wide scan can need a token refresh for every account at once.
    # Firing those simultaneously trips the endpoint's rate limiter, so each
    # worker waits out a slot before starting.
    def one(item: tuple[int, Account]) -> tuple[str, Usage]:
        idx, acc = item
        if idx:
            time.sleep(REFRESH_STAGGER_S * idx)
        if acc.name == active:
            return acc.name, usage_for(cfg.live_path)
        return acc.name, usage_for(acc.cred_path, refresh=acc.name not in held)

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


ANSI_RE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def fit_line(line: str, cols: int) -> str:
    """Cut a line to `cols` visible characters, escape codes kept whole and
    colour reset at the cut, so a narrow terminal never wraps the layout."""
    out, seen, i = [], 0, 0
    while i < len(line):
        m = ANSI_RE.match(line, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        if seen >= cols:
            return "".join(out) + "\033[0m"
        out.append(line[i])
        seen += 1
        i += 1
    return "".join(out)


def fit_frame(frame: str, cols: int, rows: int) -> str:
    """A frame cut to a terminal: lines to its width; when it is too short,
    lines dropped from the bottom but the last (the key help) kept."""
    lines = [fit_line(l, max(1, cols)) for l in frame.split("\n")]
    if rows > 1 and len(lines) > rows:
        lines = lines[:rows - 1] + lines[-1:]
    return "\n".join(lines)


def count_sessions(live_dir: str) -> int | None:
    """Running Claude Code processes on this machine that use `live_dir` as
    their config dir (Linux /proc), for the master's host list: sessions on
    another config dir run on another login and are none of this lane's.
    A process's dir is its CLAUDE_CONFIG_DIR, else ~/.claude of its HOME.
    One whose environment cannot be read (another user's) is not counted —
    it cannot be using this user's credentials file.  None where /proc is
    not there to ask."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    want = os.path.realpath(live_dir)
    n = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as fh:
                if fh.read().strip() != "claude":
                    continue
            with open(f"/proc/{pid}/environ", "rb") as fh:
                env = dict(kv.split(b"=", 1) for kv in fh.read().split(b"\0") if b"=" in kv)
        except (OSError, ValueError):
            continue
        conf = env.get(b"CLAUDE_CONFIG_DIR")
        if conf:
            conf = conf.decode(errors="replace")
        else:
            home = env.get(b"HOME")
            conf = os.path.join(home.decode(errors="replace") if home else os.path.expanduser("~"),
                                ".claude")
        if not os.path.isabs(conf):
            with contextlib.suppress(OSError):
                conf = os.path.join(os.readlink(f"/proc/{pid}/cwd"), conf)
        if os.path.realpath(os.path.expanduser(conf)) == want:
            n += 1
    return n


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
                    state["active_since"] = now()
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
def readable_accounts(accounts: list, usages: dict) -> int:
    """How many accounts the fleet forecast may count on: those whose usage
    could be read (falling back to all of them before the first scan)."""
    n = sum(1 for acc in accounts if usages.get(acc.name) and not usages[acc.name].error)
    return n or len(accounts)


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
    held = client_holdings(state)
    usages = scan_accounts(cfg, accounts, state.get("active"), held)
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
        print()
        for line in render_fleet(a, state, active, usages[active], cfg,
                                 readable_accounts(accounts, usages), label, usages=usages):
            print(line)
    pool = {n: u for n, u in usages.items() if n not in held}
    target = pick_target(pool, cfg, exclude=active, preload=preload_estimate(state))
    if target:
        u = usages[target]
        print()
        print(a.green(f"→ best fallback: {target} ({target_reason(u, cfg, label)})"))
    elif accounts:
        recovery = earliest_recovery(pool, cfg, active, preload_estimate(state))
        if recovery:
            when, who = recovery
            print()
            print(a.red(f"→ no account has headroom — {who} is next, in {fmt_dur(when - now())}"))
    for name, host in sorted(held.items()):
        print(a.dim(f"  {name} is live on client {host}"))
    if not accounts:
        print()
        print(a.yellow("No accounts yet — add them with:  ccroll add"))
    return 0


def client_holdings(state: dict) -> dict:
    """account -> client host, for every account a client machine is live on
    or being swapped to, as state.json records it.  Those accounts' refresh
    tokens belong to the client's Claude Code: nothing on the master may
    refresh them, and no other lane may be swapped onto them."""
    out = {}
    for host, lane in sorted((state.get("lanes") or {}).items()):
        for name in (lane.get("active"), (lane.get("pending") or {}).get("to")):
            if name:
                out.setdefault(name, host)
    return out


# --- master socket --------------------------------------------------------------
def master_sock_path(cfg: Cfg) -> str:
    return os.path.join(cfg.root, ".ccroll", MASTER_SOCK)


def _sock_addr(path: str) -> tuple[str, int | None]:
    """An AF_UNIX address for `path`.  Socket addresses are limited to about
    107 bytes; a store deeper than that is reached through an fd of its
    directory.  Returns (address, fd to close once bound or connected)."""
    if len(path.encode()) < 100:
        return path, None
    fd = os.open(os.path.dirname(path), os.O_RDONLY)
    return f"/proc/self/fd/{fd}/{os.path.basename(path)}", fd


def unix_connect(path: str) -> socket.socket:
    addr, fd = _sock_addr(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(addr)
    except OSError:
        s.close()
        raise
    finally:
        if fd is not None:
            os.close(fd)
    return s


def _str(v) -> str | None:
    """A message field that must be a string (or absent)."""
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(f"expected a string, got {type(v).__name__}")
    return v


def _obj(v) -> dict | None:
    """A message field that must be an object (or absent)."""
    if v is None:
        return None
    if not isinstance(v, dict):
        raise ValueError(f"expected an object, got {type(v).__name__}")
    return v


def frame_msg(msg: dict) -> bytes:
    """One protocol line: JSON, the protocol version in every message."""
    return (json.dumps(dict(msg, v=PROTO_V), separators=(",", ":")) + "\n").encode()


def _parse_lines(buf: bytes) -> tuple[list, bytes]:
    msgs = []
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):  # garbled, or nested past any real message
            msg = {"type": "garbled"}
        if isinstance(msg, dict):
            msgs.append(msg)
    return msgs, buf


class Conn:
    """One connection to the master's socket: a client machine's relay, or a
    one-shot command (`ccroll release`)."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.inbuf = b""
        self.outbuf = b""
        self.closing = False          # close once everything queued has been written
        self.lane = None              # its LaneRt once the hello is through
        self.cols, self.rows, self.color = 100, 40, True
        self.interval = 60
        self.sessions = None
        self.heard = now()
        self.last_frame = None

    def send(self, msg: dict) -> None:
        self.outbuf += frame_msg(msg)

    def flush(self) -> bool:
        """Write what the socket takes now; False when the peer is gone."""
        try:
            while self.outbuf:
                n = self.sock.send(self.outbuf)
                self.outbuf = self.outbuf[n:]
        except BlockingIOError:
            pass
        except OSError:
            return False
        return True


class Server:
    """The master's socket, served from the watch loop: one select() over the
    keyboard and every connection, so a key or a message is handled the
    moment it arrives and neither waits on the other."""

    def __init__(self, cfg: Cfg):
        self.path = master_sock_path(cfg)
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        if os.path.exists(self.path):
            try:
                unix_connect(self.path).close()
            except OSError:
                os.unlink(self.path)              # left behind by a master that died
            else:
                raise CcrollError(f"another ccroll master is already serving {cfg.root} "
                                  f"({self.path}) — two masters on one store would race")
        addr, fd = _sock_addr(self.path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        mask = os.umask(0o177)                    # 0600 from the first instant
        try:
            self.sock.bind(addr)
        finally:
            os.umask(mask)
            if fd is not None:
                os.close(fd)
        os.chmod(self.path, 0o600)
        self.sock.listen()
        self.sock.setblocking(False)
        self.conns: list[Conn] = []
        # a worker thread finishing a network read ends the wait at once
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)

    def wake(self) -> None:
        with contextlib.suppress(OSError):
            os.write(self.wake_w, b"x")

    def close(self) -> None:
        for c in list(self.conns):
            c.flush()
            with contextlib.suppress(OSError):
                c.sock.close()
        self.sock.close()
        for fd in (self.wake_r, self.wake_w):
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(self.path)

    def drop(self, c: Conn) -> None:
        if c in self.conns:
            self.conns.remove(c)
        with contextlib.suppress(OSError):
            c.sock.close()

    def wait(self, timeout: float, kb: "Keyboard") -> tuple[str | None, list]:
        """Block up to `timeout` for a key or a message.  Returns (key,
        [(conn, msg)]); a msg of None means that connection has closed."""
        got: list = []
        key = None
        for c in list(self.conns):
            if (c.outbuf and not c.flush()) or (c.closing and not c.outbuf):
                self.drop(c)
                got.append((c, None))
        rl = [self.sock, self.wake_r] + [c.sock for c in self.conns] + ([sys.stdin] if kb.enabled else [])
        wl = [c.sock for c in self.conns if c.outbuf]
        try:
            r, w, _ = select.select(rl, wl, [], timeout)
        except InterruptedError:
            return None, got
        by_sock = {c.sock: c for c in self.conns}
        for s in w:
            c = by_sock.get(s)
            if c is not None and not c.flush():
                self.drop(c)
                got.append((c, None))
        for s in r:
            if s is self.wake_r:
                with contextlib.suppress(OSError):
                    os.read(self.wake_r, 4096)
                continue
            if s is self.sock:
                with contextlib.suppress(OSError):
                    ns, _ = self.sock.accept()
                    ns.setblocking(False)
                    self.conns.append(Conn(ns))
                continue
            if s is sys.stdin:
                key = sys.stdin.read(1)
                continue
            c = by_sock.get(s)
            if c is None or c not in self.conns:
                continue
            try:
                data = s.recv(65536)
            except BlockingIOError:
                continue
            except OSError:
                data = b""
            if not data:
                self.drop(c)
                got.append((c, None))
                continue
            msgs, c.inbuf = _parse_lines(c.inbuf + data)
            got += [(c, m) for m in msgs]
            if len(c.inbuf) > MAX_MSG_BYTES:  # no line end in sight: not our protocol
                self.drop(c)
                got.append((c, {"type": "garbled", "why": "a message larger than any real one"}))
                got.append((c, None))
        return key, got


# --- master: the fleet's rotation engine ----------------------------------------
class LaneRt:
    """A lane at run time: its state view, its policy (the master's, with the
    signal switch of the machine the lane lives on), its banner, and — for a
    client's lane — the link it is reached through."""

    def __init__(self, ls: Lane, cfg: Cfg, local: bool):
        self.ls, self.cfg, self.local = ls, cfg, local
        self.notice = ""
        self.next_poll = 0.0
        self.conn: Conn | None = None
        self.pending_snapshot: Usage | None = None

    @property
    def host(self) -> str:
        return self.ls.host

    def online(self) -> bool:
        return self.local or self.conn is not None


class Master:
    """`ccroll watch`: the dashboard and every rotation decision, for this
    machine's live login and for each client machine linked to it.

    Every lane runs the same rules the single-machine ccroll always ran, on
    the accounts no other lane is on: an account a lane is live on (or being
    swapped to) is out of every other lane's pool, so no two machines are
    ever sent the same account, and each still takes the best one left.
    The store is the master's alone.  A client's account is read with the
    token the client last reported and never refreshed here — its Claude
    Code refreshes it, and reports the rotated tokens back — so a refresh
    token only ever has one owner."""

    def __init__(self, cfg: Cfg, a: Ansi):
        self.cfg, self.a = cfg, a
        self.state = load_state(cfg)
        self.accounts = list_accounts(cfg)
        self.usages: dict[str, Usage] = {}
        self.last_good: dict[str, Usage] = {}   # newest error-free read per account
        self.next_scan = 0.0
        self.paused = not cfg.rotate
        self.host = socket.gethostname()
        self.local = LaneRt(Lane(self.state, self.state, self.host), cfg, local=True)
        self.remote: dict[str, LaneRt] = {}
        for host in list(self.state["lanes"]):
            lr = self.remote_lane(host)
            if lr.ls.get("offline_since") is None:
                lr.ls["offline_since"] = now()    # whatever link it had died with the last master
        self.monitor = LiveMonitor(cfg, self.local.ls)
        self.server: Server | None = None
        self.kb: "Keyboard | None" = None
        self.busy: str | None = None      # the network read under way, while one is
        self.backlog: list = []           # messages that arrived while it was
        self.keys: list = []              # keys pressed while it was
        self.printed = None
        self.tty_out = False

    # --- slow work, with the links kept served ------------------------------
    def background(self, what: str, fn, *args):
        """fn(*args) — a network read or a swap's slow step — on a worker
        thread, while this thread keeps every link served: buffers flushed,
        heartbeats taken, the dashboard and each client's view redrawn with
        `what` on them.  Anything that could change who holds what (a hello,
        a swap result, a rotate request, a key) waits in the backlog until
        the work is done: the worker reads and refreshes accounts no lane is
        on, and that must stay true until it is finished.  State is only
        ever changed on this thread."""
        if self.server is None or self.busy is not None:
            return fn(*args)
        box: dict = {}
        server = self.server

        def work():
            try:
                box["v"] = fn(*args)
            except BaseException as e:    # handed back to the caller's thread
                box["e"] = e
            finally:
                server.wake()

        worker = threading.Thread(target=work, daemon=True)
        self.busy = what
        worker.start()
        try:
            while worker.is_alive():
                self.service_light()
            worker.join()
        finally:
            self.busy = None
        if "e" in box:
            raise box["e"]
        return box.get("v")

    def service_light(self) -> None:
        self.draw()
        self.push_views()
        key, msgs = self.server.wait(1.0, self.kb)
        if key:
            self.keys.append(key)
        for c, msg in msgs:
            lr = c.lane
            if msg is not None and msg.get("type") == "beat" and msg.get("v") == PROTO_V \
                    and lr is not None and lr.conn is c:
                c.heard = now()
                self.take_beat(c, msg)
            else:
                self.backlog.append((c, msg))

    # --- lanes ---------------------------------------------------------------
    def lanes(self) -> list:
        return [self.local] + [self.remote[h] for h in sorted(self.remote)]

    def remote_lane(self, host: str) -> LaneRt:
        data = self.state["lanes"].setdefault(host, new_lane_data())
        for k, v in new_lane_data().items():
            data.setdefault(k, v)
        lcfg = copy.copy(self.cfg)
        lcfg.signal = bool(data.get("signal", True))
        lr = LaneRt(Lane(self.state, data, host), lcfg, local=False)
        lr.ls.sink = lambda op, lr=lr: lr.conn.send(dict(op, type="signal")) if lr.conn else None
        self.remote[host] = lr
        return lr

    def holdings(self, but: "LaneRt | None" = None) -> dict:
        """account -> host for every account a lane other than `but` is live
        on or being swapped to."""
        out = {}
        for lr in self.lanes():
            if lr is but:
                continue
            for name in (lr.ls.get("active"), (lr.ls.get("pending") or {}).get("to")):
                if name and name != LIVE_PSEUDO:
                    out.setdefault(name, lr.host)
        return out

    def pool(self, lr: LaneRt) -> dict:
        """The usages this lane rotates within: every account but those the
        other lanes are on."""
        others = self.holdings(lr)
        return {n: u for n, u in self.usages.items() if n not in others}

    def adopt_reads(self, pool: dict) -> None:
        """Fresh reads a rotation made against a pool keep the dashboard honest."""
        for n, u in pool.items():
            if n in self.usages:
                self.usages[n] = u

    def displaced_by(self, lr: LaneRt) -> str | None:
        """Another host live on this lane's account, when this lane is the
        one to move off it: the later arrival (host name breaks a tie)."""
        active = lr.ls.get("active")
        if not active:
            return None
        mine = (lr.ls.get("active_since") or 0, lr.host)
        for other in self.lanes():
            if other is not lr and other.ls.get("active") == active \
                    and mine > (other.ls.get("active_since") or 0, other.host):
                return other.host
        return None

    def store_creds(self, name: str, creds: dict | None) -> bool:
        """File credentials a client reported for an account it is (or was
        until this very swap) live on.  What a client reports is always the
        newest its machine has: it reads them off its live file as they
        change, and the stream keeps them in order.  The one message that can
        arrive late — a swap result resent after a lost link — is applied
        only while its swap is still pending (`on_swapped`), before the
        account could have been refreshed or handed on here, so nothing can
        roll a refresh token back."""
        oauth = oauth_of(creds)
        if not oauth or name not in {acc.name for acc in list_accounts(self.cfg)}:
            return False
        path = os.path.join(self.cfg.root, name, CRED_FILE)
        if oauth_of(read_json(path)) != oauth:
            write_json_atomic(path, creds)
        return True

    # --- reading usage -------------------------------------------------------
    def remember_good(self, fresh: dict) -> None:
        for name, u in fresh.items():
            if u is not None and not u.error and u.session_pct is not None:
                self.last_good[name] = u

    def rescan(self) -> None:
        cfg = self.cfg
        self.accounts[:] = list_accounts(cfg)
        active = self.local.ls.get("active")
        held = self.holdings(self.local)

        def scan():
            fresh = scan_accounts(cfg, self.accounts, active, held)
            if active is None and oauth_of(read_json(cfg.live_path)):
                fresh[LIVE_PSEUDO] = usage_for(cfg.live_path)
            return fresh

        fresh = self.background(f"scanning {len(self.accounts)} accounts", scan)
        self.usages = fresh
        self.remember_good(fresh)
        record_samples(self.state, fresh)
        save_state(cfg, self.state)
        self.next_scan = now() + cfg.scan
        # The scan read every lane's live account the way its poll would (the
        # live file here, a client's reported token there), so that read *is*
        # the lane's poll: everything a poll does with its reading happens now
        # and the next poll is due one --interval later.  Merely pushing the
        # poll back, as before, meant that with --scan equal to --interval no
        # poll ever ran, and the signals that only a poll sent never went out.
        for lr in self.lanes():
            name = lr.ls.get("active")
            key = name or (LIVE_PSEUDO if lr.local else None)
            if key in fresh:
                self.take_reading(lr, name, fresh[key], sampled=True)
            lr.next_poll = now() + cfg.interval

    def poll(self, lr: LaneRt) -> None:
        """A lane's live account is polled at --interval, never faster, and a
        429 does not slow it down either (see `usage_for`).  What a limit
        needs when it is close is projection (rate × age) and the lead, not
        more reads — and not fewer.  This machine's is read through its live
        file; a client's through the store, with the token it reported."""
        cfg, ls = self.cfg, lr.ls
        lr.next_poll = now() + cfg.interval
        name = ls.get("active")
        if lr.local:
            if not oauth_of(read_json(cfg.live_path)):
                return
            key = name or LIVE_PSEUDO
            u = self.background(f"reading {key}", usage_for, cfg.live_path)
        else:
            if not name:
                return
            key = name
            u = self.background(f"reading {key} ({lr.host})", usage_for,
                                os.path.join(cfg.root, name, CRED_FILE), False)
        if ls.get("active") != name:
            return                        # the lane moved while it was being read
        self.usages[key] = u
        self.remember_good({key: u})
        self.take_reading(lr, name, u)

    def take_reading(self, lr: LaneRt, name: str | None, u: Usage, sampled: bool = False) -> None:
        """What a lane does with a fresh reading of its live account, whether
        its poll or a scan took it: burn sample, swap-cost measurement, and
        the account-switch signals.  `sampled`: the scan recorded the sample."""
        cfg, ls = self.cfg, lr.ls
        if name:
            if not sampled:
                record_samples(self.state, {name: u})
            measure_preload(ls, self.usages, lr.cfg)
            if not lr.online():
                # nobody would receive them: they fire once it links again
                save_state(cfg, self.state)
                return
            maybe_signal_threshold(lr.cfg, ls, name, u)
            maybe_signal_reset(lr.cfg, ls, name, u)
            maybe_signal_expected(lr.cfg, ls, name, u, not self.paused,
                                  scheduled=self.scheduled_swap(lr),
                                  holding=bool(self.hold_window(lr)))
            save_state(cfg, self.state)

    # --- peak-hour hold ------------------------------------------------------
    def hold_window(self, lr: LaneRt) -> tuple[float, float] | None:
        """The peak range to sit out right now, if any: inside the range,
        not released by [r] for this range, and rotation not paused (the
        hold is an automatic swap like any other)."""
        cfg = self.cfg
        if self.paused or not cfg.peak:
            return None
        win = peak_window(cfg)
        if not win or win[0] > now() or lr.ls.get("peak_released") == win[0]:
            return None
        return win

    def scheduled_swap(self, lr: LaneRt) -> float | None:
        """A moment ccroll already knows it will swap this lane at, for the
        switch_expected announcement: the coming hold's start (when there is
        something to park on and the active account is not blocked already),
        or during a hold the earlier of the parked account's release — a lead
        ahead of it, when ccroll re-parks — and the hold's end."""
        cfg, ls = self.cfg, lr.ls
        active = ls.get("active")
        if self.paused or not cfg.peak or not active:
            return None
        win = peak_window(cfg)
        if not win:
            return None
        start, end = win
        until = blocked_until(self.usages.get(active), cfg)
        pool = self.pool(lr)
        if self.hold_window(lr):
            if until is None:
                return None               # the park itself is due on the next tick
            release = until - cfg.lead
            if release < end and not parking_target(pool, cfg, active):
                return None               # nothing to re-park on: no swap to announce
            return min(end, release)
        if start > now() and ls.get("peak_released") != start and until is None \
                and parking_target(pool, cfg, active):
            return start
        return None

    def hold_in_place(self, lr: LaneRt, active: str, u_act: "Usage | None",
                      start: float, end: float) -> None:
        """Peak-hour hold: the desired state is an active account that is
        refusing requests, so every session waits out a usage limit — the
        same thing they do for any spent account — until the hold ends by
        clock or [r].  Not blocked (or about to stop being, within the lead):
        park on one that is.  Missing data never swaps.  Parking is exclusive
        like any swap: two machines on one account would share its refresh
        token."""
        cfg, ls = self.cfg, lr.ls
        if u_act is None:
            return
        if blocked_until(u_act, cfg, now() + cfg.lead) is not None:
            if ls.get("peak_hold") != start:
                ls["peak_hold"] = start
                add_event(ls, f"peak-hour hold: {active} is refusing requests already; "
                              f"staying until {fmt_local(cfg, end)}")
                save_state(cfg, ls)
            self.clear_hold_notice(lr)
            return
        pool = self.pool(lr)
        target = self.background(f"finding an account to park {lr.host} on", verified_parking,
                                 cfg, pool, active)
        self.adopt_reads(pool)
        if not target:
            taken = parking_target(self.usages, cfg, active)
            why = (f"every refusing account is held by another host ({taken} by "
                   f"{self.holdings(lr).get(taken)})" if taken and taken in self.holdings(lr)
                   else "no account is refusing requests")
            lr.notice = (f"peak-hour hold: {why}, so nothing to park on — "
                         f"{active} keeps working until one is")
            return
        self.clear_hold_notice(lr)
        reason = f"peak-hour hold until {fmt_local(cfg, end)}"
        if ls.get("peak_hold") == start:
            reason = f"re-parked, {active} was about to free up · " + reason
        # preemptive: the account left was not spent, so its cycle goes on.
        # The snapshot goes in so switch_done carries the same fields as any
        # swap, and comes straight back out: nothing re-primes on an account
        # that refuses it, so a "preload" measured here would read 0 and skew
        # the median.
        self.swap(lr, target, reason, self.usages.get(target), preemptive=True,
                  parking=start, verb="parked on")

    def clear_hold_notice(self, lr: LaneRt) -> None:
        """Drop a stale "nothing has headroom" banner once that stops being
        true — it outlived its condition and read as a live state."""
        if lr.notice.startswith(("all accounts exhausted", "active account exhausted",
                                 "peak-hour hold:")):
            lr.notice = ""

    # --- rotation ------------------------------------------------------------
    def maybe_rotate(self, lr: LaneRt) -> None:
        cfg, ls = self.cfg, lr.ls
        if self.paused or not lr.online() or ls.get("pending"):
            return
        active = ls.get("active")
        if active is None:
            if not lr.local and ls.get("bare"):
                self.assign(lr)           # a client with no login at all: give it one
            return
        if now() - ls.get("last_swap", 0) < cfg.cooldown:
            return
        live = self.usages.get(active)
        # A read that fails is what a throttled account does; fall back to the
        # last good snapshot rather than sitting on an account we cannot see.
        # Either way the reading is projected forward by its age at the
        # measured burn: what was read is a lower bound on where the account
        # is now, and this runs every tick, so the swap fires at the predicted
        # moment rather than at whichever poll happens to land after it.
        u_act, age = rotation_usage(self.last_good, active, live)
        if u_act is not None:
            u_act = projected_usage(ls, active, u_act)
        hold = self.hold_window(lr)
        if hold:
            self.hold_in_place(lr, active, u_act, *hold)
            return
        ended = ls.get("peak_hold") is not None
        if ended:
            ls["peak_hold"] = None        # the clock ended it; [r] clears it itself
            save_state(cfg, ls)
        other = self.displaced_by(lr)
        reason = f"also live on {other}" if other else None
        if reason is None:
            reason = is_exhausted(u_act, lr.cfg)
            if reason is None:
                reason = about_to_exhaust(ls, active, u_act, lr.cfg)
            if reason and age is not None:
                reason += f" · usage read: {live.error if live else 'unavailable'}"
        if reason and ended:
            reason = "peak-hour hold ended · " + reason
        if not reason:
            self.clear_hold_notice(lr)
            if cfg.preempt:
                self.maybe_preempt(lr, active)
            return
        pool = self.pool(lr)
        target = self.background(f"choosing an account for {lr.host}", verified_target,
                                 cfg, pool, active, preload_estimate(ls))
        self.adopt_reads(pool)
        if not target:
            # everyone is spent: hold position and rescan the moment the first
            # account's binding limits have reset (plus a small settle buffer)
            recovery = earliest_recovery(pool, cfg, active, preload_estimate(ls))
            if recovery:
                when, who = recovery
                lr.notice = (f"all accounts exhausted — waiting for {who} "
                             f"(recovers in {fmt_dur(when - now())})")
                self.next_scan = min(self.next_scan, max(when + RESCAN_MIN_S, now() + RESCAN_MIN_S))
            else:
                lr.notice = "active account exhausted but no fallback has headroom"
                self.next_scan = min(self.next_scan, now() + max(RESCAN_MIN_S, cfg.interval))
            return
        self.clear_hold_notice(lr)
        self.swap(lr, target, reason, self.usages.get(target))

    def maybe_preempt(self, lr: LaneRt, active: str) -> None:
        """Pre-emptive move while the active account still has headroom; the
        target is confirmed with a fresh read like any other swap."""
        cfg, ls = self.cfg, lr.ls
        pool = self.pool(lr)
        choice = preempt_target(cfg, ls, pool, active)
        if not choice:
            return
        target, why = choice
        fresh = self.background(f"checking {target} for {lr.host}", usage_for,
                                get_account(cfg, target).cred_path)
        if fresh.error or ls.get("active") != active or ls.get("pending") \
                or target in self.holdings(lr):
            return                        # unreadable, or the fleet moved meanwhile
        self.usages[target] = pool[target] = fresh
        if is_exhausted(fresh, cfg) or not comfortable(fresh, cfg, preload_estimate(ls)):
            return
        if preempt_target(cfg, ls, pool, active) != choice:
            return                        # the fresh read changed the picture
        self.swap(lr, target, why, fresh, preemptive=True, verb="moved early to")

    def manual_rotate(self, lr: LaneRt, reason: str) -> None:
        """[r] on the master for its own lane, or a client asking for its own."""
        ls = lr.ls
        hold = self.hold_window(lr)
        if hold:
            # [r] ends the hold for this range: the rotation that follows is
            # the ordinary one, and no re-park until the next day's range
            ls["peak_released"] = hold[0]
            ls["peak_hold"] = None
            add_event(ls, "peak-hour hold ended by keypress")
            self.clear_hold_notice(lr)
        if ls.get("pending"):
            lr.notice = f"a swap to {ls['pending'].get('to')} is already under way"
            return
        pool = self.pool(lr)
        target = self.background(f"choosing an account for {lr.host}", verified_target,
                                 self.cfg, pool, ls.get("active"), preload_estimate(ls))
        self.adopt_reads(pool)
        if target:
            self.swap(lr, target, reason, self.usages.get(target))
        else:
            lr.notice = "no fallback with headroom"

    def assign(self, lr: LaneRt) -> None:
        pool = self.pool(lr)
        target = self.background(f"choosing an account for {lr.host}", verified_target,
                                 self.cfg, pool, None, preload_estimate(lr.ls))
        self.adopt_reads(pool)
        if target:
            self.swap(lr, target, "first account for this host", self.usages.get(target))
        else:
            lr.notice = "no account has headroom for this host"

    def swap(self, lr: LaneRt, target: str, reason: str, snapshot: "Usage | None",
             preemptive: bool = False, parking: float | None = None, verb: str = "rotated to") -> None:
        """Move a lane to `target`.  This machine's lane swaps at once; a
        client's is sent the target's credentials and commits when the
        client reports the swap done (`finish_swap`)."""
        cfg, ls = self.cfg, lr.ls
        try:
            if lr.local:
                detail = do_swap(cfg, ls, get_account(cfg, target), reason, snapshot, preemptive,
                                 run=lambda fn, *args: self.background(f"swapping to {target}",
                                                                       fn, *args))
                self.monitor.note_own_write()
                if parking is not None:
                    ls["swap_snapshot"] = None
                    ls["peak_hold"] = parking
                    save_state(cfg, ls)
                lr.notice = f"{verb} {target} ({detail})"
                self.poll(lr)
                return
            creds = self.background(f"preparing {target} for {lr.host}", prepare_creds,
                                    cfg, get_account(cfg, target))
        except CcrollError as e:
            lr.notice = f"rotation failed: {e}"
            add_event(ls, lr.notice)
            save_state(cfg, ls)
            return
        sid = f"{lr.host}-{int(now() * 1000):x}"
        ls["pending"] = {"id": sid, "to": target, "reason": reason, "preemptive": preemptive,
                         "parking": parking, "verb": verb, "t": now()}
        lr.pending_snapshot = snapshot
        lr.conn.send({"type": "swap", "id": sid, "to": target, "credentials": creds,
                      "identity": stored_identity(cfg, target)})
        lr.notice = f"swapping to {target} …"
        save_state(cfg, ls)

    def finish_swap(self, lr: LaneRt, pend: dict, result: dict) -> None:
        ls = lr.ls
        ls["pending"] = None
        if not result.get("ok"):
            lr.notice = f"rotation failed: {result.get('error') or 'the client could not swap'}"
            add_event(ls, lr.notice)
            save_state(self.cfg, ls)
            return
        target = pend["to"]
        ls["bare"] = False
        detail = commit_swap(lr.cfg, ls, target, pend.get("reason") or "", lr.pending_snapshot,
                             bool(pend.get("preemptive")))
        lr.pending_snapshot = None
        if result.get("identity_note"):
            add_event(ls, result["identity_note"])
        if result.get("reasserted"):
            add_event(ls, "re-asserted swap over a concurrent write")
        if pend.get("parking") is not None:
            ls["swap_snapshot"] = None
            ls["peak_hold"] = pend["parking"]
        lr.notice = f"{pend.get('verb') or 'rotated to'} {target} ({detail})"
        save_state(self.cfg, ls)
        self.poll(lr)

    # --- the link ------------------------------------------------------------
    def handle_message(self, c: Conn, msg: dict | None) -> None:
        """One message, and a link that sends one this master cannot act on
        is closed — never the master brought down by it."""
        try:
            if msg is not None and msg.get("type") == "garbled":
                raise ValueError(msg.get("why") or "a line that is not a JSON object")
            self.on_message(c, msg)
        except Exception as e:            # noqa: BLE001 — a peer's bad input, whatever it is
            who = c.lane.host if c.lane is not None else "a link"
            add_event(self.state, f"{who}: link closed — unusable message ({type(e).__name__}: "
                                  f"{str(e)[:60]})")
            c.send({"type": "error", "fatal": False, "msg": "the master could not use a message"})
            c.flush()
            if self.server is not None:
                self.server.drop(c)
            self.on_disconnect(c)
            save_state(self.cfg, self.state)

    def on_message(self, c: Conn, msg: dict | None) -> None:
        if msg is None:
            self.on_disconnect(c)
            return
        c.heard = now()
        if msg.get("v") != PROTO_V:
            who = msg.get("host") or "a client"
            c.send({"type": "error", "fatal": True,
                    "msg": f"protocol v{msg.get('v')} is not the master's v{PROTO_V} — "
                           f"run the same ccroll version on both machines"})
            c.closing = True
            add_event(self.state, f"{who}: link refused — it speaks protocol v{msg.get('v')}, "
                                  f"the master v{PROTO_V}")
            return
        kind = msg.get("type")
        if kind == "release":
            self.on_release(c, str(msg.get("host") or ""))
            return
        if kind == "hello":
            self.on_hello(c, msg)
            return
        lr = c.lane
        if lr is None or lr.conn is not c:
            c.send({"type": "error", "fatal": True, "msg": "no hello on this link"})
            c.closing = True
            return
        if kind == "beat":
            self.take_beat(c, msg)
        elif kind == "creds":
            self.adopt_login(lr, _str(msg.get("email")), _obj(msg.get("credentials")), True)
        elif kind == "swapped":
            self.on_swapped(lr, msg)
        elif kind == "rotate":
            self.manual_rotate(lr, f"manual (keypress on {lr.host})")

    def take_beat(self, c: Conn, msg: dict) -> None:
        for key in ("cols", "rows", "interval"):
            if isinstance(msg.get(key), int) and msg[key] > 0:
                setattr(c, key, msg[key])
        if "color" in msg:
            c.color = bool(msg["color"])
        if "sessions" in msg:
            c.sessions = msg["sessions"] if isinstance(msg["sessions"], int) else None

    def on_hello(self, c: Conn, msg: dict) -> None:
        host = str(msg.get("host") or "").strip()
        if not host or len(host) > 64 or not host.isprintable():
            c.send({"type": "error", "fatal": True, "msg": "the client sent no usable host name"})
            c.closing = True
            return
        if host == self.host:
            c.send({"type": "error", "fatal": True,
                    "msg": f"host name {host!r} is the master's own — start the client with --name"})
            c.closing = True
            return
        holds, creds = _str(msg.get("holds")), _obj(msg.get("creds"))
        returns = msg.get("returns") or []
        if not isinstance(returns, list) or not all(isinstance(r, dict) for r in returns):
            raise ValueError("returns must be a list of objects")
        for r in returns:                 # checked before anything is recorded for the host
            _str(r.get("id")), _str(r.get("from")), _obj(r.get("from_credentials"))
        machine = _str(msg.get("machine"))
        lr = self.remote.get(host)
        known = lr.ls.get("machine") if lr is not None else None
        if lr is not None and machine and known and machine != known \
                and (lr.conn is not None or lr.ls.get("active") or lr.ls.get("pending")):
            # two machines under one name would take turns replacing each
            # other's link, and each hello would say a different login is live
            where = "is linked" if lr.conn is not None else f"holds {lr.ls.get('active') or 'a swap'}"
            c.send({"type": "error", "fatal": True,
                    "msg": f"another machine named {host!r} {where} on this master — start this one "
                           f"with --name, or `ccroll release {host}` if that machine is gone"})
            c.closing = True
            add_event(self.state, f"refused a second machine calling itself {host}")
            return
        if machine and holds:
            # the same machine back under a new --name, on the login its old
            # name still holds: that hold is this link's, not a second machine's
            for old_host, old in list(self.remote.items()):
                if old_host != host and old.conn is None and old.ls.get("machine") == machine \
                        and old.ls.get("active") == holds and not old.ls.get("pending"):
                    del self.remote[old_host]
                    self.state["lanes"].pop(old_host, None)
                    add_event(self.state, f"{old_host} is now called {host} — its hold on {holds} moves with it")
        lr = lr or self.remote_lane(host)
        if machine:
            lr.ls["machine"] = machine
        if lr.conn is not None and lr.conn is not c:
            # the host linked again while its old link still looked open — a
            # half-open one left by a network split, say: the new link is the
            # one its machine is on.  The old one is told (if it can still
            # hear) and closed now, not left to drain into a peer that may
            # never read again.
            old = lr.conn
            old.lane = None
            old.send({"type": "error", "fatal": True, "msg": "replaced by a newer link from this host"})
            old.flush()
            self.server.drop(old)
            add_event(lr.ls, "linked again — the previous link was still open, closed it")
        lr.conn, c.lane = c, lr
        self.take_beat(c, msg)
        ls = lr.ls
        ls["signal"] = bool(msg.get("signal", True))
        lr.cfg.signal = ls["signal"]
        was = ls.get("offline_since")
        ls["offline_since"] = None
        # results that never got their ack come first: they carry the newest
        # tokens of the accounts this host left
        for result in returns:
            self.on_swapped(lr, result, reply=False)
        pend = ls.get("pending")
        if pend:
            if holds == pend.get("to"):
                self.finish_swap(lr, pend, {"ok": True})   # it landed; its report was lost
            else:
                ls["pending"] = None
                add_event(ls, f"swap to {pend.get('to')} never reached this host — "
                              f"it stays on {holds or 'no account'}")
        self.adopt_login(lr, holds, creds, bool(msg.get("live")), bool(msg.get("unverified")))
        add_event(ls, "linked" + (f" (offline since {fmt_clock(was)})" if was else ""))
        c.last_frame = None
        c.send({"type": "welcome", "host": host, "master": self.host})
        save_state(self.cfg, self.state)

    def on_swapped(self, lr: LaneRt, result: dict, reply: bool = True) -> None:
        """A client's swap result.  Only the swap still pending is acted on:
        a result seen before (resent because its ack was lost) was filed
        then, and filing its tokens again could undo a refresh made since."""
        _str(result.get("id")), _str(result.get("from")), _obj(result.get("from_credentials"))
        _str(result.get("error")), _str(result.get("identity_note"))
        pend = lr.ls.get("pending") or {}
        if pend and pend.get("id") == result.get("id"):
            name = result.get("from")
            # the account left, with the newest tokens the client had for it —
            # unless another lane is on it, whose own copy is the live one
            if result.get("ok") and name and name not in self.holdings(lr):
                self.store_creds(name, result.get("from_credentials"))
            self.finish_swap(lr, pend, result)
        if reply and lr.conn:
            lr.conn.send({"type": "ack", "id": result.get("id")})

    def adopt_login(self, lr: LaneRt, email: str | None, creds: dict | None, live: bool,
                    unverified: bool = False) -> None:
        """What a client says its live login is.  Its report wins: it is what
        that machine's sessions are actually running on.

        `unverified`: the client could not name its live login just now (the
        profile read failed after its CLI refreshed the tokens) and `email`
        is only the last one it knew.  That keeps the account held — freeing
        it would let this master refresh it, rotating the token out from
        under that machine's sessions — but files no credentials: they are
        not known to be that account's."""
        ls = lr.ls
        prev = ls.get("active")
        note = None
        if unverified and email:
            if email != prev and email in {acc.name for acc in list_accounts(self.cfg)} \
                    and email not in self.holdings(lr):
                ls["active"], ls["active_since"], ls["bare"] = email, now(), False
            note = f"live login there not confirmed yet — {ls.get('active') or email} stays held"
            if note != ls.get("login_note"):
                add_event(ls, note)
            ls["login_note"] = note
            save_state(self.cfg, self.state)
            return
        if not email:
            ls["active"] = None
            ls["bare"] = not live
            if live:
                note = "an unknown login is live there — this host is not rotated until it is one of the store's"
            elif prev:
                note = f"{prev} is no longer live there"
        elif email not in {acc.name for acc in list_accounts(self.cfg)}:
            ls["active"], ls["bare"] = None, False
            note = f"live login {email} is not in the store — this host is not rotated (add it on the master)"
        else:
            ls["bare"] = False
            other = self.holdings(lr).get(email)
            if other is None:
                self.store_creds(email, creds)
            if prev != email:
                ls["active"] = email
                ls["active_since"] = now()
                note = f"live login is {email}" + (f" (was {prev})" if prev else "")
            if other:
                note = f"⚠ {email} is live on {other} too"
        if note and note != ls.get("login_note"):
            add_event(ls, note)
        ls["login_note"] = note
        save_state(self.cfg, self.state)

    def on_disconnect(self, c: Conn) -> None:
        lr = c.lane
        if lr is None or lr.conn is not c:
            return
        lr.conn, c.lane = None, None
        ls = lr.ls
        ls["offline_since"] = now()
        held = ls.get("active")
        add_event(ls, "link lost" + (f" — keeps {held} until it links again or "
                                     f"`ccroll release {lr.host}`" if held else ""))
        save_state(self.cfg, self.state)

    def on_release(self, c: Conn, host: str) -> None:
        lr = self.remote.get(host)
        if lr is None:
            ok, text = False, f"no client host {host!r} is known here"
        elif lr.conn is not None:
            ok, text = False, f"{host} is linked right now — only an offline host can be released"
        else:
            held = lr.ls.get("active") or (lr.ls.get("pending") or {}).get("to")
            del self.remote[host]
            self.state["lanes"].pop(host, None)
            text = f"released {host}" + (f" — {held} is free for the other hosts" if held else "")
            ok = True
            add_event(self.state, text + " (by command)")
            save_state(self.cfg, self.state)
        c.send({"type": "released", "ok": ok, "msg": text})
        c.closing = True

    def push_views(self) -> None:
        """Each linked client is sent its fleet view whenever it changes —
        rendered here at that client's terminal size, with its own lane as
        the one marked — and nothing while it does not."""
        for lr in self.remote.values():
            c = lr.conn
            if c is None:
                continue
            frame = fit_frame(self.render(lr, Ansi(c.color), clock=False, keys=False),
                              c.cols, max(1, c.rows - CLIENT_CHROME_LINES))
            if frame != c.last_frame:
                c.last_frame = frame
                c.send({"type": "view", "frame": frame, "active": lr.ls.get("active"),
                        "paused": self.paused})

    # --- the dashboard -------------------------------------------------------
    def host_cell(self, a: Ansi, lr: "LaneRt | None", going: "LaneRt | None",
                  viewer: LaneRt) -> tuple[str, int]:
        if lr is None:
            if going is None:
                return "", 0
            text = f"→ {going.host}"
            return a.yellow(text), len(text)
        if not lr.online():
            text = f"{lr.host} ✗ offline"
            return a.red(text), len(text)
        text = lr.host
        return (a.cyan(a.bold(text)) if lr is viewer else text), len(text)

    def table_rows(self, viewer: LaneRt, view: dict, a: Ansi) -> list:
        fleet = bool(self.remote)
        on, going = {}, {}
        for lr in self.lanes():
            if lr.ls.get("active"):
                on.setdefault(lr.ls["active"], lr)
            to = (lr.ls.get("pending") or {}).get("to")
            if to:
                going.setdefault(to, lr)
        rows = []
        for acc in self.accounts:
            lr = on.get(acc.name)
            row = (acc.name, view.get(acc.name), lr is viewer)
            if fleet:
                row += (self.host_cell(a, lr, going.get(acc.name), viewer),)
            rows.append(row)
        if self.local.ls.get("active") is None and oauth_of(read_json(self.cfg.live_path)):
            row = (LIVE_PSEUDO, view.get(LIVE_PSEUDO), viewer.local)
            if fleet:
                row += (self.host_cell(a, self.local, None, viewer),)
            rows.insert(0, row)
        return rows

    def render_hosts(self, viewer: LaneRt, a: Ansi, view: dict) -> list[str]:
        """Every lane on one line: where it runs, what it is on, whether the
        master can reach it, when its account hits a limit at its own burn,
        and when it last swapped."""
        cfg = self.cfg
        cells = [[(a.bold(h), len(h)) for h in
                  ("", "Host", "Live account", "Link", "Next limit at its burn", "Session burn", "Last swap")]]
        for lr in self.lanes():
            ls = lr.ls
            name = ls.get("active")
            mark = ("►", 1) if lr is viewer else ("", 0)
            host = lr.host + (" (master)" if lr.local else "")
            hostc = (a.cyan(a.bold(host)) if lr is viewer else host, len(host))
            if name:
                acct = name
            elif lr.local and oauth_of(read_json(cfg.live_path)):
                acct = LIVE_PSEUDO
            elif ls.get("bare"):
                acct = "(no login yet)"
            else:
                acct = "(unknown login)" if lr.online() else "—"
            to = (ls.get("pending") or {}).get("to")
            acctc = (acct + a.yellow(f" → {to}"), len(acct) + 3 + len(to)) if to else (acct, len(acct))
            if lr.local:
                link = (a.dim("this machine"), 12)
            elif lr.conn is not None:
                c = lr.conn
                if now() - c.heard > 2 * max(c.interval, 1):
                    text = f"silent since {fmt_clock(c.heard)}"
                    link = (a.yellow(text), len(text))
                else:
                    text = "linked" + (f" · {c.sessions} session{'' if c.sessions == 1 else 's'}"
                                       if c.sessions is not None else "")
                    link = (a.green(text), len(text))
            else:
                off = ls.get("offline_since")
                text = f"offline since {fmt_clock(off)}" if off else "offline"
                link = (a.red(text), len(text))
            u = view.get(name) if name else None
            lim, burn = (a.dim("—"), 1), (a.dim("—"), 1)
            if u is not None and not u.error:
                etas = [(eta, label) for _, label, _, _, eta in limit_etas(ls, name, u, cfg)
                        if eta is not None]
                if etas:
                    eta, label = min(etas)
                    dur, dlen = fmt_dur3(a, eta)
                    lim = (f"{label} in ≈" + dur, len(label) + 5 + dlen)
                elif in_grace(ls):
                    lim = (a.dim("post-swap grace"), 15)
                rate = active_burn(ls, name, "session")
                if rate is not None:
                    text = f"{rate:.1f}%/h" if rate >= BURN_MIN_RATE else "idle"
                    burn = (text, len(text))
            elif u is not None:
                text = u.error if len(u.error) <= 32 else u.error[:31] + "…"
                lim = (a.yellow(text), len(text))
            last = (fmt_clock(ls["last_swap"]), 8) if ls.get("last_swap") else (a.dim("—"), 1)
            cells.append([mark, hostc, acctc, link, lim, burn, last])
        widths = [max(w for _, w in col) for col in zip(*cells)]
        lines = [a.bold(f"hosts · {len(self.lanes())} lanes")]
        for i, row in enumerate(cells):
            lines.append("  ".join(t + " " * (widths[c] - w) for c, (t, w) in enumerate(row)).rstrip())
            if i == 0:
                lines.append(a.dim("  ".join("─" * w for w in widths)))
        for lr in self.lanes():
            if lr is not viewer and lr.notice:
                lines.append(a.dim(f"  {lr.host}: ") + a.yellow(lr.notice))
        return lines

    def render(self, viewer: LaneRt, a: Ansi, clock: bool = True, keys: bool = True) -> str:
        cfg, ls = self.cfg, viewer.ls
        label = scoped_label_of(self.usages)
        active = ls.get("active")
        fleet = bool(self.remote)
        govern = (f"{label.lower()}≥{cfg.scoped_threshold:.0f}%" if cfg.mode == "scoped"
                  else "weekly·all≥99%")
        early = (f" · early to next reset when runway>{cfg.preempt_runway / 3600:g}h"
                 + (" +touch" if cfg.touch else "")) if cfg.preempt else ""
        win = peak_window(cfg)
        peak = (a.dim("  ·  ") + a.dim(f"peak hold {fmt_local(cfg, win[0])[:5]}–{fmt_local(cfg, win[1])}")
                if win else "")
        mode = (a.red("rotation PAUSED") if self.paused
                else a.green(f"auto-rotate at session≥{cfg.threshold:.0f}% / {govern} "
                             f"or ≤{cfg.lead:.0f}s from a limit{early}")) + peak
        # what the master is waiting on goes up front: the line is cut to
        # the terminal's width, and this is what explains a pause
        busy = (a.yellow(f"⟳ {self.busy}…") + a.dim("  ·  ")) if self.busy else ""
        head = a.bold(f"ccroll {CCROLL_VERSION}") + a.dim("  ·  ") + busy + mode
        if clock:
            head += a.dim("  ·  ") + fmt_clock(now())
        role = ""
        if fleet:
            linked = sum(1 for lr in self.remote.values() if lr.conn is not None)
            where = (f"master {self.host}" if viewer.local
                     else f"this host: {viewer.host}  ·  master {self.host}")
            role = a.cyan(where) + a.dim(f"  ·  {linked} of {len(self.remote)} client hosts linked")
        lines = [head, role]
        view = merged_view(self.usages, self.last_good, [lr.ls for lr in self.lanes()])
        lines += render_table(a, self.table_rows(viewer, view, a), label)
        if fleet:
            lines += [""] + self.render_hosts(viewer, a, view)
        if active and view.get(active) and not view[active].error:
            # the burn panel and the early-rotation warning read the same
            # projected figures rotation acts on, blackout or not
            lines += [""] + render_burn(a, ls, active, view[active], label)
            soon = about_to_exhaust(ls, active, view[active], cfg)
            if soon:
                lines.append(a.red(f"  ⚠ rotating early: {soon}"))
        n = readable_accounts(self.accounts, self.usages)
        burning = [(lr.ls, lr.ls.get("active")) for lr in self.lanes()]
        burning = [(s, nm) for s, nm in burning if nm and view.get(nm) and not view[nm].error]
        if burning:
            fc = combine_forecasts([fleet_forecast(s, nm, view[nm], cfg, n) for s, nm in burning])
            lines += [""] + render_fleet_fc(a, fc, cfg, n, label, usages=self.usages,
                                            lanes=len(burning))
        hold = self.hold_window(viewer)
        if hold and active:
            until = blocked_until(view.get(active), cfg)
            held = (f"parked on {active} · refuses requests for {fmt_dur(until - now())}"
                    if until else f"{active} is not refusing requests — looking for an account that is")
            lines += ["", a.inverse(a.yellow(" PEAK-HOUR HOLD ")) + a.yellow(
                f"  {held}  ·  resumes at {fmt_local(cfg, hold[1])} "
                f"(in {fmt_dur(hold[1] - now())}) or on [r]")]
        pool = self.pool(viewer)
        target = pick_target(pool, cfg, exclude=active, preload=preload_estimate(ls))
        if target:
            lines += ["", a.dim("next in line: ") + a.green(target)
                      + a.dim(f"  ({target_reason(pool[target], cfg, label)})")]
        if viewer.notice:
            lines += ["", a.yellow(viewer.notice)]
        events = self.state.get("events", [])[-(6 if fleet else 4):]
        if events:
            lines += ["", a.bold("events")]
            tags = [event_host(self.state, e) or self.host for e in events]
            width = max((len(h) for h in tags), default=0)
            for e, tag in zip(events, tags):
                where = f"{tag:<{width}}  " if fleet else ""
                lines.append(a.dim(f"  {fmt_clock(e[0])}  {where}{e[1]}"))
        if keys:
            lines += ["", a.dim("[q]uit  [r]otate now  [s]can  [p]ause auto-rotate")]
        return "\n".join(lines)

    # --- the loop ------------------------------------------------------------
    def run(self) -> int:
        cfg, a = self.cfg, self.a
        if not self.accounts:
            print(a.yellow("No accounts under " + cfg.root))
            print("Add each subscription account with:  ccroll add")
            return 1
        if cfg.signal:
            signal_init(cfg, self.state)
            warn = check_auto_continue(cfg)
            if warn:
                print(a.yellow("⚠ " + warn))
                print(a.dim("  (it applies after a CLI restart; ccroll never edits your settings)"))
                time.sleep(2)
        try:
            self.server = Server(cfg)
        except OSError as e:
            # a filesystem that cannot hold a Unix socket: this machine is
            # still rotated as ever, only no client can link
            self.server = None
            self.local.notice = (f"no client can link: cannot serve {master_sock_path(cfg)} "
                                 f"({e.strerror or e})")
        self.tty_out = sys.stdout.isatty()
        if self.tty_out:
            sys.stdout.write("\033[?1049h\033[?25l")
            sys.stdout.flush()
        try:
            with Keyboard() as kb:
                self.kb = kb
                running = True
                while running:
                    if now() >= self.next_scan:
                        self.rescan()
                    for lr in self.lanes():
                        if now() >= lr.next_poll:
                            self.poll(lr)
                    changed = self.monitor.check()
                    if changed:
                        self.local.notice = changed
                    for lr in self.lanes():
                        self.maybe_rotate(lr)
                    self.draw()
                    self.push_views()
                    key, msgs = (self.server.wait(1.0, kb) if self.server is not None
                                 else (kb.read(1.0), []))
                    # what arrived while a read was under way goes first, in order
                    msgs, self.backlog = self.backlog + msgs, []
                    for c, msg in msgs:
                        self.handle_message(c, msg)
                    keys, self.keys = self.keys + ([key] if key else []), []
                    for key in keys:
                        if key in ("q", "\x03"):
                            running = False
                        elif key == "s":
                            self.next_scan = 0
                        elif key == "p":
                            self.paused = not self.paused
                            self.local.notice = ("auto-rotation paused" if self.paused
                                                 else "auto-rotation resumed")
                        elif key == "r":
                            self.manual_rotate(self.local, "manual (keypress)")
        finally:
            if self.tty_out:
                sys.stdout.write("\033[?25h\033[?1049l")
                sys.stdout.flush()
            if self.server is not None:
                self.server.close()
            save_state(cfg, self.state)
        return 0

    def draw(self) -> None:
        """This machine's screen: the dashboard on a terminal, a line per
        poll otherwise."""
        a = self.a
        if self.tty_out:
            size = shutil.get_terminal_size()
            frame = fit_frame(self.render(self.local, a), size.columns, size.lines)
            sys.stdout.write("\033[H" + frame.replace("\n", "\033[K\n") + "\033[K\033[J")
            sys.stdout.flush()
        elif self.printed != self.local.next_poll and self.busy is None:
            self.printed = self.local.next_poll
            act = self.local.ls.get("active") or LIVE_PSEUDO
            u = self.usages.get(act)
            notice = self.local.notice
            if u and not u.error:
                print(f"{fmt_clock(now())} active={act} session={u.session_pct or 0:.0f}% "
                      f"weekly={u.weekly_pct or 0:.0f}% scoped={u.scoped_pct or 0:.0f}%"
                      + (f" · {notice}" if notice else ""), flush=True)
            elif u:
                print(f"{fmt_clock(now())} active={act} error: {u.error}", flush=True)


def cmd_watch(cfg: Cfg, a: Ansi, as_master: bool = False) -> int:
    link = saved_link(cfg)
    if link and not as_master:
        # a second, independent master would rotate this machine's login
        # against the real master's: two engines handing out one pool
        print(a.yellow(f"⚠ this machine is set up as a client of {link.get('master_host') or link_label(link)}"
                       f" (saved in {client_state_path(cfg)})."))
        print("  Its login is meant to be rotated by `ccroll client`; starting a master here runs a "
              "second, independent rotation engine.")
        if not sys.stdin.isatty():
            print("  Pass --as-master to start one anyway, or `ccroll client --forget` to stop being a client.")
            return 1
        try:
            answer = input("  Start a master here anyway? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("  not started — run `ccroll client` instead")
            return 1
    return Master(cfg, a).run()


# --- client ---------------------------------------------------------------------
class Client:
    """`ccroll client`: this machine's live login, rotated by a master
    elsewhere.  It keeps no store and makes no decisions: it reports what
    its live login is (and every token the CLI refreshes), carries out the
    swaps the master sends, writes the account-switch feed for its own
    sessions, and shows the fleet view the master renders for it."""

    def __init__(self, cfg: Cfg, a: Ansi, master: str, cmd: list, host: str,
                 link: dict | None = None):
        self.cfg, self.a, self.master, self.cmd, self.host = cfg, a, master, cmd, host
        self.link = link or {}                 # how this run reaches the master
        self.said: list[str] = []              # what to print once the screen is back
        self.path = os.path.join(cfg.root, ".ccroll", CLIENT_STATE_FILE)
        self.cs = read_json(self.path) or {}
        self.cs.setdefault("held", None)       # the account the live file is, as far as we know
        self.cs.setdefault("returns", [])      # swap results not yet acknowledged
        self.machine = machine_id(self.cs)     # tells this machine from another of the same name
        self.fatal = False                     # the master refused this client: no retrying
        self.proc: subprocess.Popen | None = None
        self.inbuf = b""
        self.errtail = ""
        self.linked = False
        self.frame: str | None = None
        self.frame_t: float | None = None
        self.down_since: float | None = now()
        self.next_try = 0.0
        self.tries = 0
        self.problem: str | None = None        # why the link is not up, for the header
        self.notice = ""
        self.next_beat = 0.0
        self.size = None
        self.unknown = False                   # a live login we could not name yet
        self.recheck_at = 0.0
        self.live_mtime = _mtime_ns(cfg.live_path)
        self.expected = (oauth_of(read_json(cfg.live_path)) or {}).get("accessToken")

    def save(self) -> None:
        write_json_atomic(self.path, self.cs)

    def note_held(self, email: str, oauth: dict | None) -> None:
        self.cs["held"] = email
        self.cs["held_refresh"] = (oauth or {}).get("refreshToken")
        self.cs["held_access"] = (oauth or {}).get("accessToken")
        self.save()

    def identify(self) -> tuple[str | None, dict | None]:
        """Which account the live credentials are, and the credentials.  The
        tokens we last knew for it answer without a request; otherwise the
        account's own profile endpoint names it (a read, no refresh)."""
        creds = read_json(self.cfg.live_path)
        oauth = oauth_of(creds)
        if not oauth:
            self.unknown = False
            return None, None
        held = self.cs.get("held")
        if held and (oauth.get("accessToken") == self.cs.get("held_access")
                     or (oauth.get("refreshToken")
                         and oauth.get("refreshToken") == self.cs.get("held_refresh"))):
            email = held
        else:
            email = fetch_email(oauth["accessToken"])
        self.unknown = email is None
        if email is None:
            self.recheck_at = now() + self.cfg.interval
            return None, creds
        self.expected = oauth["accessToken"]
        if email != held or oauth.get("accessToken") != self.cs.get("held_access"):
            self.note_held(email, oauth)
        return email, creds

    # --- the link ------------------------------------------------------------
    def send(self, msg: dict) -> None:
        if self.proc is None:
            return
        try:
            self.proc.stdin.write(frame_msg(msg))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.drop("the link broke")

    def connect(self) -> None:
        self.tries += 1
        self.inbuf, self.errtail = b"", ""
        try:
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, bufsize=0, start_new_session=True)
        except OSError as e:
            self.proc = None
            self.problem = f"cannot start {self.cmd[0]}: {e.strerror or e}"
            self.next_try = now() + self.cfg.interval
            return
        os.set_blocking(self.proc.stdout.fileno(), False)
        os.set_blocking(self.proc.stderr.fileno(), False)
        email, creds = self.identify()
        # a live login we cannot name right now (the profile read failed) is
        # most likely still the one we last knew: claim it as unverified, so
        # the master keeps it held rather than free to be refreshed elsewhere
        unverified = email is None and creds is not None and bool(self.cs.get("held"))
        size = self.size or shutil.get_terminal_size()
        self.send({"type": "hello", "host": self.host, "version": CCROLL_VERSION,
                   "machine": self.machine,
                   "holds": email or (self.cs.get("held") if unverified else None),
                   "unverified": unverified, "live": creds is not None,
                   "creds": creds if email else None, "returns": self.cs["returns"],
                   "signal": self.cfg.signal, "interval": self.cfg.interval,
                   "cols": size.columns, "rows": size.lines, "color": self.a.enabled,
                   "sessions": count_sessions(self.cfg.live_dir)})

    def drop(self, reason: str) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                rest = proc.stderr.read() or b""
                self.errtail = (self.errtail + rest.decode(errors="replace"))[-2000:]
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
        err = [l for l in self.errtail.splitlines() if l.strip()]
        if not self.problem or self.linked:
            self.problem = err[-1].strip() if err else reason
        if self.linked:
            self.down_since = now()
        self.linked = False
        # once straight away after losing a link that was up, then at the
        # client's own cadence; never after the master refused this client
        if self.fatal:
            self.next_try = float("inf")
        else:
            self.next_try = now() if self.tries == 0 else now() + self.cfg.interval

    def handle(self, msg: dict) -> None:
        if msg.get("v") != PROTO_V:
            self.problem = (f"the master speaks protocol v{msg.get('v')}, this client v{PROTO_V} — "
                            f"run the same ccroll version on both machines")
            self.drop(self.problem)
            return
        kind = msg.get("type")
        if kind == "welcome":
            self.linked, self.tries, self.problem, self.down_since = True, 0, None, None
            self.master = str(msg.get("master") or self.master)
            self.cs["returns"] = []           # the hello carried them; the master has them
            self.remember_link()
            self.save()
            self.next_beat = 0.0
        elif kind == "view":
            self.frame, self.frame_t = msg.get("frame"), now()
        elif kind == "swap":
            self.carry_out(msg)
        elif kind == "signal":
            self.apply_signal(msg)
        elif kind == "ack":
            self.cs["returns"] = [r for r in self.cs["returns"] if r.get("id") != msg.get("id")]
            self.save()
        elif kind == "error":
            self.problem = str(msg.get("msg") or "the master refused the link")
            if msg.get("fatal"):
                self.fatal = True             # retrying would only be refused again

    def remember_link(self) -> None:
        """Saved only once a link has worked, so a mistyped host is never
        remembered; from then on a plain `ccroll client` uses it."""
        if not self.link:
            return
        saved = self.cs.get("link") if isinstance(self.cs.get("link"), dict) else {}
        link = {"master": self.link.get("master"), "relay_cmd": self.link.get("relay_cmd"),
                "name": self.link.get("name"), "master_host": self.master}
        changes = link_changes(saved, link)
        if not changes and saved.get("master_host") == self.master:
            return
        self.cs["link"] = dict(link, saved_at=now())
        if changes:
            what = ("saved: " if not saved else "saved link updated: ") + " · ".join(changes)
            self.notice = what
            self.said.append(what + " — from now on a plain `ccroll client` uses it")

    def carry_out(self, msg: dict) -> None:
        """The swap the master decided, done here exactly as the master does
        its own; the previous account's newest tokens go back with the
        result, which is kept until acknowledged."""
        to = msg.get("to")
        # the tokens going back are filed as the account they are: the one we
        # hold when the live file is still that account's lineage, else
        # whatever the live login turns out to be (someone may have run
        # /login by hand) — and nothing, when it cannot be named
        live = oauth_of(read_json(self.cfg.live_path)) or {}
        held = self.cs.get("held")
        if held and live and (live.get("accessToken") == self.cs.get("held_access")
                              or live.get("refreshToken") == self.cs.get("held_refresh")):
            left = held
        elif live:
            left, _ = self.identify()
        else:
            left = None
        try:
            old_creds, note, again = client_swap(self.cfg, to, msg.get("credentials"),
                                                 msg.get("identity"))
            result = {"type": "swapped", "id": msg.get("id"), "ok": True, "to": to,
                      "from": left, "from_credentials": old_creds if left else None,
                      "identity_note": note, "reasserted": again}
            oauth = oauth_of(msg.get("credentials"))
            self.note_held(to, oauth)
            self.expected = (oauth or {}).get("accessToken")
            self.live_mtime = _mtime_ns(self.cfg.live_path)
            self.unknown = False
            self.notice = f"swapped to {to}"
            self.cs["returns"].append(result)
            self.save()
        except (CcrollError, OSError) as e:
            result = {"type": "swapped", "id": msg.get("id"), "ok": False, "error": str(e)}
            self.notice = f"swap to {to} failed: {e}"
        self.send(result)

    def apply_signal(self, msg: dict) -> None:
        if not self.cfg.signal:
            return
        with contextlib.suppress(Exception):  # a signal must never break the client
            if msg.get("op") == "write":
                signal_write(self.cfg, self.cs, str(msg.get("event")), msg.get("fields") or {},
                             msg.get("snapshot"))
            elif msg.get("op") == "snapshot":
                _signal_snapshot(self.cfg, self.cs, msg.get("updates") or {})
            self.save()

    def watch_live(self) -> None:
        """The CLI refreshing the live token, or a manual login: tell the
        master, so its copy of the tokens is always the newest."""
        m = _mtime_ns(self.cfg.live_path)
        due = self.unknown and now() >= self.recheck_at
        if m == self.live_mtime and not due:
            return
        self.live_mtime = m
        oauth = oauth_of(read_json(self.cfg.live_path))
        if not oauth or (oauth.get("accessToken") == self.expected and not due):
            return
        email, creds = self.identify()
        if email and self.linked:
            self.send({"type": "creds", "email": email, "credentials": creds})

    def beat(self, size) -> None:
        self.size = size
        self.next_beat = now() + self.cfg.interval
        self.send({"type": "beat", "live_mtime": _mtime_ns(self.cfg.live_path),
                   "sessions": count_sessions(self.cfg.live_dir), "cols": size.columns, "rows": size.lines,
                   "color": self.a.enabled, "interval": self.cfg.interval})

    def pump(self, timeout: float, kb: "Keyboard") -> str | None:
        rl = [sys.stdin] if kb.enabled else []
        if self.proc is not None:
            rl += [self.proc.stdout, self.proc.stderr]
        try:
            r, _, _ = select.select(rl, [], [], timeout)
        except InterruptedError:
            return None
        key = None
        for f in r:
            if f is sys.stdin:
                key = sys.stdin.read(1)
            elif self.proc is not None and f is self.proc.stderr:
                with contextlib.suppress(BlockingIOError):
                    data = os.read(f.fileno(), 65536)
                    self.errtail = (self.errtail + data.decode(errors="replace"))[-2000:]
            elif self.proc is not None and f is self.proc.stdout:
                try:
                    data = os.read(f.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not data:
                    self.drop("the master closed the link")
                    continue
                msgs, self.inbuf = _parse_lines(self.inbuf + data)
                for msg in msgs:
                    try:
                        self.handle(msg)
                    except Exception as e:    # noqa: BLE001 — never let a bad line end the client
                        self.problem = f"unusable message from the master ({type(e).__name__})"
                        self.drop(self.problem)
                    if self.proc is None:
                        break
                if self.proc is not None and len(self.inbuf) > MAX_MSG_BYTES:
                    self.drop("the master sent a message larger than any real one")
        return key

    # --- the display ---------------------------------------------------------
    def render(self, size) -> str:
        """The client's chrome around the master's view: who and what this
        machine is on, then the link — and when the link is down, the last
        view dimmed under a STALE banner, since it is no longer current."""
        a = self.a
        held = self.cs.get("held") if not self.unknown else None
        if held is None:
            held = "(unknown login)" if self.unknown else (
                "(no login)" if not oauth_of(read_json(self.cfg.live_path)) else "…")
        via = self.link.get("master")
        via = f" ({via})" if via and via != self.master else ""
        head = (a.bold("ccroll client") + a.dim("  ·  ") + f"{self.host} → master {self.master}{via}"
                + a.dim("  ·  ") + "live: " + a.cyan(held) + a.dim("  ·  ") + fmt_clock(now()))
        if self.linked:
            status = a.green("● linked") + a.dim(
                "  ·  fleet view from the master"
                + (f", updated {fmt_clock(self.frame_t)}" if self.frame_t else ""))
            if self.notice:
                status += a.dim("  ·  ") + a.yellow(self.notice)
        else:
            since = f" since {fmt_clock(self.down_since)}" if self.down_since else ""
            retry = (" · not retrying — fix it and start `ccroll client` again" if self.fatal
                     else f" · retry in {max(0.0, self.next_try - now()):.0f}s")
            # the reason first: the line is cut to the terminal's width
            status = (a.red(f"✗ {'refused by the master' if self.fatal else 'master unreachable'}{since}")
                      + (a.dim(": ") + a.red(self.problem) if self.problem else "")
                      + a.dim(retry))
        if self.frame is None:
            body = a.dim("waiting for the master's view…")
        elif self.linked:
            body = self.frame
        else:
            rest = self.frame.split("\n")[1:]
            banner = (a.inverse(a.yellow(" STALE ")) + " " + a.yellow(
                f"view from {fmt_clock(self.frame_t)} · this machine keeps working on {held}; "
                f"rotation resumes when the master is back"))
            body = "\n".join([banner] + [a.dim(strip_ansi(l)) for l in rest])
        frame = "\n".join([head, status, body, "", a.dim("[q]uit  [r]otate this host now")])
        return fit_frame(frame, size.columns, size.lines)

    def run(self) -> int:
        cfg, a = self.cfg, self.a
        if cfg.signal:
            signal_init(cfg, self.cs)
            warn = check_auto_continue(cfg)
            if warn:
                print(a.yellow("⚠ " + warn))
                print(a.dim("  (it applies after a CLI restart; ccroll never edits your settings)"))
                time.sleep(2)
        tty_out = sys.stdout.isatty()
        printed = None
        if tty_out:
            sys.stdout.write("\033[?1049h\033[?25l")
            sys.stdout.flush()
        try:
            with Keyboard() as kb:
                running = True
                while running:
                    if self.proc is None and now() >= self.next_try:
                        self.connect()
                    self.watch_live()
                    size = shutil.get_terminal_size()
                    if self.linked and (now() >= self.next_beat or size != self.size):
                        self.beat(size)
                    if tty_out:
                        frame = self.render(size)
                        sys.stdout.write("\033[H" + frame.replace("\n", "\033[K\n") + "\033[K\033[J")
                        sys.stdout.flush()
                    else:
                        line = (f"{'linked' if self.linked else 'unlinked'} "
                                f"live={self.cs.get('held')} {self.problem or ''}".rstrip())
                        if (line, self.frame_t) != printed:
                            printed = (line, self.frame_t)
                            print(f"{fmt_clock(now())} {line}", flush=True)
                    key = self.pump(1.0, kb)
                    if key in ("q", "\x03"):
                        running = False
                    elif key == "r":
                        if self.linked:
                            self.send({"type": "rotate"})
                            self.notice = "asked the master to rotate this host"
                        else:
                            self.notice = "not linked — the master decides every rotation"
        finally:
            if tty_out:
                sys.stdout.write("\033[?25h\033[?1049l")
                sys.stdout.flush()
            if self.proc is not None:
                with contextlib.suppress(OSError):
                    self.proc.kill()
            self.save()
            for line in self.said:
                print(line)
        return 0


def client_state_path(cfg: Cfg) -> str:
    return os.path.join(cfg.root, ".ccroll", CLIENT_STATE_FILE)


def saved_link(cfg: Cfg) -> dict:
    """The master this machine was set up as a client of, if any."""
    link = (read_json(client_state_path(cfg)) or {}).get("link")
    return link if isinstance(link, dict) and (link.get("master") or link.get("relay_cmd")) else {}


def link_label(link: dict) -> str:
    return link.get("master") or f"--relay-cmd {link.get('relay_cmd')!r}"


def link_changes(saved: dict, link: dict) -> list[str]:
    """What `link` changes against the saved one, for the user to read."""
    out = []
    for key, label in (("master", "master"), ("relay_cmd", "relay command"), ("name", "name")):
        if saved.get(key) != link.get(key):
            was = saved.get(key)
            out.append(f"{label} {link.get(key) or '(none)'}" + (f" (was {was})" if was else ""))
    return out


def relay_command(cfg: Cfg, link: dict) -> list:
    if link.get("relay_cmd"):
        return shlex.split(link["relay_cmd"])
    # ssh probes a silent link at the client's own cadence and gives up after
    # two unanswered probes: one lost probe (a dropped packet on Wi-Fi) is
    # tolerated, a second in a row is a dead link.  So a network split is
    # noticed within about three --interval, the relay exits, and the client
    # links again — the master then replaces the half-open old link.
    return ["ssh", "-T", "-o", "BatchMode=yes",
            "-o", f"ServerAliveInterval={cfg.interval}", "-o", "ServerAliveCountMax=2",
            link["master"], "ccroll", "relay"]


def machine_id(cs: dict) -> str:
    """This machine's identity for the master: /etc/machine-id where there
    is one, else an id made once and kept in the client's state."""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        with contextlib.suppress(OSError):
            with open(path, encoding="ascii") as fh:
                mid = fh.read().strip()
            if mid:
                return mid
    if not isinstance(cs.get("machine_id"), str):
        cs["machine_id"] = os.urandom(16).hex()
    return cs["machine_id"]


def cmd_client(cfg: Cfg, a: Ansi, master: str | None, relay_cmd: str | None, name: str | None,
               forget: bool = False) -> int:
    """Set up once with --master (or --relay-cmd); the link is saved on the
    first successful hello, and a plain `ccroll client` uses it from then on.
    Options given override the saved ones and are saved in turn."""
    saved = saved_link(cfg)
    if forget:
        path = client_state_path(cfg)
        cs = read_json(path) or {}
        if not saved:
            print("no master link is saved on this machine")
            return 0
        # only the link goes: the rest of the file carries swap results the
        # master may not have acknowledged yet, and the signal feed's state
        cs.pop("link", None)
        write_json_atomic(path, cs)
        print(f"forgot the saved link to {link_label(saved)} — "
              f"`ccroll client --master HOST` sets up a new one")
        return 0
    if master or relay_cmd:
        link = {"master": master, "relay_cmd": relay_cmd}
    elif saved:
        link = {"master": saved.get("master"), "relay_cmd": saved.get("relay_cmd")}
    else:
        raise CcrollError("no master is saved on this machine — run `ccroll client --master HOST` "
                          "once (it is remembered after the first successful link)")
    link["name"] = name or saved.get("name")
    return Client(cfg, a, link["master"] or "(via --relay-cmd)", relay_command(cfg, link),
                  link["name"] or socket.gethostname(), link=link).run()


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data):]


def cmd_relay(cfg: Cfg) -> int:
    """stdin/stdout <-> the master's socket: what a client runs through ssh."""
    path = master_sock_path(cfg)
    out = sys.stdout.buffer.fileno()
    try:
        s = unix_connect(path)
    except OSError as e:
        _write_all(out, frame_msg({"type": "error", "fatal": False,
                                   "msg": f"no ccroll master is running on {socket.gethostname()} "
                                          f"({path}: {e.strerror or e})"}))
        return 1
    fin = sys.stdin.buffer.fileno()
    reading = True
    try:
        while True:
            r, _, _ = select.select([fin, s] if reading else [s], [], [])
            if fin in r:
                data = os.read(fin, 65536)
                if data:
                    s.sendall(data)
                else:                     # the client is done talking: let the master answer
                    reading = False
                    s.shutdown(socket.SHUT_WR)
            if s in r:
                data = s.recv(65536)
                if not data:
                    break
                _write_all(out, data)
    except OSError:
        return 1
    finally:
        s.close()
    return 0


def cmd_release(cfg: Cfg, a: Ansi, host: str, yes: bool) -> int:
    state = load_state(cfg)
    lane = (state.get("lanes") or {}).get(host)
    held = (lane or {}).get("active") or ((lane or {}).get("pending") or {}).get("to")
    print(a.yellow(f"⚠ releasing client host {host}" + (f", which holds {held}" if held else "")))
    print(f"  Do this only when {host} is gone for good, or its Claude Code sessions are stopped.")
    if held:
        print(f"  If it is still running on {held}, it and whichever host gets {held} next would")
        print("  share one refresh token, and the first refresh by either logs the other out.")
    if not yes:
        try:
            ok = input("Release it? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            ok = False
        if not ok:
            print("nothing changed")
            return 1
    try:
        s = unix_connect(master_sock_path(cfg))
    except OSError:
        s = None
    if s is not None:                        # the running master owns the state: ask it
        with s:
            s.sendall(frame_msg({"type": "release", "host": host}))
            buf = b""
            while b"\n" not in buf:
                data = s.recv(65536)
                if not data:
                    break
                buf += data
        msgs, _ = _parse_lines(buf)
        reply = msgs[0] if msgs else {"ok": False, "msg": "the master did not answer"}
        print((a.green("✓ ") if reply.get("ok") else a.red("✗ ")) + str(reply.get("msg")))
        return 0 if reply.get("ok") else 1
    if lane is None:
        raise CcrollError(f"no client host {host!r} is known in {cfg.state_path}")
    state["lanes"].pop(host, None)
    add_event(state, f"released {host}" + (f" — {held} is free for the other hosts" if held else "")
              + " (by command)")
    save_state(cfg, state)
    print(a.green(f"✓ released {host}"))
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


def login_email(oauth: dict, profile_dir: str) -> tuple[str | None, str | None]:
    """(email, note) for a login just completed in `profile_dir`.

    The account's own profile endpoint is asked first.  When it does not
    answer — it shares the per-account limiter with the usage endpoint, so a
    busy night is exactly when it will not — the identity the CLI wrote into
    the profile at login is the same account's own word for who it is, and
    stands in.  A completed login is never discarded for a transient read;
    the note says which source named the account."""
    email = _email_of_creds(oauth)
    if email:
        return email, None
    ident = (read_json(os.path.join(profile_dir, CONFIG_FILE)) or {}).get("oauthAccount") or {}
    email = ident.get("emailAddress") if isinstance(ident, dict) else None
    if isinstance(email, str) and "@" in email:
        return email, "profile endpoint did not answer; named from the login's stored identity"
    return None, None


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
            add_event(state, "add: login discarded — no credentials stored")
            save_state(cfg, state)
            print(a.red("✗ no credentials were stored — login not completed?"))
        else:
            if "user:profile" not in (oauth.get("scopes") or []):
                print(a.yellow("⚠ token lacks user:profile scope (console login?) — "
                               "usage cannot be read; use the Claude-account login instead"))
            email, note = login_email(oauth, tmp)
            if note:
                print(a.yellow(f"⚠ {note}"))
            if not email:
                shutil.rmtree(tmp, ignore_errors=True)
                add_event(state, "add: login discarded — account email unreadable")
                save_state(cfg, state)
                print(a.red("✗ could not read the account's email (network?) — nothing saved, try again"))
            else:
                dest = os.path.join(cfg.root, email)
                if os.path.isdir(dest):
                    _register(cfg, state, email, creds)
                    # the temp profile is about to go: keep its identity, or the
                    # account can never be identity-synced on a swap
                    warn = store_identity(
                        cfg, email,
                        (read_json(os.path.join(tmp, CONFIG_FILE)) or {}).get("oauthAccount"))
                    shutil.rmtree(tmp, ignore_errors=True)
                    print(a.green(f"↻ {email} — credentials updated (account already existed)"))
                    if warn:
                        print(a.yellow(f"⚠ {warn}"))
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
    # the live config names the account that logged in, which after a swap is
    # not necessarily the one these credentials belong to — copy it only when
    # the two agree
    ident = (read_json(cfg.live_config_path) or {}).get("oauthAccount")
    shown = (ident or {}).get("emailAddress")
    warn = None
    if isinstance(ident, dict) and shown and shown != email:
        warn = (f"live config still names {shown} — identity not stored for {email}; "
                f"re-run `ccroll add` for it to enable --sync-identity")
    else:
        warn = store_identity(cfg, email, ident)
    state["active"] = email
    add_event(state, f"adopted live login: {email}")
    save_state(cfg, state)
    print(a.green(f"✓ live login saved as {email} and marked active"))
    if warn:
        print(a.yellow(f"⚠ {warn}"))
    return 0


def cmd_switch(cfg: Cfg, a: Ansi, name: str) -> int:
    state = load_state(cfg)
    signal_init(cfg, state)
    target = get_account(cfg, name)
    host = client_holdings(state).get(target.name)
    if host:
        raise CcrollError(f"{target.name} is live on client {host} — two machines on one "
                          f"account would share its refresh token")
    do_swap(cfg, state, target, "manual")
    print(a.green(f"✓ live credentials now {target.name} — running sessions pick this up automatically"))
    if cfg.sync_identity:
        for ev in state.get("events", [])[-2:]:
            msg = ev[1]
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
    p.add_argument("--by", choices=("scoped", "weekly"), default="weekly",
                   help="which weekly limit governs rotation and next-account choice: "
                        "'scoped' = the per-model weekly limit (Fable on current Max plans), "
                        "'weekly' = the all-models weekly limit (default: weekly)")
    sub = p.add_subparsers(dest="cmd")

    w = sub.add_parser("watch", help="live dashboard + auto-rotation (default)")
    w.add_argument("--threshold", type=float, default=99, help="rotate when session %% reaches this (default 99)")
    w.add_argument("--scoped-threshold", type=float, default=97,
                   help="rotate when the per-model weekly %% reaches this (default 97)")
    w.add_argument("--interval", type=int, default=60, help="active-account poll seconds (default 60)")
    w.add_argument("--lead", type=float, default=None, metavar="SECONDS",
                   help="rotate early once the active account's predicted time to any limit at its "
                        "current burn drops under this (default 60, or one poll interval if longer); the static "
                        "thresholds remain the latest point")
    w.add_argument("--scan", type=int, default=600, help="all-accounts scan seconds (default 600)")
    w.add_argument("--cooldown", type=int, default=0,
                   help="min seconds between swaps (default 0: none needed, a swap "
                        "requires the source to be spent and the target not to be)")
    w.add_argument("--no-rotate", action="store_true", help="dashboard only, never swap")
    w.add_argument("--as-master", action="store_true",
                   help="start even though this machine is saved as a client of another master")
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
    w.add_argument("--signal-dir", metavar="DIR",
                   help="where to write the account-switch feed other Claude Code sessions "
                        "tail (default <claude-dir>/account-switch)")
    w.add_argument("--no-signal", action="store_true",
                   help="do not write the account-switch feed at all")
    w.add_argument("--peak-hold", metavar="HH:MM-HH:MM", default=None,
                   help="daily clock range to sit out (off unless given; the peak hours are "
                        f"{PEAK_HOLD_EXAMPLE} in {PEAK_TZ_DEFAULT}): at its start ccroll parks the live "
                        "credentials on an account that is already refusing requests, so every "
                        "session waits out a usage limit as usual, and rotates on normally at its "
                        "end or when you press r")
    w.add_argument("--peak-tz", metavar="ZONE", default=PEAK_TZ_DEFAULT,
                   help=f"IANA time zone the --peak-hold clock is read in (default {PEAK_TZ_DEFAULT})")
    w.add_argument("--sync-identity", action="store_true", help="also point Claude Code's displayed identity (the `oauthAccount` block in its global config) at the account swapped to, so /status stops naming the previous one; auth already follows the swap without this. Off by default: running sessions cache that config in memory, so the correction usually shows up only in newly started sessions, and a session that rewrites the file from memory undoes it")

    sub.add_parser("status", help="one-shot usage table for all accounts")
    sub.add_parser("add", help="log account(s) in interactively; each is named by its own email",
                   aliases=["login"])
    sub.add_parser("adopt", help="save the current live login into the store under its email")
    sp = sub.add_parser("switch", help="hot-swap the live credentials now")
    sp.add_argument("name", help="account email (a unique prefix is enough)")
    sp.add_argument("--signal-dir", metavar="DIR",
                   help="where to write the account-switch feed other Claude Code sessions "
                        "tail (default <claude-dir>/account-switch)")
    sp.add_argument("--no-signal", action="store_true",
                   help="do not write the account-switch feed at all")
    sp.add_argument("--sync-identity", action="store_true", help="also point Claude Code's displayed identity (the `oauthAccount` block in its global config) at the account swapped to, so /status stops naming the previous one; auth already follows the swap without this. Off by default: running sessions cache that config in memory, so the correction usually shows up only in newly started sessions, and a session that rewrites the file from memory undoes it")
    sub.add_parser("list", help="accounts and token expiries")
    cp = sub.add_parser("client", help="let a ccroll master on another machine rotate this "
                                       "machine's live login (the master keeps the store)")
    cp.add_argument("--master", metavar="HOST",
                    help="the master's host, reached as `ssh HOST ccroll relay` (key-based ssh)")
    cp.add_argument("--relay-cmd", metavar="CMD",
                    help="command that speaks to the master's socket on stdin/stdout, instead of "
                         "the ssh default (e.g. 'ssh -p 2222 me@host ~/bin/ccroll --root X relay')")
    cp.add_argument("--name", help="this host's name on the master (default: the hostname)")
    cp.add_argument("--forget", action="store_true",
                    help="forget the saved master link (set up again with --master)")
    cp.add_argument("--interval", type=int, default=60,
                    help="seconds between heartbeats and between reconnect attempts (default 60)")
    cp.add_argument("--signal-dir", metavar="DIR",
                    help="where to write the account-switch feed this machine's sessions tail "
                         "(default <claude-dir>/account-switch)")
    cp.add_argument("--no-signal", action="store_true",
                    help="do not write the account-switch feed at all")
    cp.add_argument("--sync-identity", action="store_true",
                    help="also point the displayed identity at the account swapped to "
                         "(see `watch --sync-identity`)")
    sub.add_parser("relay", help="pipe stdin/stdout to this machine's master socket "
                                 "(what a client runs through ssh)")
    rp = sub.add_parser("release", help="free the account an offline client host holds")
    rp.add_argument("host", help="the client host, as the master's dashboard names it")
    rp.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    args = p.parse_args(argv)
    a = Ansi(enabled=sys.stdout.isatty() and not args.no_color)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        cfg = Cfg(args)                   # can reject a --peak-hold range or --peak-tz
        cmd = args.cmd or "watch"
        if cmd == "watch":
            return cmd_watch(cfg, a, bool(getattr(args, "as_master", False)))
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
        if cmd == "client":
            return cmd_client(cfg, a, args.master, args.relay_cmd, args.name, args.forget)
        if cmd == "relay":
            return cmd_relay(cfg)
        if cmd == "release":
            return cmd_release(cfg, a, args.host, args.yes)
        p.error(f"unknown command {cmd!r}")
    except CcrollError as e:
        print(a.red(f"error: {e}"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
