# Deployment procedure (operator authorization required)

This repository contains preparation artifacts only. Do not execute deployment steps until the primary task authorizes the external phase.

1. Create a Vercel Node project from the repository. Set only server-side environment variables from `.env.example`; never create a `NEXT_PUBLIC_` service-role variable.
2. Create a Supabase project, apply migrations in order, enable `pg_cron`, `pg_net`, and Vault, insert the active config/evidence/weight rows, and initialize `global_risk` once. Locally, set only `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` and run `sol-alerts-bootstrap-risk --cache-dir .cache/risk-bootstrap --output .cache/risk-bootstrap/risk-v1.json --publish`; alternatively dispatch `.github/workflows/bootstrap-risk.yml` with `publish=true` and an optional canonical UTC cutoff. The initializer uses only official all-market exchangeInfo/ticker data and closed klines, and fails closed unless every static-eligible 90-day symbol has 8,641 aligned bars including BTCUSDT. Do not proceed if the baseline is absent or malformed.
3. Store the Vercel scan URL, delivery URL, and cron secret in Vault. Configure the database settings or Vault names referenced by `202608050002_schedule.sql`.
4. Create a Gmail app password for the dedicated sender account and set the Vercel SMTP variables. Do not use a normal Gmail password.
5. Run the local checks and a controlled endpoint smoke test. Confirm the first scan commits a run with no secrets in diagnostics, then confirm an outbox row is delivered or retried.
6. Configure the guarded weekly publish environment with only `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` scoped to that job. Enable weekly research only after the repository secret/cache policy, official active/delisted manifest, funding coverage, and migration execution have been reviewed.

No cloud project, provider resource, Gmail account, remote repository, deployment, or PR is created by the local implementation task.

The repository includes a deterministic database-boundary harness, but it cannot replace applying these migrations to a real Supabase Postgres instance. That external migration and provider smoke test is pending explicit deployment authorization.
