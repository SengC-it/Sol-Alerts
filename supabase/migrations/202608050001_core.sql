begin;

-- Supabase-managed extensions are installed in the dedicated extensions
-- schema.  Keep the schema explicit so SECURITY DEFINER functions can use a
-- qualified function name without widening their search_path.
create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;

create table if not exists public.sol_app_configurations (
  config_key text primary key,
  config_version text not null,
  config_value jsonb not null,
  is_active boolean not null default true,
  updated_at timestamptz not null default now()
);

create table if not exists public.sol_instruments (
  symbol text primary key,
  base_asset text not null,
  quote_asset text not null,
  margin_asset text not null,
  contract_type text not null,
  status text not null,
  onboard_date timestamptz,
  delivery_date timestamptz,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  historical_eligible boolean not null default false,
  live_eligible boolean not null default false,
  metadata jsonb not null default '{}'::jsonb,
  check (quote_asset = 'USDT' and margin_asset = 'USDT' and contract_type = 'PERPETUAL')
);

create table if not exists public.sol_scan_runs (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null,
  completed_at timestamptz,
  config_version text not null,
  status text not null default 'completed' check (status in ('running', 'completed', 'failed', 'halted')),
  diagnostics jsonb not null default '{}'::jsonb,
  live_simulated_drawdown numeric not null default 0,
  created_at timestamptz not null default now()
);

create table if not exists public.sol_signal_states (
  state_key text primary key,
  symbol text not null references public.sol_instruments(symbol),
  strategy_family text not null check (strategy_family in ('trend_breakout', 'trend_pullback', 'range_mean_reversion')),
  direction text not null check (direction in ('long', 'short')),
  active boolean not null default false,
  last_triggered_at timestamptz,
  last_invalidated_at timestamptz,
  cooldown_until timestamptz,
  updated_at timestamptz not null default now()
);

create table if not exists public.sol_signals (
  id uuid primary key default gen_random_uuid(),
  scan_run_id uuid not null references public.sol_scan_runs(id),
  symbol text not null references public.sol_instruments(symbol),
  strategy_family text not null check (strategy_family in ('trend_breakout', 'trend_pullback', 'range_mean_reversion')),
  direction text not null check (direction in ('long', 'short')),
  parameter_key text not null,
  trigger_time timestamptz not null,
  current_price numeric not null,
  mark_price numeric not null,
  entry_low numeric not null,
  entry_high numeric not null,
  chase_boundary numeric not null,
  technical_invalidation numeric not null,
  reference_stop numeric not null,
  reference_target numeric not null,
  net_reward_risk numeric not null,
  score jsonb not null,
  evidence jsonb not null,
  confluence smallint not null check (confluence between 1 and 5),
  weight numeric not null,
  expires_at timestamptz not null,
  shadow_only boolean not null default false,
  idempotency_key text not null unique,
  created_at timestamptz not null default now()
);

create table if not exists public.sol_simulated_positions (
  id uuid primary key default gen_random_uuid(),
  signal_id uuid not null references public.sol_signals(id),
  symbol text not null references public.sol_instruments(symbol),
  direction text not null check (direction in ('long', 'short')),
  status text not null check (status in ('open', 'closed', 'shadow')),
  entry_time timestamptz,
  entry_price numeric,
  reference_stop numeric not null,
  reference_target numeric not null,
  risk_budget numeric,
  opened_at timestamptz,
  closed_at timestamptz,
  metadata jsonb not null default '{}'::jsonb
);

create table if not exists public.sol_simulated_trades (
  id uuid primary key default gen_random_uuid(),
  position_id uuid references public.sol_simulated_positions(id),
  signal_id uuid references public.sol_signals(id),
  symbol text not null,
  direction text not null check (direction in ('long', 'short')),
  entry_time timestamptz not null,
  entry_price numeric not null,
  exit_time timestamptz not null,
  exit_price numeric not null,
  exit_reason text not null check (exit_reason in ('stop', 'target', 'time', 'invalidation')),
  gross_r numeric not null,
  net_r numeric not null,
  funding_cost numeric not null default 0,
  shadow_only boolean not null default false,
  created_at timestamptz not null default now()
);

