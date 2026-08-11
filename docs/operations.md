# Operations runbook

## Normal checks

- Confirm `/api/health` returns `ok: true` and the expected config version.
- Confirm the four scan jobs run at UTC minutes 01, 16, 31, and 46, and delivery runs every minute.
- Inspect `sol_scan_runs`, `sol_health_events`, `sol_outbox`, and `sol_delivery_attempts` for stale sending rows, repeated failures, or expired signals.
- Review the latest strategy evidence and active weight version before the guarded weekly job. The scheduled job performs global family-direction selection on train/validation evidence, merges all shard events, and persists failed lifecycle evaluations as well as passing publications; a missing champion only permits the explicit bootstrap path after the historical gates pass.

## Research cache and formal gate

The weekly research workflow keeps only compressed, per-symbol rolling inputs under `.cache/market-data`. Kline and funding files are merged from validated head/tail ranges, deduplicated by timestamp, pruned to the requested availability window, and atomically replaced only after a complete fetch succeeds. The prepare job explicitly restores the newest compatible `rolling-v1` prefix, then saves the updated cache under a unique ISO-week/run/attempt key because GitHub cache entries are immutable. Its exact saved key is passed to every downstream job, so a same-week retry inherits the repaired generation rather than the first partial generation. The 10 GB working-cache limit, per-symbol pruning, and GitHub cache quota/eviction bound retained immutable generations; raw market history is never committed or uploaded as an artifact.

The preparation job always writes `prepare-report.json`. A missing historical manifest, funding gap, or other incomplete provenance produces `formal_ready=false`, reports the exact gaps, and ends the accumulation workflow successfully. Selection, formal shards, aggregation, and guarded publication are skipped until a later preparation report is formally ready; no incomplete cache can produce formal alerts or publication.

## Champion/Challenger replay boundary

The selection job freezes one non-secret evaluation-descriptor artifact before any untouched-test evaluation. It contains the selected fixed parameters, global and symbol weights, modes, provenance, and a deterministic descriptor hash. The protected production job obtains the active Champion descriptor from Supabase server-side; formal shards replay that exact Champion and the frozen Challenger on the same prepared cutoff and untouched calendar block universe, including zero-return blocks with no accepted trade. Publication verifies exact descriptor content and hashes, rejects block-universe mismatches, and stores the exact descriptor/hash in the weight version and research report. A failed or stale/mutated descriptor is rejected without replacing the Champion. Push validation never receives Supabase secrets; only the scheduled run or an explicitly enabled protected workflow dispatch can fetch or publish production state. Setting the protected `bootstrap_champion` input alone enables the one-time initial path; ordinary scheduled runs never bootstrap implicitly.

## No-alert investigation

Check, in order: cron authorization, latest closed candle age, candle gaps, complete eligible-universe scan, dynamic instrument status, 24-hour USDT volume, spread, direction-specific 0.5% depth, mark anomaly, funding-event completeness/watermark, risk-overlay pause, system halt, evidence sample counts, net R:R, score, conflict/correlation/capacity suppression, then durable outbox state. Do not relax a gate to make an alert appear.

## Halt and rollback

If all strategy families fail their evidence gates, keep research/shadow mode running, send one halt notice through the outbox, and keep trade emails stopped until two consecutive valid weekly evaluations. Use `sol_rollback_weight_version` for an emergency version rollback. Emergency rollback is required when live simulated drawdown exceeds 1.5 times the active version's untouched-test drawdown. Statistical rollback requires at least 30 closed live simulated signals and a significantly negative paired difference.

## Email delivery

The scan transaction creates at most one digest key for a signal activation set, even when identical scans overlap. `/api/deliver` claims rows with row locks, reclaims stale sending leases, sends through Gmail SMTP, records delivery attempts, and marks expired rows without sending. A failed initial send is retried after approximately 1 minute, then 3 minutes, then 5 minutes; the fourth failed send is terminal and no further attempt is claimed. SMTP errors are stored in truncated form and must not contain credentials. SMTP cannot guarantee exactly-once delivery after a worker crashes immediately after sending; the durable idempotency key prevents ordinary duplicate generation.
