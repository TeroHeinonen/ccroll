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
rolling time series, and hot-swaps to the account with the most headroom when
the active one approaches a limit.

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
import json
import os
import re
import select
import shutil
import signal
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
REFRESH_MARGIN_S = 180          # refresh an access token this close to expiry
SWAP_VERIFY_DELAY_S = 2.0       # re-check the live file this long after a swap
SAMPLE_RETENTION_S = 24 * 3600  # keep at most a day of burn-rate samples
BURN_WINDOW_S = 45 * 60         # fit burn rate over the last 45 minutes
BURN_MIN_SAMPLES = 3
BURN_MIN_SPAN_S = 90            # 3 polls at the 60s default: a figure after ~2 min
BURN_SETTLED_SPAN_S = 8 * 60    # shorter fits are shown dimmed as provisional
BURN_MIN_RATE = 0.05            # %/h below this shows as idle
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
        live_dir = getattr(args, "claude_dir", None) or os.environ.get("CLAUDE_CONFIG_DIR") \
            or os.path.join(os.path.expanduser("~"), ".claude")
        self.live_dir = os.path.expanduser(live_dir)
        self.live_path = os.path.join(self.live_dir, CRED_FILE)
        self.state_path = os.path.join(self.root, ".ccroll", "state.json")
        self.threshold = float(getattr(args, "threshold", 90))
        self.scoped_threshold = float(getattr(args, "scoped_threshold", 97))
        self.interval = max(15, int(getattr(args, "interval", 60)))
        self.scan = max(self.interval, int(getattr(args, "scan", 300)))
        self.cooldown = int(getattr(args, "cooldown", 600))
        self.rotate = not getattr(args, "no_rotate", False)
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


def usage_for(cred_path: str) -> Usage:
    """Fetch usage for stored credentials, refreshing the token when needed
    (including one retry when a supposedly-valid token turns out revoked)."""
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
def harvest(cfg: Cfg, state: dict) -> None:
    """Copy the live credentials back into the active account's store dir.
    Refresh tokens rotate, so the store must always hold the newest one."""
    name = state.get("active")
    live = read_json(cfg.live_path)
    if not name or not oauth_of(live):
        return
    write_json_atomic(os.path.join(cfg.root, name, CRED_FILE), live)


def do_swap(cfg: Cfg, state: dict, target: Account, reason: str) -> str:
    """Swap the live credentials to `target`. Returns the human-readable detail
    ("left <prev>: <reason>") so callers can show the same text in notices."""
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
    detail = f"left {prev}: {reason}" if prev and prev != target.name else reason
    add_event(state, f"→ {target.name} ({detail})")
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
        save_state(cfg, state)
    return detail


# --- rotation policy ------------------------------------------------------------
def is_exhausted(u: Usage | None, cfg: Cfg) -> str | None:
    """Return the reason the account counts as spent, or None."""
    if u is None or u.error:
        return None  # never rotate on missing data
    if (u.status or "").lower() in ("rejected", "exceeded", "blocked"):
        return f"status {u.status}"
    if (u.session_pct or 0) >= cfg.threshold:
        return f"session {u.session_pct:.0f}%"
    if cfg.mode == "scoped" and u.scoped_pct is not None and u.scoped_pct >= cfg.scoped_threshold:
        return f"{(u.scoped_label or 'scoped').lower()} weekly {u.scoped_pct:.0f}%"
    if (u.weekly_pct or 0) >= 99.5:
        return f"weekly {u.weekly_pct:.0f}%"
    return None


def pick_target(usages: dict, cfg: Cfg, exclude: str | None) -> str | None:
    def key(item):
        u = item[1]
        if cfg.mode == "weekly":
            return (u.weekly_pct or 0, u.session_pct or 0, u.scoped_pct or 0)
        return (u.scoped_pct or 0, u.session_pct or 0, u.weekly_pct or 0)

    ok = [(n, u) for n, u in usages.items()
          if n != exclude and n != LIVE_PSEUDO
          and u and not u.error and u.session_pct is not None
          and not is_exhausted(u, cfg)]
    strict = [(n, u) for n, u in ok
              if (u.session_pct or 0) < cfg.threshold - 15
              and (u.weekly_pct or 0) < 95
              and (cfg.mode != "scoped" or (u.scoped_pct or 0) < 90)]
    pool = strict or ok
    return min(pool, key=key)[0] if pool else None


def earliest_recovery(usages: dict, cfg: Cfg) -> tuple[float, str] | None:
    """When every account is spent: the first moment any of them frees up.
    An account recovers when ALL of its binding limits have reset (max of its
    resets); the fleet recovers at the min of that across accounts."""
    best = None
    for name, u in usages.items():
        if name == LIVE_PSEUDO or u is None or u.error or not is_exhausted(u, cfg):
            continue
        resets = []
        if (u.session_pct or 0) >= cfg.threshold and u.session_reset:
            resets.append(u.session_reset)
        if cfg.mode == "scoped" and (u.scoped_pct or 0) >= cfg.scoped_threshold and u.scoped_reset:
            resets.append(u.scoped_reset)
        if (u.weekly_pct or 0) >= 99.5 and u.weekly_reset:
            resets.append(u.weekly_reset)
        if resets and (best is None or max(resets) < best[0]):
            best = (max(resets), name)
    return best