create table if not exists public.sol_strategy_evidence (
  id uuid primary key default gen_random_uuid(),
  symbol text not null,
  strategy_family text not null check (strategy_family in ('trend_breakout', 'trend_pullback', 'range_mean_reversion')),
  config_version text not null,
  evaluated_at timestamptz not null,
  history_days integer not null,
  formal_eligible boolean not null default false,
  untouched_test_trades integer not null,
  untouched_mean_r numeric not null,
  untouched_max_drawdown numeric not null,
  mean_r_lower_bound_95 numeric not null,
  robustness_score numeric not null,
  evidence jsonb not null default '{}'::jsonb,
  unique (symbol, strategy_family, evaluated_at)
);

create table if not exists public.sol_weight_versions (
  id uuid primary key default gen_random_uuid(),
  version integer not null unique,
  config_version text not null default '1.0.0',
  status text not null check (status in ('champion', 'challenger', 'rolled_back')),
  weights jsonb not null,
  source text not null,
  evidence_id uuid references public.sol_strategy_evidence(id),
  activated_at timestamptz,
  rollback_of uuid references public.sol_weight_versions(id),
  out_of_sample_max_drawdown numeric not null default 0.1,
  created_at timestamptz not null default now()
);

create unique index if not exists sol_one_champion_weight_version on public.sol_weight_versions(status) where status = 'champion';

create table if not exists public.sol_outbox (
  id uuid primary key default gen_random_uuid(),
  idempotency_key text not null unique,
  scan_run_id uuid references public.sol_scan_runs(id),
  recipient text not null,
  subject text not null,
  body_text text not null,
  status text not null default 'pending' check (status in ('pending', 'sending', 'delivered', 'failed', 'expired')),
  attempt_count integer not null default 0,
  next_attempt_at timestamptz not null default now(),
  expires_at timestamptz not null,
  claimed_at timestamptz,
  delivered_at timestamptz,
  last_error text,
  created_at timestamptz not null default now()
);

create table if not exists public.sol_delivery_attempts (
  id uuid primary key default gen_random_uuid(),
  outbox_id uuid not null references public.sol_outbox(id),
  attempt_number integer not null,
  attempted_at timestamptz not null default now(),
  status text not null check (status in ('sent', 'failed', 'expired')),
  error text
);

