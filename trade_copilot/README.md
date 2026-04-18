# Binance Futures Trade Copilot

A small local command-line copilot for Binance USD-M futures.

It scans high-volatility perpetual futures, applies a simple judgement filter, sizes orders against the actual futures account balance, and can place a guarded live trade with protective exits.

This is not an investment system, financial advice, or a promise of profit. It is a risk-controlled execution helper for tiny, high-risk futures experiments.

## What It Does

- Scans Binance USD-M perpetual futures using public market-data endpoints.
- Scores short-term momentum using 15m, 30m, 1h, and 4h tape.
- Rejects setups that are too extended, too crowded by funding, too low-score, or against the immediate tape.
- Reads the futures account before arming a trade.
- Refuses impossible tickets when available margin cannot meet Binance minimum notional rules.
- Uses a local futures symbol whitelist through `ALLOWED_SYMBOLS`.
- Places live orders only with `--live` and an exact typed confirmation.
- Can run a long-lived `watch` loop that waits for rare clean setups.
- Uses isolated margin, sets leverage, opens a market entry, then places reduce-only stop-market and take-profit-market exits.

## Safety Model

The copilot is intentionally conservative about execution mechanics:

- No Binance login, password, 2FA code, or seed phrase is ever needed.
- API keys are read only from local environment variables or `.env`.
- `.env` is ignored by git.
- Live orders outside `ALLOWED_SYMBOLS` are refused.
- The account must be in One-way Mode.
- Multi-Assets Mode must be off.
- Live mode checks available futures margin before attempting entry.
- Watch mode does not stack risk: if an allowed symbol already has a position or open order, it waits.
- Watch mode has confirmation streaks, cooldowns, and daily trade caps.
- If exit-order placement fails after entry, the script attempts a reduce-only emergency market close.

Use a restricted Binance API key:

```text
Enable Reading: on
Enable Futures: on
Enable Withdrawals: off
Enable Internal Transfer: off
Enable Universal Transfer: off
Enable Spot & Margin Trading: off unless you explicitly need it elsewhere
IP restriction: on, restricted to your current public IP or trading host
```

## Requirements

- Python 3.11 or newer
- A Binance account with USD-M Futures enabled
- Classic Trading account mode
- One-way futures position mode
- Multi-Assets Mode disabled
- USDT available in the USD-M Futures wallet

The project currently uses only the Python standard library. `requirements.txt` is present so dependencies can be added cleanly later.

## Setup

```powershell
git clone <your-repo-url>
cd trade_copilot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env
notepad .env
```

For Git Bash on Windows:

```bash
python -m venv .venv
source .venv/Scripts/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` with your restricted Binance API key:

```text
BINANCE_API_KEY=your_key_here
BINANCE_API_SECRET=your_secret_here
BINANCE_FAPI_BASE_URL=https://fapi.binance.com
ALLOWED_SYMBOLS=RAVEUSDT,HIGHUSDT,ALICEUSDT,PORTALUSDT,SIRENUSDT
```

Do not commit `.env`.

## Commands

### Auth Check

```bash
python trade_copilot.py auth
```

Checks that `.env` is present, prints the current public IP, verifies signed Binance futures access, checks One-way/Multi-Assets status, and shows available USDT margin.

You want to see signed futures reads succeed before attempting live mode.

### Scan

```bash
python trade_copilot.py scan --limit 8 --candidates 40
```

Scans active USD-M perpetuals and prints the highest-scoring symbols.

This command does not need API keys.

### Judge

```bash
python trade_copilot.py judge
```

Scores only `ALLOWED_SYMBOLS`, applies rejection rules, checks available account margin, and prints either:

```text
ACTION: WAIT
```

or:

```text
ACTION: ARM THIS TICKET
```

If the account is too small for a candidate at the selected leverage, the candidate is rejected before live mode.

Useful options:

```bash
python trade_copilot.py judge --margin 5 --leverage 15 --target-pct 6.8 --stop-pct 3 --reserve 0.5
```

`--reserve` leaves some USDT unused when account-aware sizing is available.

### Watch

```bash
python trade_copilot.py watch
```

Runs the judgement layer in a loop. By default this is dry-run only: it prints `ACTION: WAIT` or `ACTION: DRY SIGNAL` but does not place orders.

Useful dry-run example:

