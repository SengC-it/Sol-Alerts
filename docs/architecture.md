# Architecture and invariants

The system has four narrow boundaries:

1. `python/sol_alerts` consumes official historical market files and emits deterministic evidence/config preparation. Scheduled research first runs `sol_alerts.prepare_data`, which stores only compressed raw klines/funding plus dated point-in-time manifests in the GitHub Actions cache (10 GB default bound); shards always receive explicit `--input` and `--funding` paths and cannot fall back to fixtures.
2. `src` contains pure strategy, scoring, gating, risk-overlay, portfolio, and lifecycle functions plus thin Binance/Supabase/SMTP adapters.
3. Vercel handlers construct adapters only at request time. The Supabase service-role key is read from server-only environment variables and never reaches a browser or a `NEXT_PUBLIC_` variable.
4. Supabase SQL functions perform durable state changes. `sol_commit_scan` inserts scan run, signals, deduplication state, and outbox content in one transaction. Weight publication and rollback are separate atomic functions.

## Scan sequence

1. Validate the cron secret using a constant-time comparison.
2. Load and validate the compatible strategy config. A schema/config mismatch fails closed.
3. Discover Binance instruments dynamically and apply `PERPETUAL` plus USDT quote/margin/status filters; `deliveryDate` is lifecycle metadata only.
4. Batch-fetch tickers and premium-index mark prices, apply cheap quote-volume/spread gates, evaluate every static-eligible symbol with bounded concurrency/deadline guards, and request entry-side depth only after a directional raw candidate exists. A budget or symbol-data failure aborts the scan before state invalidation.
5. Use only closed 15-minute bars; derive closed 1-hour bars from complete four-bar groups.
6. Classify the market with EMA20/EMA50, ADX14, ATR14 and the fixed ADX threshold grid. Ambiguous states emit no candidates.
7. Evaluate only the three configured strategy families and fixed parameter grids for long and short separately.
8. Compute technical invalidation, a distinct volatility-buffered reference stop, target, after-cost R:R, and the solved chase boundary.
9. Apply evidence/sample gates, the globally selected family-direction fixed parameter map, published global and sample-size-shrunk symbol weights, score thresholds, conflict suppression, deduplication/cooldown, published 90-day correlation/global-risk baselines, positive-correlation clusters, global extreme-return pause, and portfolio capacity.
10. Reconcile pending/open simulated positions from newly closed bars, apply each public Binance funding event once using a per-position watermark, reserve formal capacity by ranked score, and commit formal/shadow simulation, invalidations, risk observations, and qualified outbox content in one durable transaction. Shadow positions use the same price lifecycle but do not affect formal capacity or equity. Only qualified signals create a single idempotent email after the commit succeeds.

## Failure behavior

Missing/stale/inconsistent candles, incomplete depth, mark anomalies, failed calculations, and failed durable writes do not create alerts. Two consecutive failures create one durable health event and a validated recovery event follows only after a successful data-and-write cycle. A failed SMTP send leaves the outbox pending with approximately 1/3/5-minute retry delays; expired rows are never sent.
