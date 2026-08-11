# Sol Alerts

Sol Alerts is a research-first personal decision-support system for Binance USDT-M perpetual contracts. It runs deterministic historical research, publishes versioned evidence and weights, scans closed 15-minute candles, and sends one aggregated email through Gmail SMTP. It never reads a Binance account and never places orders.

> **Trading-risk disclaimer:** This software is not investment advice, a recommendation, a solicitation, a guarantee of profit, or a substitute for independent risk management. A signal is a research ranking, never a probability or instruction. Perpetual contracts can lose all collateral quickly. Do not use leverage based on this project; keep any liquidation level safely beyond the reference stop. The email deliberately does not calculate user position size or liquidation.

## Scope and safety boundary

- Public Binance market endpoints only: exchange metadata, tickers, order-book depth, klines, mark price, and (when available in research inputs) public funding data.
- USDT-quoted, USDT-margined, `PERPETUAL` contracts with the required status are eligible. Binance's `deliveryDate` is retained as lifecycle metadata and is not used as a perpetual gate (live Binance perpetuals can expose a future timestamp). USDC, coin-margin, account, balance, order, news, social, on-chain, liquidation-map, and ML/AI price-prediction inputs are excluded.
- The live surface is three server-only endpoints: `/api/health`, `/api/scan`, and `/api/deliver`. There is no dashboard or authentication UI.
- This checkout contains implementation and deployment-preparation artifacts. It has not created cloud projects, configured Gmail, or deployed anything; the real Supabase migration run and provider smoke test remain operator-authorized checks.

## Local verification

Node 20+ and Python 3.12 are required. Dependencies are pinned by `package-lock.json` and `pyproject.toml`.

```text
npm ci
npm run check
npm run build
python -m pip install -e ".[dev]"
python -m pytest -q
python -m sol_alerts.backtest --smoke
```

The smoke command uses deterministic in-repository generated fixtures. It never calls Binance, Supabase, Vercel, Gmail, or a secret. The smoke report is written under `reports/`, which is ignored by Git.

For an operator-supplied official history, `sol-alerts-prepare-data --cache-dir .cache/market-data --start <UTC> --end <UTC>` incrementally fetches only missing per-symbol kline/funding head/tails, atomically merges compressed files, and always writes a truthful `prepare-report.json`. An incomplete current snapshot or funding/data gap returns `formal_ready=false` and keeps the weekly workflow successful in accumulation mode; selection, formal shards, aggregation, publication, and formal email eligibility remain gated off. When formal-ready, four disjoint selection shards emit compact train/validation sufficient statistics, one merge job selects the global fixed-grid keys, and formal shards consume that selection. A formal run must pass the generated provenance with `python -m sol_alerts.backtest --input <directory> --funding <funding-directory> --prepare-report .cache/market-data/prepare-report.json`; arbitrary folders cannot be labeled formal. A current exchangeInfo snapshot is not 24-month point-in-time/delisted-universe evidence, so preparation remains fail-closed until the dated archive coverage supports that claim. `sol-alerts-bootstrap-risk` seeds the validated 90-day BTC/market baseline. Only `--smoke` enables deterministic fixtures; the guarded weekly workflow is the only path that can atomically publish a passing weight/config version.

## Architecture

```text
GitHub Actions (research + checks)
        | versioned config/evidence/weights
        v
Supabase (RLS state, atomic scan commit, outbox, cron/pg_net; all project-owned public tables and RPCs use the `sol_` prefix)
        | secret-protected POST at UTC :01/:16/:31/:46
        v
Vercel Node runtime -> Binance public market data
        | closed candles + live liquidity/depth gates
        v
Supabase outbox -> Gmail SMTP app password -> one aggregated email
```

See [architecture](docs/architecture.md), [operations](docs/operations.md), [data limitations](docs/data-limitations.md), [statistical methodology](docs/statistical-methodology.md), [security](docs/security.md), [deployment](docs/deployment.md), and [disaster recovery](docs/disaster-recovery.md).

## Repository layout

- `src/`: pure indicators, three strategy families, score, gates, lifecycle, portfolio simulation, and adapters.
- `api/`: Vercel-compatible server-only handlers.
- `python/sol_alerts/`: deterministic downloader-input boundary, features, walk-forward backtest, portfolio simulation, bootstrap evidence, and weight evaluation.
- `config/`: the shared versioned machine-readable strategy schema and active v1 configuration.
- `supabase/migrations/`: schema, RLS, atomic scan/outbox functions, weight publication/rollback functions, and cron placeholders.
- `.github/workflows/`: least-privilege checks, sharded weekly research, report aggregation, and a separately scoped guarded publication job.

## Cost and data retention

The intended deployment uses free/zero-fixed-cost tiers: GitHub Actions for bounded research, Supabase for bounded state and scheduling, Vercel for serverless execution, and Gmail SMTP. Years of raw klines are not stored in Supabase. GitHub Actions caches compressed research inputs but does not commit or artifact full raw history. Operators must monitor provider quotas and retention limits; “zero fixed monthly cost” is an architecture target, not a provider guarantee.
