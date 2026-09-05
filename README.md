# ccroll

**Hands your long-running Claude Code session from one subscription account to the next — without breaking stride.**

You're deep into an interactive Claude Code session: hours of context, subagents running, a compaction or two behind you. Then the 5-hour window (or the weekly limit) runs out. Restarting under another account kills your subagents and costs you the re-fill of the whole context anyway. Hitting token limits kills all your subtasks requiring restart and scanning the status. This restart can
take 25% of your session token quota. And switching by hand — `/login`, browser, login rituals — breaks your flow a dozen times a week (or per day if you fan out your work).

ccroll removes the chore. It runs in its own terminal as a live dashboard over all of your accounts, estimates when the active one will hit its limits, and **hot-swaps the live credentials to the account whose unused quota would otherwise expire first** — while your session keeps running, untouched.

```
ccroll 0.1.0  ·  auto-rotate at session≥95% / fable≥97% or ≤180s from a limit · early to next reset when runway>3h  ·  22:04:31

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

- session (5-hour) usage ≥ `--threshold` (default **95%**) — proactive, so you never see the "limit reached" banner;
- the governing weekly limit is (nearly) spent;
- **the burn says a limit is closer than `--lead` seconds** (default 180, or three poll intervals if that is longer) — on any window: session, the governing weekly limit, or weekly-all;
- the API reports the account as blocked.

**Static thresholds are the latest point, not the trigger.** With a fan-out of subagents an account can burn 150%/h of its session window: 95% → 100% takes two minutes, which is about one poll plus one swap — too late, and every request in flight dies with a 429. So ccroll predicts the time to each limit from the burn estimate and rotates once that prediction drops under the lead time, however far from the static threshold the account still is. The estimate is deliberately pessimistic: the larger of the 45-minute least-squares slope and the recent step slope, so a burst that started two minutes ago is caught before the fit catches up. It restarts at every swap (samples taken while the account sat idle in the fleet scan say nothing about how it burns once live), a spike must persist across two consecutive polls before it counts (the first poll after a swap shows every running agent re-priming its context at once — a one-time jump, not a rate), and early rotation only applies from 50% of a window upward; below that the static thresholds alone decide. Once a limit is under ten minutes away the active account is polled every 15 s instead of every `--interval`. No burn estimate (first two minutes after a swap) means the static thresholds alone apply; ccroll never rotates on missing data.

**Which weekly limit governs is a flag.** Plans with a per-model weekly limit (e.g. the Fable limit on current Max plans) usually hit *that* wall first, so `--by scoped` (the default) treats it as the constraint and picks the next account by that window. If you don't care about the per-model limit — or your plan has none — use `--by weekly` to govern by the all-models weekly limit instead.

**The next account is the one whose unused quota is about to go to waste.** Weekly windows roll: a window opens on an account's first use and resets a week later, and whatever headroom is still unused at that moment simply vanishes. So spending from an account that resets in six hours costs nothing in the long run, while spending from one that resets in six days eats into reserve for six days. ccroll therefore ranks candidates by how fast their remaining headroom on the governing window is being lost — headroom divided by hours until reset — and drains the fastest-perishing one first, keeping accounts with distant resets as reserve. Accounts whose weekly window is not open at all (just reset, not yet touched) come first of all: using them is free too, and it starts their next week's clock immediately rather than later.

Candidates must be comfortably clear of the thresholds to qualify (hysteresis), least-loaded is the tie-break, and ties then break on account name so the same fleet always resolves the same way. The choice is confirmed against a fresh read of that account before the swap commits, because fleet data can be up to one scan old and an account may have been consumed on another machine meanwhile. ccroll never swaps onto an account that is already spent.

In simulation against a fleet of 4–11 accounts under steady, back-loaded and bursty demand near weekly capacity, this ordering cut blocked time by roughly 2–4× compared with picking the least-loaded account, at the same swap rate. Pure "earliest reset first" is not enough on its own: it leaves freshly reset accounts untouched (their reset is undefined), which delays their next week and loses capacity.

**When the weekly window is the bottleneck, ccroll also moves early.** If the fleet is Fable-bound rather than session-bound, the account to *sit on* is the one whose Fable window resets next: its unused headroom is the first to vanish, and being on it at the moment it resets reopens its next week with no idle gap. So while the active account still has headroom, ccroll moves to the account with the earliest reset among the comfortable candidates. Reset times never move once a window is open, so this target is stable and cannot flap. The move is gated on runway: it only happens while the active account's predicted time to any limit exceeds `--preempt-runway` (default 3 h), which keeps it off entirely while sessions are the constraint (there a pre-emptive swap would only add a cache re-prime). In simulation of a Fable-bound fleet at weekly capacity this cut blocked time roughly 2–3× (steady 3.6% → 1.2%, bursty 4.5% → 0.4%) for about one extra swap a day. `--no-preempt` restores rotate-only-when-spent. `--touch` goes one step further and also opens freshly reset windows at once (one request there, then on to the next-reset account); that adds another ~1.5 swaps a day and only pays when a swap costs no more than about 1% of the weekly window, so it is opt-in.

**There is no swap cooldown**, and none is needed. A swap requires the account you are leaving to be spent and the one you are moving to not to be; usage within a window only rises, and only the active account consumes. Returning to an earlier account therefore requires that account's window to have reset, which is recovery rather than flapping. Set `--cooldown` if you want one anyway.

**When every account is spent**, ccroll doesn't give up: it shows which account recovers first and waits for exactly that moment (the latest reset among that account's binding limits), then rescans and rotates as soon as headroom exists.

Tuning: `--threshold`, `--scoped-threshold`, `--lead` (seconds of burn-predicted headroom at which to rotate early, default 180), `--interval` (active-account poll, default 60 s), `--scan` (full-fleet scan, default 300 s), `--preempt-runway` (hours, default 3), `--no-preempt`, `--touch`, `--cooldown`, `--no-rotate` (observe only).

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
