# ccroll

**Hands your long-running Claude Code session from one subscription account to the next — without breaking stride.**

You're deep into an interactive Claude Code session: hours of context, subagents running, a compaction or two behind you. Then the 5-hour window (or the weekly limit) runs out. Restarting under another account kills your subagents and costs you the re-fill of the whole context anyway. Hitting token limits kills all your subtasks requiring restart and scanning the status. This restart can
take 25% of your session token quota. And switching by hand — `/login`, browser, login rituals — breaks your flow a dozen times a week (or per day if you fan out your work).

ccroll removes the chore. It runs in its own terminal as a live dashboard over all of your accounts, estimates when the active one will hit its limits, and **hot-swaps the live credentials to the account with the most headroom** — while your session keeps running, untouched.

```
ccroll 0.1.0  ·  auto-rotate at session≥97% / fable≥97%  ·  22:04:31

   Account            Session (5h)      Weekly · all      Weekly · Fable    Status
─  ─────────────────  ────────────────  ────────────────  ────────────────  ──────
►  tero@example.com    62% ↺0d 02h 10m   31% ↺3d 02h 40m   45% ↺5d 01h 12m  ok
   dev@example.com    100% ↺0d 00h 55m   88% ↺1d 04h 02m  100% ↺2d 03h 15m  BLOCKED
   ops@example.com     12% ↺0d 04h 41m    8% ↺6d 01h 33m    9% ↺6d 01h 33m  ok

burn · tero@example.com
  session        62.3%  ·  burn   14.2%/h  ·  limit in ≈0d 02h 39m  ·  resets in 0d 02h 10m  ✓ reset first
  weekly·all     31.0%  ·  burn    1.1%/h  ·  limit in ≈2d 14h 40m  ·  resets in 3d 02h 40m  ✓ reset first
  weekly·fable   45.2%  ·  burn    1.9%/h  ·  limit in ≈1d 04h 12m  ·  resets in 5d 01h 12m  ⚠ limit first

next in line: ops@example.com

events
  12:10  → tero@example.com (session 91%)

[q]uit  [r]otate now  [s]can  [p]ause auto-rotate
```

## How it works

Claude Code watches its credentials file (`.credentials.json` in the config dir) **by mtime and reloads it live** when it changes on disk — it has to, because concurrent sessions share that file and rotate refresh tokens through it. ccroll uses that deliberately: to switch accounts it atomically replaces the file with another account's credentials. The running interactive session — TUI, context, subagents, everything — never notices anything beyond "credentials refreshed". No restart, no `/login`, no browser automation, no terminal multiplexer tricks.

Each account lives in its own isolated Claude config dir under `~/.claude-accounts/`, **named by the account's email address** — enforced, not chosen: `ccroll add` logs you in first and then reads the email from the account itself, so the name on screen is always the identity that is actually live, never a label that drifted out of sync. From then on ccroll keeps every account's tokens fresh via the same OAuth refresh flow the CLI uses, reads usage through the free OAuth usage endpoint (no quota is spent on monitoring), and — whenever it swaps — first *harvests* the live file back into the store, so rotated refresh tokens are never lost.

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
| `ccroll add` | Register account(s) via one-time interactive login — each is named by its own email, read from the account after login |
| `ccroll adopt` | Save the current live login into the store (under its email) and mark it active |
| `ccroll switch <email>` | Hot-swap the live credentials right now (a unique prefix is enough: `ccroll switch ops`) |
| `ccroll list` | Accounts and token expiries |

Global flags: `--root` (account store, default `~/.claude-accounts`), `--claude-dir` (live config dir, default `$CLAUDE_CONFIG_DIR` or `~/.claude`), `--by {scoped,weekly}`, `--no-color`.

## Rotation policy

ccroll rotates away from the active account when any of these hold:

- session (5-hour) usage ≥ `--threshold` (default **90%**) — proactive, so you never see the "limit reached" banner;
- the governing weekly limit is (nearly) spent;
- the API reports the account as blocked.

**Which weekly limit governs is a flag.** Plans with a per-model weekly limit (e.g. the Fable limit on current Max plans) usually hit *that* wall first, so `--by scoped` (the default) treats it as the constraint and picks the next account by most scoped headroom. If you don't care about the per-model limit — or your plan has none — use `--by weekly` to govern by the all-models weekly limit instead.

