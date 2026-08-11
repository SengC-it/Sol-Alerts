# Disaster recovery

The durable source of truth is Supabase migrations plus the versioned config/evidence/weight exports. Keep encrypted, access-controlled backups of Supabase database dumps and the active config/weight JSON; do not back up secrets in the repository.

## Recovery order

1. Freeze scans and email delivery.
2. Restore a new Supabase project from the migration files and an approved database backup.
3. Restore the active config in `sol_app_configurations`, evidence in `sol_strategy_evidence`, champion weights in `sol_weight_versions`, rollback pointer, `sol_outbox` status, and `sol_health_events` history. Verify RLS and function grants before exposing the service role.
4. Recreate Vercel server-only environment variables and Vault/pg_cron references.
5. Run offline checks, then `/api/health`, then one controlled scan with a non-production recipient.
6. Resume only after candle, depth, risk-overlay, write, and outbox invariants are validated.

Expired outbox rows are intentionally not replayed. A missing or ambiguous historical input is reported as a data gap and cannot be silently filled by a future-looking value.
