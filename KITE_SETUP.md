# Zerodha Kite Connect: real broker data for the paper sleeves (decided 7 Nov 2026)

Why: the paper sleeves price option fills from an NSE scrape (fragile, blocked from the cloud sandbox) and
cash fills from Yahoo's next open. Before any real-money step we need to know whether those paper fills
match what the broker would actually have given. Kite Connect (Rs 500 a month, historical data included)
gives the broker's own quotes, option-chain depth and margins. It is used for DATA ONLY.

**Hard rule.** No code in this repository may place, modify or cancel an order. The Kite wrapper exposes
read functions only (`papertrade/kite.py` also wraps the client so that only `profile`, `instruments`,
`historical_data`, `quote` and `basket_order_margins` can be reached), and `tests/test_no_orders.py` fails if
`place_order`, `modify_order`, `cancel_order`, `place_gtt`, `modify_gtt`, `convert_position` or
`place_mf_order` appears outside a comment in any `.py` file under `papertrade/` or `tools/`
(run: `python -m unittest discover -s tests`). Going live is a
separate, written decision with its own gate (earliest April 2027, momentum sleeve only, about Rs 1 lakh).

Why no static IP yet: SEBI's retail algo framework (in force 1 Apr 2026) requires a whitelisted static IP
only for API order placement, not for data. GitHub Actions runners have no fixed address anyway, so the
day orders start, that part must run from the user's PC or a small server. Buy the static IP then.

Which account: the user's own Zerodha account. Not the family member's account (tax, authorisation and
"who is trading" questions that a chartered accountant must answer; none arise on the user's own account).

## How it fits together

```
user's PC, 08:30 IST (Task Scheduler)          GitHub Actions, 17:33 IST (existing daily run)
tools/kite_login.py                            python -m papertrade run
  logs in to Kite (manual or TOTP)               if KITE_API_KEY and KITE_ACCESS_TOKEN are set and the
  -> access token for the day                      token still works: Zerodha data (labelled "real Zerodha")
  -> gh secret set KITE_ACCESS_TOKEN              else: Yahoo + NSE scrape exactly as today (labelled as today)
                                                 journal says which source was used and why
```

The access token lives one trading day (dies about 06:00 IST next morning). A token made at 08:30 covers
the 17:33 run. If the PC was off, the run falls back; nothing breaks, the journal says "Kite: no token".

What goes where:

| Secret | Where | Who sets it |
|---|---|---|
| Kite API key | GitHub repo secret `KITE_API_KEY` | user, once |
| Kite API secret | user's PC only (Windows Credential Manager via `keyring`) | user, once |
| Kite user ID, password, TOTP secret | user's PC only, and only if the automatic login mode is used | user |
| Access token (daily) | GitHub repo secret `KITE_ACCESS_TOKEN`, written by the PC script | script |

The repository is public. Nothing above is ever committed. Claude never types or sees any of these values.

## Phase 1 (this build): measure, do not replace

1. **Option chains from Kite** for every underlying the F&O sleeve tracks, written in the existing
   `state/chains/<SYM>.json` format with `source` = `"zerodha"`. Kite's `quote()` gives depth (best bid/ask),
   last price and OI; it gives no implied vol, so IV is solved by bisection from the mid (the solver already
   in `candidates.py`). The NSE scrape stays as the fallback and the two are compared in the journal on days
   both exist (ATM mid difference per expiry).
2. **Fill check**: for every cash symbol held, bought or sold by the momentum and thesis sleeves on the
   market day, fetch Kite's official open and close and print a table in the journal: Yahoo open, Kite open,
   difference in rupees and bps, same for close. Yahoo stays the fill source in phase 1. A persistent
   difference is the evidence for phase 2.
3. **Zerodha margin** for each F&O structure in the candidates table (basket margin API), shown beside our
   estimate when a token is present. Read only.
4. Nothing changes in strategy, sizing, fills, marks or the reviewer prompts.

Phase 2 (later, with evidence from phase 1): Zerodha as the primary price and chain source, Yahoo as fallback.

## User steps (once)

Pages change; where a screen differs from this, follow the screen and tell Claude so this file is fixed.

1. **Create the API app.** Sign in at https://developers.kite.trade with your own Zerodha login. Create app,
   type "Connect", name `PaperTrade`, Zerodha client ID = your own, redirect URL
   `http://127.0.0.1:5555/callback`, postback URL empty. Pay the Rs 500 monthly plan. Note the API key and
   API secret shown on the app page.
2. **GitHub secret.** On https://github.com/AmitJoshiAI/PaperTrade: Settings, Secrets and variables,
   Actions, New repository secret: name `KITE_API_KEY`, value = the API key. (The access token secret is
   created by the script in step 5.)
3. **GitHub CLI on your PC**, needed by the script to write the daily token. In PowerShell:
   `winget install GitHub.cli`, then open a new PowerShell and run `gh auth login` (GitHub.com, HTTPS,
   browser). Check with `gh auth status`.
4. **Python packages** in the project venv: `kiteconnect`, `keyring`, `pyotp` (the coding session pins them
   in requirements.txt).
5. **Store the secrets on the PC and test the login.** From the project folder:
   `.venv\Scripts\python tools\kite_login.py --setup` asks for the API key and secret (and, only if you
   choose automatic mode, user ID, password and TOTP secret) and stores them in Windows Credential Manager
   (service `papertrade-kite`). Nothing you type is shown. Then `.venv\Scripts\python tools\kite_login.py
   --manual` the first time: it opens the Kite login page in your browser, you log in and click Authorize
   (Kite asks this once per app), and it catches the redirect on `http://127.0.0.1:5555/callback` (waits 5
   minutes). It writes `KITE_ACCESS_TOKEN` to GitHub with `gh secret set` (the token goes in on stdin, never
   on a command line) and keeps a copy in Credential Manager for local runs that day. After that, plain
   `python tools\kite_login.py` uses automatic mode if a TOTP secret is stored, else manual; `--auto` forces
   automatic. `python tools\kite_login.py --check` prints whether today's token exists and works. Exit codes
   are listed at the top of the script; the log is `tools/kite_login.log` (git-ignored, no secrets in it).
   - TOTP secret: Kite web, profile, Password and security, enable external TOTP (if already enabled, you
     will re-enrol and must add the new code to your authenticator app too). Keep the text secret shown.
   - Automatic login is tolerated by Zerodha rather than officially supported. Start with manual mode if
     you prefer; both write the same token.
6. **Schedule it.** `python tools/kite_login.py --install-task` creates a Windows Task Scheduler entry
   "PaperTrade Kite login" at 08:30 on weekdays with a retry at 16:30, in the PC's local time (keep the PC on
   IST). Both runs use `--if-needed`, so the 16:30 run does nothing when the 08:30 token works. It runs as
   you, only while you are logged on (Credential Manager is per user), and catches up when the PC comes back
   if a time was missed. In manual mode the 08:30 run opens the browser and waits 5 minutes for you; only
   automatic mode works unattended. If the PC is off all day, the daily run falls back to Yahoo and NSE.