The next account is chosen by most headroom on the governing limit (with hysteresis: an account must be comfortably clear of the thresholds to qualify), and ties break on account name so the same fleet always resolves the same way. The choice is then confirmed against a fresh read of that account before the swap commits, because fleet data can be up to one scan old and an account may have been consumed on another machine meanwhile. ccroll never swaps onto an account that is already spent.

**There is no swap cooldown**, and none is needed. A swap requires the account you are leaving to be spent and the one you are moving to not to be; usage within a window only rises, and only the active account consumes. Returning to an earlier account therefore requires that account's window to have reset, which is recovery rather than flapping. Set `--cooldown` if you want one anyway.

**When every account is spent**, ccroll doesn't give up: it shows which account recovers first and waits for exactly that moment (the latest reset among that account's binding limits), then rescans and rotates as soon as headroom exists.

Tuning: `--threshold`, `--scoped-threshold`, `--interval` (active-account poll, default 60 s), `--scan` (full-fleet scan, default 300 s), `--cooldown`, `--no-rotate` (observe only).

## Burn estimates

Every duration — resets, limit ETAs, token expiries — is shown in a fixed `0d 00h 00m` format with days, hours and minutes each in their own color (zero-value leading units dimmed), so remaining time reads in a single glance.

The dashboard samples the active account's three windows (session / weekly-all / weekly-scoped) once per minute and fits a least-squares slope over the last 45 minutes. From that it shows the burn rate (%/h), the estimated time until each limit is hit, the time until each window resets — and which comes first (`✓ reset first` / `⚠ limit first`). A window reset clears its series automatically. A first estimate appears after ~2 minutes (three samples) and is shown dimmed until the fit covers 8 minutes; a session can burn out in 10 minutes, so an early rough number beats none.

## What ccroll writes, and where

- `~/.claude-accounts/<name>/.credentials.json` — each account's tokens (0600), refreshed in place.
### Token refresh

The OAuth token endpoint only serves requests carrying the official client signature. ccroll performs the same refresh the CLI performs, against your own stored credentials, so it sends the same `User-Agent`. Without it the edge returns a Cloudflare 403 (`error code: 1010`) and every account goes blank; a generic agent gets throttled instead. Refreshes are staggered across a scan and retried with backoff when the server answers 429.

- `~/.claude-accounts/.ccroll/state.json` — active-account marker, per-account emails, burn-rate samples, event log. No secrets.
- `~/.claude/.credentials.json` (or `$CLAUDE_CONFIG_DIR`) — replaced atomically on each swap; harvested back into the store first so rotated refresh tokens survive.

Nothing is ever sent anywhere except Anthropic's own OAuth/usage endpoints.

## Caveats, honestly

- **A swap re-primes the prompt cache.** Server-side prompt caching is per account, so your session's first request after a swap re-sends its full context to the new account. That cost is inherent to switching accounts by *any* method (it's the same as restarting with `--resume`); what ccroll preserves is everything else — running subagents, session state, and your flow. This is also why ccroll rotates as rarely as possible instead of load-balancing.
- **All sessions sharing the config dir swap together.** Usually that's exactly what you want.
- **A manual `/login` in your session is detected, not fought.** If the new login matches a known account, ccroll follows it; if unknown, it pauses rotation and asks you to `ccroll adopt` it.
- **Unofficial internals.** The credentials-file format, the usage/profile endpoints, and the live-reload behavior are implementation details of Claude Code and may change in any release. ccroll fails safe (it never rotates on missing data), but expect to update it now and then.
- There is a narrow race where the running CLI writes a just-refreshed *old* token back over a fresh swap; ccroll re-checks after each swap and re-asserts once, and its change monitor catches anything later.

## Intended use

ccroll is meant for **interactive, personal use in compliance with the terms that govern your subscriptions**: it removes the manual chore of switching between accounts you legitimately hold while you work hands-on. It is **not** intended for automating headless or unattended operation, sharing accounts across people, or otherwise circumventing usage limits contrary to Anthropic's terms of service. You are responsible for ensuring that your use complies with the agreements applicable to your accounts. ccroll is an independent project, not affiliated with or endorsed by Anthropic.

## License

MIT — see [LICENSE](LICENSE).