```bash
python trade_copilot.py watch --interval 60 --min-score 25 --confirmations 2 --max-trades-per-day 3 --reserve 0.5
```

For a one-cycle test:

```bash
python trade_copilot.py watch --max-cycles 1 --interval 10
```

Live autonomous mode is explicit:

```bash
python trade_copilot.py watch --live-auto --interval 60 --min-score 25 --confirmations 2 --max-trades-per-day 3 --cooldown 900
```

`--live-auto` places orders without the typed confirmation used by `place --live`. Use it only with a restricted API key, a tight `ALLOWED_SYMBOLS` list, and money you can afford to lose.

Watch gates:

- The best setup must pass all judgement filters.
- The score must be at least `--min-score`.
- The same symbol and side must pass for `--confirmations` consecutive cycles.
- Existing allowed-symbol positions or open orders block new trades.
- The daily trade cap must not be reached.
- The cooldown must be finished.
- Account margin must fit Binance minimum notional rules.

### Levels

```bash
python trade_copilot.py levels RAVEUSDT
```

Prints short-term levels, VWAP, approximate 5m ATR, returns, highs/lows, and range position.

### Dry-Run Ticket

```bash
python trade_copilot.py place PORTALUSDT SHORT --margin 5 --leverage 15 --target-pct 6.8 --stop-pct 3
```

Without `--live`, this prints the order ticket only.

### Live Trade

```bash
python trade_copilot.py place PORTALUSDT SHORT --margin 5 --leverage 15 --target-pct 6.8 --stop-pct 3 --live
```

Live mode requires typing an exact confirmation phrase:

```text
PLACE PORTALUSDT SHORT 5
```

The live flow is:

1. Check local symbol whitelist.
2. Check available futures margin and resize/refuse if needed.
3. Check One-way Mode.
4. Check Multi-Assets Mode is off.
5. Set isolated margin.
6. Set leverage.
7. Place market entry.
8. Recalculate exits from actual fill price when Binance returns one.
9. Place reduce-only stop-market and take-profit-market exits.

## Judgement Rules

The scoring model is intentionally simple and transparent. It favors strong short-term continuation and rejects obvious traps:

- Rejects low score setups.
- Rejects longs when 15m tape is strongly against the long.
- Rejects shorts when 15m tape is strongly against the short.
- Rejects longs too close to the 1h high.
- Rejects shorts too close to the 1h low.
- Rejects shorts with extreme negative funding because crowded shorts can squeeze violently.
- Rejects longs with high positive funding because crowded longs can unwind.
- Rejects any setup the account cannot legally fit under Binance minimum notional rules.

The goal is not to trade constantly. `ACTION: WAIT` is a valid output.

## Account Sizing

Binance often requires at least `5 USDT` notional per futures order. A small account can fail even when the signal is valid.

Example:

```text
available margin: 0.1776 USDT
15x notional: 2.664 USDT
result: below 5 USDT minimum notional
```

In that situation the copilot refuses to arm the trade. It also prints the minimum margin needed by symbol at the symbol's max bracket leverage when account sizing blocks execution.

## Troubleshooting

### `-2015 Invalid API-key, IP, or permissions`

Common causes:

- `.env` contains an old or deleted key.
- Futures permission is not enabled on the Binance API key.
- The key was not saved after permission changes.
- The key IP whitelist does not include the current public IP printed by `auth`.
- The key belongs to a different account or sub-account.

Run:

```bash
python trade_copilot.py auth
```

### `-4168 Unable to adjust to isolated-margin mode under the Multi-Assets mode`

Turn off Multi-Assets Mode in Binance USD-M Futures settings.

The copilot uses isolated margin and will not bypass this with cross margin.

### `-2019 Margin is insufficient`

The futures wallet does not have enough available USDT for the requested margin/notional.

Run:

```bash
python trade_copilot.py auth
python trade_copilot.py judge --reserve 0.03
```

Then transfer more USDT into the USD-M Futures wallet or use a smaller account-feasible setup.

### Hedge Mode Error

Switch USD-M Futures to One-way Mode. The copilot does not currently support Hedge Mode.

## Public Repo Checklist

Before pushing:

```bash
git status
git diff -- . ':!.env'
```

Confirm:

- `.env` is not tracked.
- `.venv/` is not tracked.
- No API key, API secret, account identifier, or screenshot with credentials is committed.
- `.env.example` contains placeholders only.

## License

MIT License. See `LICENSE`.
