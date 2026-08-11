-- Apply after configuring Supabase pg_cron, pg_net, and Vault. Real URLs and
-- secrets are operator-managed values and must never be committed here.
-- Required Vault names: sol_alerts_scan_url, sol_alerts_delivery_url,
-- sol_alerts_cron_secret. Optional database settings can override the URLs:
-- app.settings.sol_alerts_scan_url and app.settings.sol_alerts_delivery_url.
-- Supabase's Vault extension is named supabase_vault and installs its API in
-- the vault schema. Keep this idempotent for projects where Vault is already enabled.

create extension if not exists pg_cron;
create extension if not exists pg_net;
create extension if not exists supabase_vault with schema vault;

do $do$
begin
  if exists (select 1 from pg_proc where proname = 'schedule') then
    if exists (select 1 from cron.job where jobname = 'sol_alerts_scan') then
      perform cron.unschedule('sol_alerts_scan');
    end if;
    perform cron.schedule(
      'sol_alerts_scan',
      '1,16,31,46 * * * *',
      $cron$
        select net.http_post(
          url := coalesce(current_setting('app.settings.sol_alerts_scan_url', true), (select decrypted_secret from vault.decrypted_secrets where name = 'sol_alerts_scan_url')),
          headers := jsonb_build_object('Content-Type', 'application/json', 'x-cron-secret', (select decrypted_secret from vault.decrypted_secrets where name = 'sol_alerts_cron_secret')),
          body := '{}'::jsonb
        );
      $cron$
    );
    if exists (select 1 from cron.job where jobname = 'sol_alerts_delivery') then
      perform cron.unschedule('sol_alerts_delivery');
    end if;
    perform cron.schedule(
      'sol_alerts_delivery',
      '* * * * *',
      $cron$
        select net.http_post(
          url := coalesce(current_setting('app.settings.sol_alerts_delivery_url', true), (select decrypted_secret from vault.decrypted_secrets where name = 'sol_alerts_delivery_url')),
          headers := jsonb_build_object('Content-Type', 'application/json', 'x-cron-secret', (select decrypted_secret from vault.decrypted_secrets where name = 'sol_alerts_cron_secret')),
          body := '{}'::jsonb
        );
      $cron$
    );
  end if;
end;
$do$;
