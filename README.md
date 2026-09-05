# ccroll

**Hands your long-running Claude Code session from one subscription account to the next — without breaking stride.**

You're deep into an interactive Claude Code session: hours of context, subagents running, a compaction or two behind you. Then the 5-hour window (or the weekly limit) runs out. Restarting under another account kills your subagents and costs you the re-fill of the whole context anyway. Hitting token limits kills all your subtasks requiring restart and scanning the status. This restart can
take 25% of your session token quota. And switching by hand — `/login`, browser, login rituals — breaks your flow a dozen times a week (or per day if you fan out your work).

ccroll removes the chore. It runs in its own terminal as a live dashboard over all of your accounts, estimates when the active one will hit its limits, and **hot-swaps the live credentials to the account whose unused quota would otherwise expire first** — while your session keeps running, untouched.

```
ccroll 0.1.0  ·  auto-rotate at session≥95% / fable≥97% or ≤60s from a limit · early to next reset when runway>3h  ·  22:04:31

   Account            Session (5h)      Weekly · all      Weekly · Fable    Status
─  ─────────────────  ────────────────  ────────────────  ────────────────  ──────
►  tero@example.com    62% ↺0d 02h 10m   31% ↺3d 02h 40m   45% ↺5d 01h 12m  ok
   dev@example.com    100% ↺0d 00h 55m   88% ↺1d 04h 02m  100% ↺2d 03h 15m  BLOCKED
   ops@example.com     12% ↺0d 04h 41m    8% ↺6d 01h 33m    9% ↺6d 01h 33m  ok

burn · tero@example.com
  session        62.3%  ·  burn   14.2%/h  ·  limit in ≈0d 02h 39m  ·  resets in 0d 02h 10m  ✓ reset first
  weekly·all     31.0%  ·  burn    1.1%/h  ·  limit in ≈2d 14h 40m  ·  resets in 3d 02h 40m  ✓ reset first
  weekly·fable   45.2%  ·  burn    1.9%/h  ·  limit in ≈1d 04h 12m  ·  resets in 5d 01h 12m  ⚠ limit first

next in line: ops@example.com  (91% fable headroom expires in 0d 06h 33m · 12% session)

events
  12:10  → tero@example.com (session 91%)

[q]uit  [r]otate now  [s]can  [p]ause auto-rotate
```

## How it works

Claude Code watches its credentials file (`.credentials.json` in the config dir) **by mtime and reloads it live** when it changes on disk — it has to, because concurrent sessions share that file and rotate refresh tokens through it. ccroll uses that deliberately: to switch accounts it atomically replaces the file with another account's credentials. The running interactive session — TUI, context, subagents, everything — never notices anything beyond "credentials refreshed". No restart, no `/login`, no browser automation, no terminal multiplexer tricks.

Each account lives in its own isolated Claude config dir under `~/.claude-accounts/`, **named by the account's email address** — enforced, not chosen: `ccroll add` logs you in first and then reads the email from the account itself, so the name on screen is always the identity that is actually live, never a label that drifted out of sync. Each account's own identity — the `oauthAccount` object the login writes, naming its email and organization — is stored beside its credentials and refreshed every time you re-add it; that object is what `--sync-identity` copies. From then on ccroll keeps every account's tokens fresh via the same OAuth refresh flow the CLI uses, reads usage through the free OAuth usage endpoint (no quota is spent on monitoring), and — whenever it swaps — first *harvests* the live file back into the store, so rotated refresh tokens are never lost.

The endpoints and the public OAuth client id ccroll talks to are the same ones the Claude Code binary itself uses (verified against the shipped bundle), not third-party services. Your credentials never leave your machine.

## Requirements

- Python 3.10+ (standard library only — no dependencies)
- Claude Code 2.x on Linux — or any platform where it stores credentials in a plain `.credentials.json` file rather than the OS keychain (on macOS it usually uses the Keychain, which ccroll does not manage)
- One or more Claude subscriptions **that are yours to use**

## Install

It's a single file; run it directly or install it as a command:

