# rhtrader — trend-following bot for Robinhood

A small, testable trading bot for US stocks and ETFs on Robinhood:

- **Strategy:** moving-average crossover. Long a symbol while its 50-day SMA
  is above its 200-day SMA; flat otherwise. Each symbol gets an equal slice
  of the account.
- **Backtester:** signals on each close, fills at the **next** day's open
  plus slippage, so it cannot see the future.
- **Paper trading:** a simulated account saved to `state/paper.json`.
- **Live trading:** through Robinhood's official **Agentic Trading** MCP server,
  signed in with **OAuth** (no username or password), behind three separate
  opt-ins, with risk limits and a kill switch.

> **Read this first.** The bot can only trade in the one account you enable
> for agentic trading in the Robinhood app, and Robinhood notifies you of
> every agent trade. This is not investment advice. Past backtest results
> don't predict future returns.

## Quick start

```bash
pip install -e ".[dev]"          # includes the MCP/OAuth client
python -m pytest

python -m rhtrader backtest                      # SPY + QQQ, 50/200 SMA
python -m rhtrader backtest --fast 20 --slow 100 --trades
python -m rhtrader trade                         # dry run: show what it would do
python -m rhtrader trade --execute               # fill orders in the paper account
python -m rhtrader status                        # paper account summary
```

`data/SPY.csv` and `data/QQQ.csv` hold split-adjusted daily bars from
2016-01-04 to 2026-09-22, pulled from Robinhood.

### Backtest on the included data (defaults: $10k, 5 bps slippage, whole shares)

| SMA 50/200, SPY + QQQ, 2016-10 → 2026-09 | Strategy | Buy & hold |
|---|---:|---:|
| Total return | 262.5% | 402.0% |
| CAGR | 13.9% | 17.7% |
| Volatility | 16.5% | 20.4% |
| Max drawdown | −29.9% | −31.3% |
| Sharpe | 0.87 | 0.90 |
| Trades | 18 | — |

In a decade that mostly went up, the crossover **lagged buy-and-hold** and
barely reduced the worst drawdown. The 50/200 cross is too slow to dodge
sharp crashes like March 2020. It cut volatility, and it would help most in
long, slow bear markets. Test other windows and symbols before trusting it.
Both columns use price returns only, without dividends.

## Connecting to Robinhood (OAuth)

rhtrader talks to Robinhood's Agentic Trading MCP server
(`https://agent.robinhood.com/mcp/trading`) and signs in with OAuth 2.1 + PKCE.
You never give it your username or password.

1. In the Robinhood app, turn on **Agentic Trading** for an account and fund
   it. Keep only what you're willing to let the bot trade in it.
2. Authorize rhtrader once:
   ```bash
   python -m rhtrader login
   ```
   This opens Robinhood's sign-in page in your browser. After you approve,
   Robinhood redirects to `http://127.0.0.1:8765/callback`, and rhtrader saves
   the tokens to `~/.config/rhtrader/oauth.json` (readable only by you). The
   command then lists your accounts and marks the one the bot can trade.
3. Put that account number in `config.toml` under `[robinhood]`.

Later runs, including scheduled ones, refresh the access token silently.
If the authorization is revoked or can't be refreshed, commands exit with
`run rhtrader login` instead of hanging on a browser prompt. To revoke
access, disconnect the agent in the Robinhood app and delete the token file.

Every live order first goes through Robinhood's pre-trade review
(`review_equity_order`). If the review returns **any** alerts, such as
buying power or a trading halt, the order is skipped and the alerts are
logged. Orders carry an idempotency key (`ref_id`), so a retried request
can't create a duplicate order.

## Configuration

Copy `config.example.toml` to `config.toml` and pass `-c config.toml`. Every
key is documented in the example. `config.toml` is git-ignored.

## How a trading cycle works (`rhtrader trade`)