create table if not exists public.sol_health_events (
  id uuid primary key default gen_random_uuid(),
  event_type text not null check (event_type in ('scan_failure', 'scan_recovery', 'halt', 'rollback')),
  detail text not null,
  occurred_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create table if not exists public.sol_runtime_status (
  status_key text primary key,
  status_value jsonb not null,
  updated_at timestamptz not null default now()
);

-- Foreign-key indexes are kept next to the base tables for fresh installs.
-- Migration 005 repeats the same idempotent definitions for already-migrated
-- databases.
create index if not exists sol_signal_states_symbol_idx on public.sol_signal_states(symbol);
create index if not exists sol_signals_scan_run_id_idx on public.sol_signals(scan_run_id);
create index if not exists sol_signals_symbol_idx on public.sol_signals(symbol);
create index if not exists sol_simulated_positions_symbol_idx on public.sol_simulated_positions(symbol);
create index if not exists sol_simulated_trades_position_id_idx on public.sol_simulated_trades(position_id);
create index if not exists sol_simulated_trades_signal_id_idx on public.sol_simulated_trades(signal_id);
create index if not exists sol_weight_versions_evidence_id_idx on public.sol_weight_versions(evidence_id);
create index if not exists sol_weight_versions_rollback_of_idx on public.sol_weight_versions(rollback_of);
create index if not exists sol_outbox_scan_run_id_idx on public.sol_outbox(scan_run_id);
create index if not exists sol_delivery_attempts_outbox_id_idx on public.sol_delivery_attempts(outbox_id);

create or replace function public.sol_commit_scan(p_payload jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $function$
declare
  v_scan_id uuid;
  v_instrument jsonb;
  v_signal jsonb;
  v_email jsonb;
  v_expires_at timestamptz;
  v_outbox_id uuid;
begin
  insert into public.sol_scan_runs (started_at, completed_at, config_version, status, diagnostics)
  values (
    (p_payload->>'started_at')::timestamptz,
    (p_payload->>'completed_at')::timestamptz,
    p_payload->>'config_version',
    case when coalesce((p_payload->'diagnostics'->>'halted')::boolean, false) then 'halted' else 'completed' end,
    coalesce(p_payload->'diagnostics', '{}'::jsonb)
  ) returning id into v_scan_id;

  for v_instrument in select value from jsonb_array_elements(coalesce(p_payload->'instruments', '[]'::jsonb)) loop
    insert into public.sol_instruments (
      symbol, base_asset, quote_asset, margin_asset, contract_type, status,
      onboard_date, delivery_date, last_seen_at, live_eligible, metadata
    ) values (
      v_instrument->>'symbol', v_instrument->>'baseAsset', v_instrument->>'quoteAsset', v_instrument->>'marginAsset',
      v_instrument->>'contractType', v_instrument->>'status',
      to_timestamp(nullif(v_instrument->>'onboardDate', '0')::double precision / 1000),
      to_timestamp(nullif(v_instrument->>'deliveryDate', '0')::double precision / 1000),
      now(),
      v_instrument->>'status' = 'TRADING',
      coalesce(v_instrument, '{}'::jsonb)
    )
    on conflict (symbol) do update set
      status = excluded.status, last_seen_at = now(), live_eligible = excluded.live_eligible, metadata = excluded.metadata;
  end loop;

  for v_signal in select value from jsonb_array_elements(coalesce(p_payload->'signals', '[]'::jsonb)) loop
    insert into public.sol_signals (
      scan_run_id, symbol, strategy_family, direction, parameter_key, trigger_time,
      current_price, mark_price, entry_low, entry_high, chase_boundary,
      technical_invalidation, reference_stop, reference_target, net_reward_risk,
      score, evidence, confluence, weight, expires_at, shadow_only, idempotency_key
    ) values (
      v_scan_id,
      v_signal->>'symbol', v_signal->>'family', v_signal->>'direction', v_signal->>'parameterKey',
      to_timestamp((v_signal->>'triggerTime')::double precision / 1000),
      (v_signal->>'currentPrice')::numeric, (v_signal->>'markPrice')::numeric,
      (v_signal->>'entryLow')::numeric, (v_signal->>'entryHigh')::numeric, (v_signal->>'chaseBoundary')::numeric,
      (v_signal->>'technicalInvalidation')::numeric, (v_signal->>'referenceStop')::numeric, (v_signal->>'referenceTarget')::numeric,
      (v_signal->>'netRewardRisk')::numeric, coalesce(v_signal->'score', '{}'::jsonb), coalesce(v_signal->'evidence', '{}'::jsonb),
      least(5, greatest(1, coalesce((v_signal->>'confluence')::integer, 1))), (v_signal->>'weight')::numeric,
      to_timestamp((v_signal->>'expiresAt')::double precision / 1000), coalesce((v_signal->>'shadowOnly')::boolean, true),
      'signal:' || (v_signal->>'symbol') || ':' || (v_signal->>'direction') || ':' || (v_signal->>'triggerTime')
    );

    insert into public.sol_signal_states (state_key, symbol, strategy_family, direction, active, last_triggered_at, updated_at)
    values (
      v_signal->>'symbol' || ':' || v_signal->>'direction',
      v_signal->>'symbol', v_signal->>'family', v_signal->>'direction',
      true, to_timestamp((v_signal->>'triggerTime')::double precision / 1000), now()
    )
    on conflict (state_key) do update set active = excluded.active, last_triggered_at = excluded.last_triggered_at, updated_at = now();
  end loop;

  v_email := p_payload->'email';
  if v_email is not null and v_email <> 'null'::jsonb and coalesce(jsonb_array_length(p_payload->'signals'), 0) > 0 then
    select min(to_timestamp((value->>'expiresAt')::double precision / 1000)) into v_expires_at
    from jsonb_array_elements(p_payload->'signals');
    insert into public.sol_outbox (idempotency_key, scan_run_id, recipient, subject, body_text, expires_at)
    values ('scan-email:' || md5(coalesce((p_payload->>'config_version'), '') || ':' || coalesce((select string_agg((value->>'symbol') || ':' || (value->>'family') || ':' || (value->>'direction') || ':' || (value->>'triggerTime'), '|' order by (value->>'symbol'), (value->>'family'), (value->>'direction'), (value->>'triggerTime')) from jsonb_array_elements(coalesce(p_payload->'signals', '[]'::jsonb)) value where not coalesce((value->>'shadowOnly')::boolean, true)), 'none')), v_scan_id, v_email->>'recipient', v_email->>'subject', v_email->>'bodyText', coalesce(v_expires_at, now() + interval '30 minutes'))
    on conflict (idempotency_key) do nothing returning id into v_outbox_id;
  end if;
  return jsonb_build_object('scan_run_id', v_scan_id, 'outbox_id', v_outbox_id);
end;
$function$;

create or replace function public.sol_claim_due_outbox(p_limit integer, p_now timestamptz)
returns table (id uuid, idempotency_key text, recipient text, subject text, body_text text, attempt_count integer, expires_at timestamptz)
language plpgsql
security definer
set search_path = public
as $function$
begin
  update public.sol_outbox set status = 'pending', claimed_at = null where status = 'sending' and claimed_at is not null and claimed_at <= p_now - interval '5 minutes' and expires_at > p_now and attempt_count < 4;
  update public.sol_outbox set status = 'expired', last_error = 'expired while claimed' where status = 'sending' and expires_at <= p_now;
  return query
  update public.sol_outbox o
  set status = 'sending', attempt_count = o.attempt_count + 1, claimed_at = p_now
  where o.id in (
    select candidate.id from public.sol_outbox candidate
    where candidate.status = 'pending' and candidate.attempt_count < 4 and candidate.next_attempt_at <= p_now and candidate.expires_at > p_now
    order by candidate.next_attempt_at, candidate.created_at
    for update skip locked
    limit greatest(1, least(p_limit, 50))
  )
  returning o.id, o.idempotency_key, o.recipient, o.subject, o.body_text, o.attempt_count, o.expires_at;
end;
$function$;

create or replace function public.sol_mark_outbox_delivered(p_id uuid, p_delivered_at timestamptz)
returns void language plpgsql security definer set search_path = public as $function$
begin
  update public.sol_outbox set status = 'delivered', delivered_at = p_delivered_at, last_error = null where id = p_id and status = 'sending';
  insert into public.sol_delivery_attempts (outbox_id, attempt_number, attempted_at, status)
  select id, attempt_count, p_delivered_at, 'sent' from public.sol_outbox where id = p_id;
end;
$function$;

create or replace function public.sol_mark_outbox_failed(p_id uuid, p_next_attempt_at timestamptz, p_error text)
returns void language plpgsql security definer set search_path = public as $function$
declare v_expired boolean;
begin
  select expires_at <= p_next_attempt_at or attempt_count >= 4 into v_expired from public.sol_outbox where id = p_id for update;
  update public.sol_outbox
  set status = case when coalesce(v_expired, true) then 'expired' else 'pending' end,
      next_attempt_at = p_next_attempt_at, last_error = left(p_error, 1000)
  where id = p_id and status = 'sending';
  insert into public.sol_delivery_attempts (outbox_id, attempt_number, attempted_at, status, error)
  select id, attempt_count, now(), case when coalesce(v_expired, true) then 'expired' else 'failed' end, left(p_error, 1000)
  from public.sol_outbox where id = p_id;
end;
$function$;

create or replace function public.sol_set_runtime_status(p_key text, p_value jsonb)
returns void language sql security definer set search_path = public as $function$
  insert into public.sol_runtime_status(status_key, status_value, updated_at) values (p_key, p_value, now())
  on conflict (status_key) do update set status_value = excluded.status_value, updated_at = now();
$function$;

create or replace function public.sol_record_health_event(p_type text, p_detail text, p_occurred_at timestamptz)
returns void language plpgsql security definer set search_path = public as $function$
declare
  v_value jsonb;
  v_count integer;
  v_open boolean;
begin
  select status_value into v_value from public.sol_runtime_status where status_key = 'scan_health' for update;
  v_count := coalesce((v_value->>'consecutiveFailures')::integer, 0);
  v_open := coalesce((v_value->>'failureEventOpen')::boolean, false);
  if p_type = 'scan_failure' then
    v_count := v_count + 1;
    if v_count >= 2 and not v_open then
      insert into public.sol_health_events(event_type, detail, occurred_at) values ('scan_failure', left(p_detail, 2000), p_occurred_at);
      v_open := true;
    end if;
    insert into public.sol_runtime_status(status_key, status_value, updated_at) values ('scan_health', jsonb_build_object('consecutiveFailures', v_count, 'failureEventOpen', v_open), now())
    on conflict (status_key) do update set status_value = excluded.status_value, updated_at = now();
  elsif p_type = 'scan_recovery' then
    if v_open then insert into public.sol_health_events(event_type, detail, occurred_at) values ('scan_recovery', left(p_detail, 2000), p_occurred_at); end if;
    insert into public.sol_runtime_status(status_key, status_value, updated_at) values ('scan_health', jsonb_build_object('consecutiveFailures', 0, 'failureEventOpen', false), now())
    on conflict (status_key) do update set status_value = excluded.status_value, updated_at = now();
  else
    insert into public.sol_health_events(event_type, detail, occurred_at) values (p_type, left(p_detail, 2000), p_occurred_at);
  end if;
end;
$function$;

create or replace function public.sol_publish_weight_version(p_id uuid)
returns void language plpgsql security definer set search_path = public as $function$
begin
  perform 1 from public.sol_weight_versions where id = p_id for update;
  update public.sol_weight_versions set status = 'rolled_back' where status = 'champion';
  update public.sol_weight_versions set status = 'champion', activated_at = now() where id = p_id and status = 'challenger';
  if not found then raise exception 'challenger weight version not found'; end if;
end;
$function$;

create or replace function public.sol_rollback_weight_version(p_target_id uuid, p_reason text)
returns void language plpgsql security definer set search_path = public as $function$
begin
  update public.sol_weight_versions set status = 'rolled_back' where status = 'champion';
  update public.sol_weight_versions set status = 'champion', activated_at = now(), source = left('rollback:' || p_reason, 200) where id = p_target_id;
  if not found then raise exception 'rollback target weight version not found'; end if;
  insert into public.sol_health_events(event_type, detail) values ('rollback', left(p_reason, 2000));
end;
$function$;

create or replace function public.sol_rollback_active_weight(p_reason text)
returns void language plpgsql security definer set search_path = public as $function$
declare
  v_current_version integer;
  v_previous_id uuid;
begin
  select version into v_current_version from public.sol_weight_versions where status = 'champion' for update;
  select id into v_previous_id from public.sol_weight_versions where status = 'rolled_back' and version < coalesce(v_current_version, 0) order by version desc limit 1;
  if v_previous_id is null then raise exception 'no previous champion weight version available'; end if;
  update public.sol_weight_versions set status = 'rolled_back' where status = 'champion';
  update public.sol_weight_versions set status = 'champion', activated_at = now(), source = left('automatic_rollback:' || p_reason, 200) where id = v_previous_id;
  insert into public.sol_health_events(event_type, detail) values ('rollback', left(p_reason, 2000));
end;
$function$;

do $block$
declare
  table_name text;
begin
  foreach table_name in array array[
    'sol_app_configurations','sol_instruments','sol_scan_runs','sol_signal_states','sol_signals','sol_simulated_positions',
    'sol_simulated_trades','sol_strategy_evidence','sol_weight_versions','sol_outbox','sol_delivery_attempts','sol_health_events','sol_runtime_status'
  ] loop
    execute format('alter table public.%I enable row level security', table_name);
    execute format('revoke all on table public.%I from anon, authenticated', table_name);
  end loop;
end;
$block$;

revoke all on function public.sol_commit_scan(jsonb) from public, anon, authenticated;
revoke all on function public.sol_claim_due_outbox(integer, timestamptz) from public, anon, authenticated;
revoke all on function public.sol_mark_outbox_delivered(uuid, timestamptz) from public, anon, authenticated;
revoke all on function public.sol_mark_outbox_failed(uuid, timestamptz, text) from public, anon, authenticated;
revoke all on function public.sol_set_runtime_status(text, jsonb) from public, anon, authenticated;
revoke all on function public.sol_record_health_event(text, text, timestamptz) from public, anon, authenticated;
revoke all on function public.sol_publish_weight_version(uuid) from public, anon, authenticated;
revoke all on function public.sol_rollback_weight_version(uuid, text) from public, anon, authenticated;
revoke all on function public.sol_rollback_active_weight(text) from public, anon, authenticated;

commit;