```bash
python3 ccroll.py status          # run in place
pipx install .                   # …or install the `ccroll` command
```

## Quick start

```bash
# 1. Register your accounts — a one-time interactive login each.
#    Claude Code opens with a fresh isolated profile; complete the login,
#    quit, and ccroll names the account by its own email (read from the
#    account, so it can never be mislabeled). It then asks "Add another?" —
#    loop through all your accounts in one sitting (14 accounts? fine).
ccroll add

# 2. Register the login your current live session is using
#    (this also copies it into the store, named by its email):
ccroll adopt

# 3. Run the dashboard in its own terminal (e.g. a second IDE terminal):
ccroll
```

That's it. Work normally in your Claude Code session(s). When the active account approaches a limit, ccroll swaps and your session continues on the next account.

## Commands

| Command | What it does |
|---|---|
| `ccroll` / `ccroll watch` | Live dashboard + auto-rotation (the main mode) |
| `ccroll status` | One-shot usage table for all accounts |
| `ccroll add` | Register account(s) via one-time interactive login — each is named by its own email, read from the account after login, and its identity stored alongside its credentials (re-adding an existing account refreshes both) |
| `ccroll adopt` | Save the current live login into the store (under its email) and mark it active — the identity is taken from the live config too, but only when that config still names the same account |
| `ccroll switch <email>` | Hot-swap the live credentials right now (a unique prefix is enough: `ccroll switch ops`) |
| `ccroll list` | Accounts and token expiries |

Global flags: `--root` (account store, default `~/.claude-accounts`), `--claude-dir` (live config dir, default `$CLAUDE_CONFIG_DIR` or `~/.claude`), `--by {scoped,weekly}`, `--no-color`.

## Rotation policy

ccroll rotates away from the active account when any of these hold:

- session (5-hour) usage ≥ `--threshold` (default **95%**) — proactive, so you never see the "limit reached" banner;
- the governing weekly limit is (nearly) spent;
- **the burn says a limit is closer than `--lead` seconds** (default 60, or one poll interval if that is longer) — on any window: session, the governing weekly limit, or weekly-all;
- the API reports the account as blocked.

**Static thresholds are the latest point, not the trigger.** With a fan-out of subagents an account can burn 150%/h of its session window: 95% → 100% takes two minutes, which is about one poll plus one swap — too late, and every request in flight dies with a 429. So ccroll predicts the time to each limit from the burn estimate and rotates once that prediction drops under the lead time, however far from the static threshold the account still is. The estimate is deliberately pessimistic: the larger of the 45-minute least-squares slope and the recent step slope, so a burst that started two minutes ago is caught before the fit catches up. It restarts at every swap (samples taken while the account sat idle in the fleet scan say nothing about how it burns once live), a spike must persist across two consecutive polls before it counts (the first poll after a swap shows every running agent re-priming its context at once — a one-time jump, not a rate), and early rotation only applies from 50% of a window upward; below that the static thresholds alone decide. Once a limit is under ten minutes away the active account is polled every 15 s instead of every `--interval`. No burn estimate (the post-swap grace plus two minutes) means the static thresholds alone apply.

**A swap has a price: the preload.** In the first minutes after a swap every running agent re-primes its full context on the new account at once. With a large fan-out that alone can cost more than half a session window and a quarter of the weekly Fable window — it looks like a burn of 1000%/h and is nothing of the kind. So for `--grace` seconds after a swap (default 300) ccroll ignores the burn series, never rotates early and never pre-empts; the static thresholds still apply. At the end of the grace it measures what the swap cost on each window (the jump since the swap minus the sustained burn over that time), logs it as an event, shows the running median under the burn block (`swap cost ≈ session 59% · fable 24%`), and uses it twice: a candidate only counts as comfortable if the preload would still leave it real headroom, so ccroll never swaps onto an account the re-prime alone would exhaust; and pre-emption is skipped altogether while the measured cost on the governing window exceeds `--preempt-max-cost` percent (default 5) — at that price a pre-emptive swap loses more than the perishing headroom it saves.

