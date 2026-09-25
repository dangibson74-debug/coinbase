# Momentum experiment bot

A small, rule-fixed crypto momentum experiment on the Coinbase **Momentum experiment** portfolio
(`a1675951-fc70-4129-b2a5-dfa42d872653`). Runs on GitHub Actions, no server needed.

## Strategy (fixed, see `config.json`)

- Universe: BTC-GBP, ETH-GBP, LINK-GBP, plus GBP cash. Spot only, no leverage.
- Daily check after 20:00 London. The signal is each coin's 7-day return (current price vs the hourly close 7 days earlier).
- Hold the coin with the highest positive 7-day return. Hold GBP if all three are negative.
- Switch only if the target beats the current holding by at least 3 percentage points (GBP = 0%). Max one switch per day.
- Pause: switched off (`"pause_enabled": false`). When switched on, a value of GBP 20.30 or below sells to GBP and pauses for 7 days; trading resumes only after approval (see below), and the pause triggers once per experiment.
- End: at a value of GBP 8 or below, sell to GBP and stop permanently.
- Duration: 28 days from the first **live** check, then checks stop and the position is kept.

## Safety guards

- **Live trading needs two switches:** `"dry_run": false` in `config.json` AND the repository variable `LIVE_TRADING` set to `enabled` (Settings, then Secrets and variables, then Actions, then Variables). Either one alone means a dry run.
- On every run the bot checks the API key itself. It halts if the key has Transfer permission or is not scoped to the experiment portfolio.
- Hard cap: the bot halts if the portfolio value exceeds GBP 70. It also never places an order above GBP 70.
- It refuses to trade if its computed value differs from Coinbase's reported total by more than 10%.
- It halts on any unexpected asset in the portfolio, any rejected or unfilled order, or any error after an order has been placed.
- Mock tests run before every bot run. If any test fails, the bot does not run.
- Dependencies are pinned with hashes, and GitHub actions are pinned to commit SHAs.

## Files

| File | Purpose |
|---|---|
| `bot.py` | The strategy |
| `config.json` | All rules as parameters |
| `state.json` | Status, activation date, pause state, last check and switch dates |
| `log.csv` | Every signal, decision, order, fill, fee and disposal (keep for CGT records) |
| `tests/test_scenarios.py` | 25 mock-scenario tests: `python -m unittest discover -s tests -v` |
| `.github/workflows/momentum.yml` | Daily schedule plus a manual Run workflow button |
| `.github/workflows/tests.yml` | Re-runs the tests on every code change |

## Secrets and variables (Settings, then Secrets and variables, then Actions)

- Secrets: `MOM_BOT_API_KEY_NAME`, `MOM_BOT_API_PRIVATE_KEY`
- Optional secret: `NTFY_TOPIC` (phone alerts). Without it, check `state.json` and `log.csv` in the repo, and the Actions tab (a failed run shows red and GitHub emails you).
- Variable (only when approved for live): `LIVE_TRADING` = `enabled`

## Operating

- **Manual run:** Actions, then momentum-bot, then Run workflow. A manual run does not use up the day's scheduled check.
- **Resume after a pause** (only if the pause is switched on): once the 7 days are up, edit `state.json` and set `"pause_acknowledged": true`.
- **Clear a halt** (`halted_error` / `halted_cap`): investigate first, then set `"status": "active"` and `"halt_reason": null` in `state.json`.
- **Emergency stop:** set the `LIVE_TRADING` variable to anything other than `enabled`, or disable the workflow (Actions, then momentum-bot, then the ... menu, then Disable workflow). To revoke access entirely, delete the API key in Coinbase Developer Platform.

`status` values: `active`, `paused`, `ended` (end threshold), `complete` (4 weeks), `halted_cap`, `halted_error`.