7. **Check.** The next daily journal has, under the F&O sleeve's Observations, `Chains: real Zerodha (n
   underlyings: ...)` or `Kite: no token, NSE scrape used` (or `Kite: <error>, ...`), with a `Chain check
   <SYM>, mid Zerodha vs NSE ...` line per underlying on days both sources worked; option rows priced
   `real Zerodha chain (mid)`; a "Zerodha margin" column in the F&O candidates structure tables; and,
   after the momentum sleeve's Automatic observations, a `### Fill check (Yahoo vs Zerodha)` table (rows also
   in `state/fill_check.csv`). Without a token each of these is one `Kite: no token ...` line instead.

## Phase 1 as built (7 Nov 2026)

- `papertrade/kite.py`: `available()` (token present and `profile()` works, once per run; why not in
  `kite.reason`), `instruments(exchange)` (cached per day in `data_cache/`), `ohlc_day(symbols, day)`,
  `chain_snapshot(sym, kind, expiries, spot)`, `basket_margin(legs)` (`consider_positions=False`, so your
  real positions do not change the number). Pacing: historical 3/s, quote 1/s and 500 instruments a call.
- `papertrade/fno.py` `load_chains`: Kite first for the same expiries as the NSE path, NSE still scraped; the
  Zerodha snapshot owns `state/chains/<SYM>.json` when it is of the market date and has two-sided quotes,
  else NSE does. `papertrade/fillcheck.py`: the fill check. `papertrade/candidates.py`: the margin column.
- `.github/workflows/daily.yml` passes `KITE_API_KEY` and `KITE_ACCESS_TOKEN` to the "Run the daily cycle"
  step only. `state/fill_check.csv` is committed with the rest of `state/`.
- **Not yet run against real Kite data** (no app or token existed when it was built). Tested: the no-token
  run (journal identical to the old code except the `Kite:` lines), and the token path with a fake client
  fed with real NSE chain numbers. Unknowns until the first real run: whether Kite's depth is still filled
  at 17:33 IST (if the book is empty after the close, the NSE chain is used that day and the journal says
  so), the exact basket-margin response, and whether the automatic login flow still matches Kite's pages.

## What this does NOT do

- No orders, ever, from this code. No static IP. No change to any sleeve's rules or fills in phase 1.
- Nothing for the US sleeve (Kite has no US data); DEFERRED item 4 stays open.
- The cloud routines (daily reviewer, scanner, strategist) do not get the token and do not need it.