**Which weekly limit governs is a flag.** Plans with a per-model weekly limit (e.g. the Fable limit on current Max plans) usually hit *that* wall first, so `--by scoped` (the default) treats it as the constraint and picks the next account by that window. If you don't care about the per-model limit — or your plan has none — use `--by weekly` to govern by the all-models weekly limit instead.

**The next account is the one whose unused quota is about to go to waste.** Weekly windows roll: a window opens on an account's first use and resets a week later, and whatever headroom is still unused at that moment simply vanishes. So spending from an account that resets in six hours costs nothing in the long run, while spending from one that resets in six days eats into reserve for six days. ccroll therefore ranks candidates by how fast their remaining headroom on the governing window is being lost — headroom divided by hours until reset — and drains the fastest-perishing one first, keeping accounts with distant resets as reserve. Accounts whose weekly window is not open at all (just reset, not yet touched) come first of all: using them is free too, and it starts their next week's clock immediately rather than later.

Candidates must be comfortably clear of the thresholds to qualify (hysteresis), least-loaded is the tie-break, and ties then break on account name so the same fleet always resolves the same way. The choice is confirmed against a fresh read of that account before the swap commits, because fleet data can be up to one scan old and an account may have been consumed on another machine meanwhile. ccroll never swaps onto an account that is already spent.

In simulation against a fleet of 4–11 accounts under steady, back-loaded and bursty demand near weekly capacity, this ordering cut blocked time by roughly 2–4× compared with picking the least-loaded account, at the same swap rate. Pure "earliest reset first" is not enough on its own: it leaves freshly reset accounts untouched (their reset is undefined), which delays their next week and loses capacity.

**When the weekly window is the bottleneck, ccroll also moves early.** If the fleet is Fable-bound rather than session-bound, the account to *sit on* is the one whose Fable window resets next: its unused headroom is the first to vanish, and being on it at the moment it resets reopens its next week with no idle gap. So while the active account still has headroom, ccroll moves to the account with the earliest reset among the comfortable candidates. Reset times never move once a window is open, so this target is stable and cannot flap. The move is gated on runway: it only happens while the active account's predicted time to any limit exceeds `--preempt-runway` (default 3 h), which keeps it off entirely while sessions are the constraint (there a pre-emptive swap would only add a cache re-prime). In simulation of a Fable-bound fleet at weekly capacity this cut blocked time roughly 2–3× (steady 3.6% → 1.2%, bursty 4.5% → 0.4%) for about one extra swap a day. `--no-preempt` restores rotate-only-when-spent. `--touch` goes one step further and also opens freshly reset windows at once (one request there, then on to the next-reset account); that adds another ~1.5 swaps a day and only pays when a swap costs no more than about 1% of the weekly window, so it is opt-in.

**There is no swap cooldown**, and none is needed. A swap requires the account you are leaving to be spent and the one you are moving to not to be; usage within a window only rises, and only the active account consumes. Returning to an earlier account therefore requires that account's window to have reset, which is recovery rather than flapping. Set `--cooldown` if you want one anyway.

**When every account is spent**, ccroll doesn't give up: it shows which account recovers first and waits for exactly that moment (the latest reset among that account's binding limits), then rescans and rotates as soon as headroom exists. Only resets still ahead count, and no rescan is ever scheduled less than 30 seconds out — a reset time already in the past names a moment that has happened, and treating it as one to wait for turned the watch loop into a scan of every account every second, which is itself enough to get the usage endpoint to throttle you.

**Stale readings don't strand an account, and a failed read doesn't freeze rotation.** A percentage whose reset time has already passed is the endpoint lagging behind a window that has rolled over — an account reported at 100% with an expired reset reads 0% a minute later — so ccroll ignores that window rather than counting the account as spent, and the fresh read before every swap keeps the choice honest. Missing data still never rotates, with one exception: when the *active* account's own usage read fails, which is exactly what an account being throttled does, ccroll falls back to its last error-free snapshot if that is under 15 minutes old and its window has not since reset. A window's percentage only rises until it resets, so a recent "this account is spent" reading is still sound, and the alternative is a live session pinned to an account nobody can see. An account the endpoint throttles is left alone for a minute rather than retried on every poll, and keeps showing its last good numbers with the error noted instead of a blank row.