# --- rendering ------------------------------------------------------------------
def _pct_cell(a: Ansi, pct, reset):
    if pct is None:
        return a.dim("—"), 1
    plain = f"{pct:.0f}%"
    dur, dur_len = fmt_dur3(a, (reset - now()) if reset else None)
    return sev_color(a, pct)(f"{plain:>4}") + a.dim(" ↺") + dur, max(len(plain), 4) + 2 + dur_len


def _status_cell(a: Ansi, u: Usage):
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
        series = state["samples"].get(name, {}).get(key, [])
        fit = burn_fit(series)
        rate, span = fit if fit else (None, 0.0)
        provisional = fit is not None and span < BURN_SETTLED_SPAN_S
        eta = eta_to_limit(pct, rate)
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
        lines.append(f"  {label:<14}" + "  ·  ".join(parts) + ("  " + verdict if verdict else ""))
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
    target = pick_target(usages, cfg, exclude=active)
    if target:
        u = usages[target]
        print()
        print(a.green(f"→ best fallback: {target} "
                      f"({u.session_pct or 0:.0f}% session, {u.scoped_pct or 0:.0f}% {label.lower()})"))
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
        record_samples(state, usages)
        save_state(cfg, state)
        next_scan = now() + cfg.scan
        next_active_poll = now() + cfg.interval

    def poll_active():
        nonlocal next_active_poll
        name = state.get("active")
        key = name or LIVE_PSEUDO
        if oauth_of(read_json(cfg.live_path)):
            usages[key] = usage_for(cfg.live_path)
            if name:
                record_samples(state, {name: usages[key]})
                save_state(cfg, state)
        next_active_poll = now() + cfg.interval

    def maybe_rotate():
        nonlocal notice, next_scan
        if paused or state.get("active") is None:
            return
        if now() - state.get("last_swap", 0) < cfg.cooldown:
            return
        active = state["active"]
        reason = is_exhausted(usages.get(active), cfg)
        if not reason:
            return
        target = pick_target(usages, cfg, exclude=active)
        if not target:
            # everyone is spent: hold position and rescan the moment the first
            # account's binding limits have reset (plus a small settle buffer)
            recovery = earliest_recovery(usages, cfg)
            if recovery:
                when, who = recovery
                notice = (f"all accounts exhausted — waiting for {who} "
                          f"(recovers in {fmt_dur(when - now())})")
                next_scan = min(next_scan, when + 30)
            else:
                notice = "active account exhausted but no fallback has headroom"
            return
        try:
            detail = do_swap(cfg, state, get_account(cfg, target), reason)
            monitor.note_own_write()
            notice = f"rotated to {target} ({detail})"
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
        mode = (a.red("rotation PAUSED") if paused
                else a.green(f"auto-rotate at session≥{cfg.threshold:.0f}% / {govern}"))
        head = a.bold(f"ccroll {CCROLL_VERSION}") + a.dim("  ·  ") + mode + a.dim("  ·  ") + fmt_clock(now())
        lines = [head, ""]
        lines += render_table(a, build_rows(cfg, state, accounts, usages), label)
        if active and usages.get(active):
            lines += [""] + render_burn(a, state, active, usages[active], label)
        target = pick_target(usages, cfg, exclude=active)
        if target:
            lines += ["", a.dim("next in line: ") + a.green(target)]
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
                    target = pick_target(usages, cfg, exclude=state.get("active"))
                    if target:
                        try:
                            detail = do_swap(cfg, state, get_account(cfg, target), "manual (keypress)")
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
    w.add_argument("--threshold", type=float, default=90, help="rotate when session %% reaches this (default 90)")
    w.add_argument("--scoped-threshold", type=float, default=97,
                   help="rotate when the per-model weekly %% reaches this (default 97)")
    w.add_argument("--interval", type=int, default=60, help="active-account poll seconds (default 60)")
    w.add_argument("--scan", type=int, default=300, help="all-accounts scan seconds (default 300)")
    w.add_argument("--cooldown", type=int, default=600, help="min seconds between swaps (default 600)")
    w.add_argument("--no-rotate", action="store_true", help="dashboard only, never swap")

    sub.add_parser("status", help="one-shot usage table for all accounts")
    sub.add_parser("add", help="log account(s) in interactively; each is named by its own email",
                   aliases=["login"])
    sub.add_parser("adopt", help="save the current live login into the store under its email")
    sp = sub.add_parser("switch", help="hot-swap the live credentials now")
    sp.add_argument("name", help="account email (a unique prefix is enough)")
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