1. Load daily bars (CSV or Robinhood), dropping today's bar if the market
   hasn't closed yet. Skip any symbol whose data is older than
   `max_data_age_days`.
2. Compute each symbol's signal from its last completed bar.
3. Skip symbols that already have an open order.
4. **Exit** any held symbol whose signal is off. **Enter** any symbol whose
   signal is on and isn't held, sized at `equity × weight` and capped by
   available cash.
5. Run risk checks, then submit the orders, or just print them in a dry run.
6. Append everything to `state/trades.jsonl`.

The cycle is idempotent. Running it twice in one day places no duplicate
orders. Run it once per trading day **after the 4pm ET close**, for example
with cron:

```cron
# 4:30pm ET (adjust for your server's timezone), Mon–Fri
30 16 * * 1-5  cd /path/to/repo && python -m rhtrader -c config.toml trade --execute >> state/cron.log 2>&1
```

Orders use a day time-in-force (`gfd`), so orders placed after the close queue
for the next session. That matches the backtest's next-open fills.

## Scheduled cloud mode (no Mac needed)

If you'd rather not run rhtrader on your own computer, a scheduled Claude
session can drive it through Claude's Robinhood connection:

1. Claude calls only read-only Robinhood tools (`get_equity_historicals`,
   `get_equity_quotes`, `get_portfolio`, `get_equity_positions`,
   `get_equity_orders`) and saves each JSON response to `<dir>/<tool>.json`.
2. `rhtrader plan --snapshot <dir>` runs the strategy and all risk checks on
   that data and prints the orders, as exact `place_equity_order` arguments.
   The planner refuses to call any order tool itself.
3. In dry-run mode the session just reports the plan. `--assume-cash 10000`
   sizes orders as if the account held that much, which is useful before
   you fund it.

The model never chooses trades; it only moves data and reports. As an extra
guard against bad or mistyped data, any symbol whose price is more than
`max_price_gap_pct` (10%) away from its last close is skipped.

## Safety rails

| Guard | Default | Behavior |
|---|---|---|
| Dry run | on | `trade` only prints orders unless you pass `--execute` |
| Live gate | off | Real orders need `broker.mode = "live"` **and** `--execute` **and** `--live` |
| Kill switch | `KILL` file | `touch KILL` blocks all new orders; `rm KILL` resumes |
| Max order size | $5,000 | Larger buys are clipped to this. Exits are never capped |
| Max orders per run | 10 | More than this halts the whole run, since it suggests a bug |
| No shorting | always | Sells larger than the held position are rejected |
| No margin | always | Buys are sized from the smaller of cash and buying power |
| Limit orders | 0.20% buffer | Whole-share orders are limit orders near the last price |
| Stale data | 5 days | Symbols with old data are skipped |
| Universe | config | Orders for symbols outside `symbols` are rejected |
| Price sanity | 10% | Symbols priced more than 10% from their last close are skipped |

## Going live (suggested path)

1. Backtest your symbols and parameters. Understand the drawdowns.
2. Run `rhtrader login`, set `data.source = "robinhood"`, and paper trade for
   a few weeks with `trade --execute`. Check `state/trades.jsonl` every day.
3. Set `broker.mode = "live"` and `robinhood.account_number`, start with a
   small `max_order_notional`, and run `trade --live` (a dry run against your
   real account) to check the orders.
4. Only then, schedule `trade --execute --live`.

## Layout

```
rhtrader/
  strategy.py          SMA crossover signals
  backtest.py          next-open fill simulator
  metrics.py           CAGR, Sharpe, drawdown
  trader.py            one live/paper trading cycle
  risk.py              pre-trade checks and kill switch
  broker.py            PaperBroker, RobinhoodBroker
  data.py              CSV / Robinhood bar loading
  robinhood_mcp.py     OAuth + MCP client for Robinhood Agentic Trading
  snapshot.py          plan orders from saved Robinhood tool responses
  config.py            TOML config
  cli.py               command-line entry point
```