Tuning: `--threshold`, `--scoped-threshold`, `--lead` (seconds of burn-predicted headroom at which to rotate early, default 60), `--interval` (active-account poll, default 60 s), `--scan` (full-fleet scan, default 300 s), `--preempt-runway` (hours, default 3), `--no-preempt`, `--touch`, `--grace` (post-swap seconds without burn-based rotation or pre-emption, default 300), `--preempt-max-cost` (percent of the governing window a swap may cost before pre-emption is skipped, default 5), `--sync-identity` (off by default, see below), `--cooldown`, `--no-rotate` (observe only), `--signal-dir`, `--no-signal` (see **Account-switch signals**).

## Burn estimates

Every duration — resets, limit ETAs, token expiries — is shown in a fixed `0d 00h 00m` format with days, hours and minutes each in their own color (zero-value leading units dimmed), so remaining time reads in a single glance.

The dashboard samples the active account's three windows (session / weekly-all / weekly-scoped) once per minute and fits a least-squares slope over the last 45 minutes. The burn rate shown (%/h) and the time-to-limit derived from it are the same pessimistic estimate the rotation rules act on (the larger of that fit and the sustained recent slope; when the two differ by more than a fifth the plain fit is shown alongside in dim text), so the dashboard never says 20 minutes while ccroll acts on 7. It also shows the time until each window resets — and which comes first (`✓ reset first` / `⚠ limit first`). A window reset clears its series automatically. A first estimate appears after ~2 minutes (three samples) and is shown dimmed until the fit covers 8 minutes; a session can burn out in 10 minutes, so an early rough number beats none.

## Account-switch signals

Other Claude Code sessions on the machine can be told when a rotation is coming and when it has happened, so a session can hold off spawning an expensive fan-out moments before a swap and relaunch cheaply after one. ccroll writes two files under the live config dir; sessions only read them, and if the files are absent sessions behave exactly as before.

- `~/.claude/account-switch/events.jsonl` — append-only, one JSON object per line, flushed and `fsync`ed per line so a `tail -F` never misses one. Never rewritten or truncated. Rotate it by renaming, at a moment with no live sessions.
- `~/.claude/account-switch/state.json` — the latest snapshot (`seq`, `account`, `next_switch_eta_utc`, `window_resets_utc`), replaced atomically.

Three events, and nothing else — no heartbeats and no countdowns, because every line costs each reading session a turn:

| Event | When | Fields |
|---|---|---|
| `switch_expected` | once, about 3 minutes ahead; again only if the ETA moves by more than 3 minutes | `eta_utc`, `usage_pct` |
| `switch_done` | once, immediately after the new account is live | `account`, `window_resets_utc` |
| `usage_threshold` | crossing 75% and 90%, once each per account | `usage_pct` |

`seq` is strictly increasing and never rewinds: on startup ccroll takes the highest value already in either file, so losing its own state cannot confuse a session still tailing the log. `ts` and every time field are ISO-8601 UTC with a literal `Z`. The `account` field is an **opaque label** (`acct-07`), stable per account and persisted, so the feed never carries an email — a guard refuses to write any value containing `@`.

The ETA in `switch_expected` is the time until ccroll would *actually rotate*, which means the earlier of reaching the static threshold and coming within `--lead` of 100%, computed from the same pessimistic burn the rotation rules use. It stays silent while there is no burn estimate, which includes the post-swap grace period, and while rotation is paused. `window_resets_utc` is the new account's 5-hour session window; it is `null` when that window has not opened yet, and the snapshot is filled in once a later poll learns it.

Writing a signal can never break or delay a swap: every write is wrapped, and a full disk or a read-only home is logged to the event list and swallowed. `--signal-dir` moves the feed, `--no-signal` turns it off.

**Two things ccroll deliberately does not do.** It does not edit `~/.claude/settings.json`; it only checks at startup that `"autoContinueAtUsageLimit": true` is set (so the CLI waits out a usage limit instead of prompting) and warns if it is not, because that file is yours. And it does not send keystrokes to other terminals to dismiss a usage-limit dialog — ccroll has no handle on those sessions, so that fallback has to stay manual.

