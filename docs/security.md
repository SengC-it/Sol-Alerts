# Security model

- `SUPABASE_SERVICE_ROLE_KEY`, `CRON_SECRET`, Gmail username/app password, and recipient are server-only environment/secret values. `.env.example` contains names and descriptions only.
- No secret is accepted from a query string or request body. The scan and delivery handlers require a constant-time header comparison.
- Supabase RLS is enabled on every public table. No `anon` or `authenticated` table/function access is granted. Only the server-side service-role path reaches the RPCs.
- Errors returned to HTTP clients are generic. Adapter errors truncate and redact secret values before persistence.
- SMTP headers reject CR/LF injection. Outbox idempotency and row locking prevent duplicate delivery under concurrent workers.
- GitHub workflows use read-only permissions, immutable action SHAs where practical, bounded concurrency, and no secrets in artifacts. Dependabot is configured for visibility without automatic breaking upgrades.

This is an engineering boundary, not a legal, financial, statistical, or operational guarantee. Rotate provider credentials and review provider audit logs after deployment.