## What ccroll writes, and where

- `~/.claude-accounts/<name>/.credentials.json` — each account's tokens (0600), refreshed in place.
### Token refresh

The OAuth token endpoint only serves requests carrying the official client signature. ccroll performs the same refresh the CLI performs, against your own stored credentials, so it sends the same `User-Agent`. Without it the edge returns a Cloudflare 403 (`error code: 1010`) and every account goes blank; a generic agent gets throttled instead. Refreshes are staggered across a scan and retried with backoff when the server answers 429.

- `~/.claude-accounts/.ccroll/state.json` — active-account marker, per-account emails, burn-rate samples, event log. No secrets.
- `~/.claude/.credentials.json` (or `$CLAUDE_CONFIG_DIR`) — replaced atomically on each swap; harvested back into the store first so rotated refresh tokens survive.
- `~/.claude/account-switch/{events.jsonl,state.json}` — the account-switch feed for other sessions (0700 dir): opaque account labels, timestamps and percentages, no emails and no secrets. `--no-signal` turns it off.
- `~/.claude.json` — **only with `--sync-identity`, and only its `oauthAccount` key** (see below). Never otherwise, and never replaced wholesale: that file also holds every project's session history.

Nothing is ever sent anywhere except Anthropic's own OAuth/usage endpoints.

## Caveats, honestly

- **A swap re-primes the prompt cache.** Server-side prompt caching is per account, so your session's first request after a swap re-sends its full context to the new account. That cost is inherent to switching accounts by *any* method (it's the same as restarting with `--resume`); what ccroll preserves is everything else — running subagents, session state, and your flow. This is also why ccroll rotates as rarely as possible instead of load-balancing.
- **`/status` keeps naming the previous account.** ccroll swaps credentials, and authentication follows the new account immediately — the very next request bills and rate-limits against it. But Claude Code reads the *displayed* identity (email, organization) from a separate `oauthAccount` object in its global config, which a credentials swap does not touch, so `/status` can name an account you left hours ago. Cosmetic for ordinary interactive use; it also feeds the organization header used by Claude Code on the web and Remote Control, telemetry attribution, and org-scoped plugin gating. `--sync-identity` patches that one key on each swap, atomically, skipping the write if the file looks truncated or another session wrote it underneath. It is **off by default** for two reasons: running sessions cache that config in memory, so the correction reliably shows up only in newly started sessions and a session that rewrites the file from memory undoes it; and the file holds your session history, which is not worth risking for a cosmetic fix. An account whose stored identity is missing is skipped with a note naming the remedy: re-run `ccroll add` for it. `ccroll adopt` stores the identity only when the live config still names the account being adopted, since after a swap it often names the previous one.
- **All sessions sharing the config dir swap together.** Usually that's exactly what you want.
- **A manual `/login` in your session is detected, not fought.** If the new login matches a known account, ccroll follows it; if unknown, it pauses rotation and asks you to `ccroll adopt` it.
- **Unofficial internals.** The credentials-file format, the usage/profile endpoints, and the live-reload behavior are implementation details of Claude Code and may change in any release. ccroll fails safe (missing data never rotates, except that the active account falls back to its last good reading when its own usage read fails), but expect to update it now and then.
- There is a narrow race where the running CLI writes a just-refreshed *old* token back over a fresh swap; ccroll re-checks after each swap and re-asserts once, and its change monitor catches anything later.

## Intended use

ccroll is meant for **interactive, personal use in compliance with the terms that govern your subscriptions**: it removes the manual chore of switching between accounts you legitimately hold while you work hands-on. It is **not** intended for automating headless or unattended operation, sharing accounts across people, or otherwise circumventing usage limits contrary to Anthropic's terms of service. You are responsible for ensuring that your use complies with the agreements applicable to your accounts. ccroll is an independent project, not affiliated with or endorsed by Anthropic.

## License

MIT — see [LICENSE](LICENSE).
