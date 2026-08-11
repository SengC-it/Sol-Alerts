begin;

-- This migration is intentionally additive so an operator can apply it after
-- the original v1 schema without deleting the historical scan ledger.
alter table public.sol_simulated_positions add column if not exists state_key text;
alter table public.sol_simulated_positions add column if not exists trigger_time timestamptz;
alter table public.sol_simulated_positions add column if not exists expires_at timestamptz;
alter table public.sol_simulated_positions add column if not exists entry_valid_until timestamptz;
alter table public.sol_simulated_positions add column if not exists chase_boundary numeric;
alter table public.sol_simulated_positions add column if not exists technical_invalidation numeric;
alter table public.sol_simulated_positions add column if not exists exit_reason text;
alter table public.sol_simulated_positions add column if not exists exit_price numeric;
alter table public.sol_simulated_positions add column if not exists net_r numeric;
alter table public.sol_simulated_positions add column if not exists fees_r numeric not null default 0;
alter table public.sol_simulated_positions add column if not exists shadow_only boolean not null default false;
alter table public.sol_simulated_positions add column if not exists last_evaluated_close_time timestamptz;
alter table public.sol_simulated_positions add column if not exists weight_version_id uuid references public.sol_weight_versions(id);
alter table public.sol_simulated_positions add column if not exists accrued_funding_r numeric not null default 0;
alter table public.sol_simulated_positions add column if not exists last_funding_time timestamptz;
alter table public.sol_simulated_positions add column if not exists config_version text;
alter table public.sol_simulated_positions add column if not exists evidence_id text;
alter table public.sol_simulated_trades add column if not exists weight_version_id uuid references public.sol_weight_versions(id);
alter table public.sol_simulated_trades add column if not exists funding_r numeric not null default 0;
alter table public.sol_simulated_trades add column if not exists fees_r numeric not null default 0;
alter table public.sol_simulated_trades add column if not exists config_version text;
alter table public.sol_simulated_trades add column if not exists evidence_id text;
alter table public.sol_weight_versions add column if not exists symbol_weights jsonb not null default '{}'::jsonb;
alter table public.sol_weight_versions add column if not exists family_modes jsonb not null default '{}'::jsonb;
alter table public.sol_weight_versions add column if not exists symbol_modes jsonb not null default '{}'::jsonb;
alter table public.sol_weight_versions add column if not exists selected_parameters jsonb not null default '{}'::jsonb;
alter table public.sol_weight_versions add column if not exists report_id uuid;
alter table public.sol_signal_states add column if not exists shadow_only boolean not null default false;
alter table public.sol_simulated_positions drop constraint if exists sol_simulated_positions_status_check;
alter table public.sol_simulated_positions add constraint sol_simulated_positions_status_check check (status in ('pending', 'open', 'closed', 'shadow', 'expired'));
alter table public.sol_simulated_trades drop constraint if exists sol_simulated_trades_exit_reason_check;
alter table public.sol_simulated_trades add constraint sol_simulated_trades_exit_reason_check check (exit_reason in ('stop', 'target', 'time', 'invalidation', 'expired'));

create unique index if not exists sol_one_position_per_signal on public.sol_simulated_positions(signal_id);
create index if not exists sol_simulated_positions_capacity_idx on public.sol_simulated_positions(status, shadow_only, direction);

create table if not exists public.sol_simulation_accounts (
  id boolean primary key default true check (id = true),
  equity numeric not null default 100000,
  peak_equity numeric not null default 100000,
  drawdown numeric not null default 0,
  closed_signals integer not null default 0,
  halted boolean not null default false,
  last_funding_observation_at timestamptz,
  updated_at timestamptz not null default now()
);
alter table public.sol_simulation_accounts add column if not exists last_funding_observation_at timestamptz;
insert into public.sol_simulation_accounts(id) values (true) on conflict (id) do nothing;

create table if not exists public.sol_research_reports (
  id uuid primary key default gen_random_uuid(),
  report_version text not null,
  status text not null check (status in ('champion', 'challenger', 'rolled_back')),
  report jsonb not null,
  weight_version_id uuid references public.sol_weight_versions(id),
  created_at timestamptz not null default now()
);
create unique index if not exists sol_one_champion_research_report on public.sol_research_reports(status) where status = 'champion';
-- The active config can have only one latest evidence row per symbol/family.
-- Keep historical rows for other config versions while making publication's
-- evidence write an actual idempotent upsert.
create unique index if not exists sol_strategy_evidence_config_key on public.sol_strategy_evidence(symbol, strategy_family, config_version);
create index if not exists sol_simulated_positions_weight_version_id_idx on public.sol_simulated_positions(weight_version_id);
create index if not exists sol_simulated_trades_weight_version_id_idx on public.sol_simulated_trades(weight_version_id);
create index if not exists sol_research_reports_weight_version_id_idx on public.sol_research_reports(weight_version_id);

create or replace function public.sol_get_champion_research_report()
returns jsonb language sql security definer set search_path = public as $function$
  select report from public.sol_research_reports where status = 'champion' order by created_at desc limit 1;
$function$;

create or replace function public.sol_reconcile_simulation(p_bars jsonb, p_now timestamptz)
returns void
language plpgsql
security definer
set search_path = public
as $function$
declare
  v_account public.sol_simulation_accounts%rowtype;
  v_position public.sol_simulated_positions%rowtype;
  v_batch jsonb;
  v_candle jsonb;
  v_entry jsonb;
  v_stop boolean;
  v_target boolean;
  v_invalidated boolean;
  v_price numeric;
  v_reason text;
  v_entry_price numeric;
  v_risk numeric;
  v_gross_r numeric;
  v_net_r numeric;
  v_funding numeric;
  v_event jsonb;
  v_event_time timestamptz;
  v_event_rate numeric;
  v_event_mark numeric;
  v_fees numeric;
begin
  select * into v_account from public.sol_simulation_accounts where id = true for update;
  if not found then
    insert into public.sol_simulation_accounts(id) values (true) returning * into v_account;
  end if;
  for v_position in select * from public.sol_simulated_positions where status in ('pending', 'open', 'shadow') order by trigger_time, id for update loop
    select value into v_batch from jsonb_array_elements(coalesce(p_bars, '[]'::jsonb)) where value->>'symbol' = v_position.symbol limit 1;
    if v_batch is null then raise exception 'simulation batch missing for %', v_position.symbol; end if;
    if not (v_batch ? 'fundingEvents') or jsonb_typeof(v_batch->'fundingEvents') <> 'array' then raise exception 'funding events missing for %', v_position.symbol; end if;
    if v_position.status = 'shadow' then
      v_position.status := 'pending';
      update public.sol_simulated_positions set status = 'pending' where id = v_position.id;
    end if;
    if v_position.status = 'pending' then
      select value into v_entry
      from jsonb_array_elements(coalesce(v_batch->'candles', '[]'::jsonb))
      where (value->>'openTime')::double precision / 1000 > extract(epoch from v_position.trigger_time)
        and (value->>'openTime')::double precision / 1000 <= extract(epoch from v_position.entry_valid_until)
        and case when v_position.direction = 'long' then (value->>'open')::numeric <= v_position.chase_boundary else (value->>'open')::numeric >= v_position.chase_boundary end
      order by (value->>'openTime')::bigint limit 1;
      if v_entry is not null then
        v_entry_price := case when v_position.direction = 'long' then (v_entry->>'open')::numeric * 1.0005 else (v_entry->>'open')::numeric * 0.9995 end;
        update public.sol_simulated_positions set status = 'open', entry_time = to_timestamp((v_entry->>'openTime')::double precision / 1000), entry_price = v_entry_price, opened_at = p_now, last_evaluated_close_time = to_timestamp(((v_entry->>'openTime')::double precision - 1) / 1000) where id = v_position.id;
        v_position.status := 'open';
        v_position.entry_time := to_timestamp((v_entry->>'openTime')::double precision / 1000);
        v_position.entry_price := v_entry_price;
        v_position.last_evaluated_close_time := to_timestamp(((v_entry->>'openTime')::double precision - 1) / 1000);
      elsif p_now > v_position.entry_valid_until then
        update public.sol_simulated_positions set status = 'expired', exit_reason = 'expired', closed_at = p_now where id = v_position.id;
        continue;
      end if;
    end if;
    for v_candle in
      select value from jsonb_array_elements(coalesce(v_batch->'candles', '[]'::jsonb))
      where (value->>'closeTime')::double precision / 1000 > coalesce(extract(epoch from v_position.last_evaluated_close_time), 0)
        and (value->>'closeTime')::double precision / 1000 <= extract(epoch from p_now)
        and (value->>'closeTime')::double precision / 1000 <= extract(epoch from v_position.entry_time + interval '48 hours')
      order by (value->>'closeTime')::bigint
    loop
      -- Apply each funding event once, immediately before the candle in which
      -- it is observed. A position watermark makes retries/restarts idempotent
      -- and prevents events after an earlier exit from being charged.
      for v_event in select value from jsonb_array_elements(coalesce(v_batch->'fundingEvents', '[]'::jsonb)) where value->>'symbol' = v_position.symbol order by (value->>'fundingTime')::bigint loop
        v_event_time := to_timestamp((v_event->>'fundingTime')::double precision / 1000);
        v_event_rate := (v_event->>'fundingRate')::numeric;
        v_event_mark := coalesce((v_event->>'markPrice')::numeric, v_position.entry_price);
        if v_event_rate is null or v_event_rate = 'NaN'::numeric or abs(v_event_rate) > 1 or v_event_mark is null or v_event_mark <= 0 then raise exception 'malformed funding event'; end if;
        if v_event_time > v_position.entry_time and v_event_time <= to_timestamp((v_candle->>'closeTime')::double precision / 1000) and (v_position.last_funding_time is null or v_event_time > v_position.last_funding_time) then
          v_risk := case when v_position.direction = 'long' then v_position.entry_price - v_position.reference_stop else v_position.reference_stop - v_position.entry_price end;
          v_funding := case when v_risk > 0 then (case when v_position.direction = 'long' then -1 else 1 end) * v_event_rate * v_event_mark / v_risk else 0 end;
          v_position.accrued_funding_r := coalesce(v_position.accrued_funding_r, 0) + v_funding;
          v_position.last_funding_time := v_event_time;
          update public.sol_simulated_positions set accrued_funding_r = v_position.accrued_funding_r, last_funding_time = v_position.last_funding_time where id = v_position.id;
        end if;
      end loop;
      v_stop := case when v_position.direction = 'long' then (v_candle->>'low')::numeric <= v_position.reference_stop else (v_candle->>'high')::numeric >= v_position.reference_stop end;
      v_target := case when v_position.direction = 'long' then (v_candle->>'high')::numeric >= v_position.reference_target else (v_candle->>'low')::numeric <= v_position.reference_target end;
      v_invalidated := case when v_position.direction = 'long' then (v_candle->>'close')::numeric <= v_position.technical_invalidation else (v_candle->>'close')::numeric >= v_position.technical_invalidation end;
      if v_stop then
        v_reason := 'stop';
        v_price := case when v_position.direction = 'long' then least((v_candle->>'open')::numeric, v_position.reference_stop) else greatest((v_candle->>'open')::numeric, v_position.reference_stop) end;
      elsif v_target then
        v_reason := 'target'; v_price := v_position.reference_target;
      elsif v_invalidated then
        v_reason := 'invalidation'; v_price := (v_candle->>'close')::numeric;
      elsif (v_candle->>'closeTime')::double precision / 1000 - extract(epoch from v_position.entry_time) >= 48 * 3600 - 15 * 60 then
        v_reason := 'time'; v_price := (v_candle->>'close')::numeric;
      else
        update public.sol_simulated_positions set last_evaluated_close_time = to_timestamp((v_candle->>'closeTime')::double precision / 1000) where id = v_position.id;
        continue;
      end if;
      v_price := case when v_position.direction = 'long' then v_price * 0.9995 else v_price * 1.0005 end;
      v_risk := case when v_position.direction = 'long' then v_position.entry_price - v_position.reference_stop else v_position.reference_stop - v_position.entry_price end;
      v_gross_r := case when v_position.direction = 'long' then v_price - v_position.entry_price else v_position.entry_price - v_price end;
      v_gross_r := case when v_risk > 0 then v_gross_r / v_risk else 0 end;
      v_funding := coalesce(v_position.accrued_funding_r, 0);
      v_fees := case when v_risk > 0 then ((v_position.entry_price + v_price) * 0.0005) / v_risk else 0 end;
      v_net_r := v_gross_r - v_fees + v_funding;
      update public.sol_simulated_positions set status = 'closed', closed_at = to_timestamp((v_candle->>'closeTime')::double precision / 1000), exit_price = v_price, exit_reason = v_reason, net_r = v_net_r, fees_r = v_fees, accrued_funding_r = v_funding, last_evaluated_close_time = to_timestamp((v_candle->>'closeTime')::double precision / 1000) where id = v_position.id;
      insert into public.sol_simulated_trades(position_id, signal_id, symbol, direction, entry_time, entry_price, exit_time, exit_price, exit_reason, gross_r, net_r, fees_r, funding_cost, funding_r, shadow_only, weight_version_id, config_version, evidence_id)
      values (v_position.id, v_position.signal_id, v_position.symbol, v_position.direction, v_position.entry_time, v_position.entry_price, to_timestamp((v_candle->>'closeTime')::double precision / 1000), v_price, v_reason, v_gross_r, v_net_r, v_fees, v_funding, v_funding, v_position.shadow_only, v_position.weight_version_id, v_position.config_version, v_position.evidence_id);
      if not v_position.shadow_only then
        v_account.equity := v_account.equity + v_position.risk_budget * v_net_r;
        v_account.peak_equity := greatest(v_account.peak_equity, v_account.equity);
        v_account.closed_signals := v_account.closed_signals + 1;
        v_account.drawdown := case when v_account.peak_equity > 0 then (v_account.peak_equity - v_account.equity) / v_account.peak_equity else 0 end;
        update public.sol_simulation_accounts set equity = v_account.equity, peak_equity = v_account.peak_equity, drawdown = v_account.drawdown, closed_signals = v_account.closed_signals, updated_at = p_now where id = true;
      end if;
      exit;
    end loop;
  end loop;
end;
$function$;

create or replace function public.sol_get_simulation_state()
returns jsonb language sql security definer set search_path = public as $function$
  select jsonb_build_object(
    'equity', account.equity,
    'peak_equity', account.peak_equity,
    'drawdown', account.drawdown,
    'closed_signals', account.closed_signals,
    'halt', account.halted,
    'last_funding_observation_at', account.last_funding_observation_at,
    'positions', coalesce((select jsonb_agg(to_jsonb(position)) from public.sol_simulated_positions position where position.status in ('pending', 'open', 'shadow')), '[]'::jsonb),
    'current_net_rs', coalesce((select jsonb_agg(trade.net_r order by trade.exit_time) from public.sol_simulated_trades trade where trade.shadow_only = false and trade.weight_version_id = (select id from public.sol_weight_versions where status = 'champion' order by version desc limit 1)), '[]'::jsonb),
    'prior_net_rs', coalesce((select jsonb_agg(trade.net_r order by trade.exit_time) from public.sol_simulated_trades trade where trade.shadow_only = false and trade.weight_version_id = (select id from public.sol_weight_versions where status = 'rolled_back' order by version desc limit 1)), '[]'::jsonb)
  ) from public.sol_simulation_accounts account where account.id = true;
$function$;

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
  v_signal_id uuid;
  v_state_key text;
  v_formal boolean;
  v_total integer;
  v_long integer;
  v_short integer;
  v_account public.sol_simulation_accounts%rowtype;
  v_weight_version_id uuid;
begin
  select * into v_account from public.sol_simulation_accounts where id = true for update;
  if not found then insert into public.sol_simulation_accounts(id) values (true) returning * into v_account; end if;
  perform public.sol_reconcile_simulation(coalesce(p_payload->'simulation'->'bars', '[]'::jsonb), (p_payload->>'completed_at')::timestamptz);
  v_weight_version_id := nullif(p_payload->'simulation'->>'weightVersionId', '')::uuid;
  insert into public.sol_scan_runs (started_at, completed_at, config_version, status, diagnostics, live_simulated_drawdown)
  values ((p_payload->>'started_at')::timestamptz, (p_payload->>'completed_at')::timestamptz, p_payload->>'config_version', case when coalesce((p_payload->'diagnostics'->>'halted')::boolean, false) then 'halted' else 'completed' end, coalesce(p_payload->'diagnostics', '{}'::jsonb), coalesce((select drawdown from public.sol_simulation_accounts where id = true), 0)) returning id into v_scan_id;
  for v_instrument in select value from jsonb_array_elements(coalesce(p_payload->'instruments', '[]'::jsonb)) loop
    insert into public.sol_instruments (symbol, base_asset, quote_asset, margin_asset, contract_type, status, onboard_date, delivery_date, last_seen_at, live_eligible, metadata)
    values (v_instrument->>'symbol', v_instrument->>'baseAsset', v_instrument->>'quoteAsset', v_instrument->>'marginAsset', v_instrument->>'contractType', v_instrument->>'status', to_timestamp(nullif(v_instrument->>'onboardDate', '0')::double precision / 1000), to_timestamp(nullif(v_instrument->>'deliveryDate', '0')::double precision / 1000), now(), v_instrument->>'status' = 'TRADING', coalesce(v_instrument, '{}'::jsonb))
    on conflict (symbol) do update set status = excluded.status, last_seen_at = now(), live_eligible = excluded.live_eligible, metadata = excluded.metadata;
  end loop;
  -- A vanished active state is invalidated in the same transaction. The one-hour
  -- cooldown is persisted, so two concurrent scans cannot re-arm it early.
  update public.sol_signal_states set active = false, shadow_only = false, last_invalidated_at = (p_payload->>'completed_at')::timestamptz, cooldown_until = (p_payload->>'completed_at')::timestamptz + interval '1 hour', updated_at = now()
  where active and not exists (select 1 from jsonb_array_elements_text(coalesce(p_payload->'observed_state_keys', '[]'::jsonb)) observed(state_key) where observed.state_key = public.sol_signal_states.state_key);
  for v_signal in select value from jsonb_array_elements(coalesce(p_payload->'signals', '[]'::jsonb)) loop
    -- One setup activation is shared by all same-direction family votes.
    v_state_key := v_signal->>'symbol' || ':' || v_signal->>'direction';
    v_formal := not coalesce((v_signal->>'shadowOnly')::boolean, true);
    insert into public.sol_signals (scan_run_id, symbol, strategy_family, direction, parameter_key, trigger_time, current_price, mark_price, entry_low, entry_high, chase_boundary, technical_invalidation, reference_stop, reference_target, net_reward_risk, score, evidence, confluence, weight, expires_at, shadow_only, idempotency_key)
    values (v_scan_id, v_signal->>'symbol', v_signal->>'family', v_signal->>'direction', v_signal->>'parameterKey', to_timestamp((v_signal->>'triggerTime')::double precision / 1000), (v_signal->>'currentPrice')::numeric, (v_signal->>'markPrice')::numeric, (v_signal->>'entryLow')::numeric, (v_signal->>'entryHigh')::numeric, (v_signal->>'chaseBoundary')::numeric, (v_signal->>'technicalInvalidation')::numeric, (v_signal->>'referenceStop')::numeric, (v_signal->>'referenceTarget')::numeric, (v_signal->>'netRewardRisk')::numeric, coalesce(v_signal->'score', '{}'::jsonb), coalesce(v_signal->'evidence', '{}'::jsonb), least(5, greatest(1, coalesce((v_signal->>'confluence')::integer, 1))), (v_signal->>'weight')::numeric, to_timestamp((v_signal->>'expiresAt')::double precision / 1000), not v_formal, 'signal:' || v_state_key || ':' || (v_signal->>'triggerTime'))
    on conflict (idempotency_key) do update set scan_run_id = excluded.scan_run_id returning id into v_signal_id;
    insert into public.sol_signal_states (state_key, symbol, strategy_family, direction, active, shadow_only, last_triggered_at, updated_at)
    values (v_state_key, v_signal->>'symbol', v_signal->>'family', v_signal->>'direction', true, not v_formal, to_timestamp((v_signal->>'triggerTime')::double precision / 1000), now())
    on conflict (state_key) do update set active = excluded.active, shadow_only = excluded.shadow_only, last_triggered_at = excluded.last_triggered_at, updated_at = now();
    if not exists (select 1 from public.sol_simulated_positions where signal_id = v_signal_id) then
      select count(*) filter (where status in ('pending', 'open') and shadow_only = false), count(*) filter (where status in ('pending', 'open') and shadow_only = false and direction = 'long'), count(*) filter (where status in ('pending', 'open') and shadow_only = false and direction = 'short') into v_total, v_long, v_short from public.sol_simulated_positions;
      if v_formal and v_total < 6 and (case when v_signal->>'direction' = 'long' then v_long < 3 else v_short < 3 end) then
        insert into public.sol_simulated_positions (signal_id, symbol, direction, status, state_key, trigger_time, expires_at, entry_valid_until, chase_boundary, technical_invalidation, reference_stop, reference_target, risk_budget, shadow_only, weight_version_id, config_version, evidence_id)
        values (v_signal_id, v_signal->>'symbol', v_signal->>'direction', 'pending', v_state_key, to_timestamp((v_signal->>'triggerTime')::double precision / 1000), to_timestamp((v_signal->>'expiresAt')::double precision / 1000), to_timestamp((v_signal->>'expiresAt')::double precision / 1000), (v_signal->>'chaseBoundary')::numeric, (v_signal->>'technicalInvalidation')::numeric, (v_signal->>'referenceStop')::numeric, (v_signal->>'referenceTarget')::numeric, (select equity * 0.01 from public.sol_simulation_accounts where id = true), false, v_weight_version_id, p_payload->>'config_version', coalesce(v_signal->>'evidenceId', v_signal->>'symbol' || ':' || v_signal->>'family'));
      elsif not v_formal then
        insert into public.sol_simulated_positions (signal_id, symbol, direction, status, state_key, trigger_time, expires_at, entry_valid_until, chase_boundary, technical_invalidation, reference_stop, reference_target, risk_budget, shadow_only, weight_version_id, config_version, evidence_id)
        values (v_signal_id, v_signal->>'symbol', v_signal->>'direction', 'pending', v_state_key, to_timestamp((v_signal->>'triggerTime')::double precision / 1000), to_timestamp((v_signal->>'expiresAt')::double precision / 1000), to_timestamp((v_signal->>'expiresAt')::double precision / 1000), (v_signal->>'chaseBoundary')::numeric, (v_signal->>'technicalInvalidation')::numeric, (v_signal->>'referenceStop')::numeric, (v_signal->>'referenceTarget')::numeric, (select equity * 0.01 from public.sol_simulation_accounts where id = true), true, v_weight_version_id, p_payload->>'config_version', coalesce(v_signal->>'evidenceId', v_signal->>'symbol' || ':' || v_signal->>'family'));
      end if;
    end if;
  end loop;
  if p_payload->'simulation'->'riskReference' is not null then
    insert into public.sol_runtime_status(status_key, status_value, updated_at) values ('global_risk', p_payload->'simulation'->'riskReference', now()) on conflict (status_key) do update set status_value = excluded.status_value, updated_at = now();
  end if;
  if p_payload->'simulation'->>'fundingObservationAt' is not null then
    update public.sol_simulation_accounts set last_funding_observation_at = to_timestamp((p_payload->'simulation'->>'fundingObservationAt')::double precision / 1000), updated_at = now() where id = true;
  end if;
  v_email := p_payload->'email';
  if v_email is not null and v_email <> 'null'::jsonb then
    select min(to_timestamp((value->>'expiresAt')::double precision / 1000)) into v_expires_at from jsonb_array_elements(coalesce(p_payload->'signals', '[]'::jsonb)) value where not coalesce((value->>'shadowOnly')::boolean, true);
    if v_expires_at is not null then
      insert into public.sol_outbox (idempotency_key, scan_run_id, recipient, subject, body_text, expires_at) values ('scan-email:' || md5(coalesce((p_payload->>'config_version'), '') || ':' || coalesce((select string_agg((value->>'symbol') || ':' || (value->>'family') || ':' || (value->>'direction') || ':' || (value->>'triggerTime'), '|' order by (value->>'symbol'), (value->>'family'), (value->>'direction'), (value->>'triggerTime')) from jsonb_array_elements(coalesce(p_payload->'signals', '[]'::jsonb)) value where not coalesce((value->>'shadowOnly')::boolean, true)), 'none')), v_scan_id, v_email->>'recipient', v_email->>'subject', v_email->>'bodyText', v_expires_at) on conflict (idempotency_key) do nothing returning id into v_outbox_id;
    end if;
  end if;
  return jsonb_build_object('scan_run_id', v_scan_id, 'outbox_id', v_outbox_id);
end;
$function$;

create or replace function public.sol_claim_due_outbox(p_limit integer, p_now timestamptz)
returns table (id uuid, idempotency_key text, recipient text, subject text, body_text text, attempt_count integer, expires_at timestamptz)
language plpgsql security definer set search_path = public as $function$
begin
  -- A crashed Vercel worker may leave a claim in sending.  Requeue its
  -- lease before selecting due messages; expired rows remain unsendable.
  update public.sol_outbox set status = 'pending', claimed_at = null
    where status = 'sending' and claimed_at is not null and claimed_at <= p_now - interval '5 minutes' and expires_at > p_now and attempt_count < 4;
  update public.sol_outbox set status = 'expired', last_error = 'expired while claimed'
    where status = 'sending' and expires_at <= p_now;
  return query update public.sol_outbox o set status = 'sending', attempt_count = o.attempt_count + 1, claimed_at = p_now where o.id in (select candidate.id from public.sol_outbox candidate where candidate.status = 'pending' and candidate.attempt_count < 4 and candidate.next_attempt_at <= p_now and candidate.expires_at > p_now order by candidate.next_attempt_at, candidate.created_at for update skip locked limit greatest(1, least(p_limit, 50))) returning o.id, o.idempotency_key, o.recipient, o.subject, o.body_text, o.attempt_count, o.expires_at;
end;
$function$;

create or replace function public.sol_mark_outbox_failed(p_id uuid, p_next_attempt_at timestamptz, p_error text, p_terminal boolean default false)
returns void language plpgsql security definer set search_path = public as $function$
declare v_expired boolean; v_terminal boolean;
begin
  select expires_at <= p_next_attempt_at, attempt_count >= 4 or p_terminal into v_expired, v_terminal from public.sol_outbox where id = p_id for update;
  update public.sol_outbox set status = case when coalesce(v_expired, true) then 'expired' when coalesce(v_terminal, true) then 'failed' else 'pending' end, next_attempt_at = p_next_attempt_at, last_error = left(p_error, 1000) where id = p_id and status = 'sending';
  insert into public.sol_delivery_attempts (outbox_id, attempt_number, attempted_at, status, error) select id, attempt_count, now(), case when coalesce(v_expired, true) then 'expired' else 'failed' end, left(p_error, 1000) from public.sol_outbox where id = p_id;
end;
$function$;

-- The weekly research job calls this boundary after evaluating the complete
-- market evidence. Halt state is durable and is also copied to the simulation
-- account so every Vercel instance fails closed without module globals.
create or replace function public.sol_apply_lifecycle_evaluation(p_all_strategies_pass boolean, p_emergency boolean, p_detail text default '')
returns jsonb
language plpgsql
security definer
set search_path = public
as $function$
declare
  v_value jsonb;
  v_halted boolean;
  v_valid_weeks integer;
  v_notice_sent boolean;
  v_recipient text;
  v_now timestamptz := now();
begin
  select status_value into v_value from public.sol_runtime_status where status_key = 'lifecycle' for update;
  v_halted := coalesce((v_value->>'halted')::boolean, false);
  v_valid_weeks := coalesce((v_value->>'consecutiveValidWeeklyEvaluations')::integer, 0);
  v_notice_sent := coalesce((v_value->>'haltNoticeSent')::boolean, false);
  if p_emergency or not p_all_strategies_pass then
    v_halted := true;
    v_valid_weeks := 0;
    if not v_notice_sent then
      select config_value->>'recipient' into v_recipient from public.sol_app_configurations where config_key = 'alert_recipient' and is_active;
      if v_recipient is not null then
        insert into public.sol_outbox (idempotency_key, recipient, subject, body_text, expires_at)
        values ('halt-notice:' || to_char(v_now, 'IYYY-IW'), v_recipient, 'Sol Alerts: trade emails halted', 'All strategy families failed the persisted weekly evidence gates. Trade emails remain halted; research and shadow tracking continue.', v_now + interval '7 days')
        on conflict (idempotency_key) do nothing;
      end if;
      insert into public.sol_health_events(event_type, detail, occurred_at) values ('halt', left(coalesce(p_detail, 'all strategy gates failed'), 2000), v_now);
      v_notice_sent := true;
    end if;
  else
    v_valid_weeks := least(2, v_valid_weeks + 1);
    if v_halted and v_valid_weeks >= 2 then
      v_halted := false;
      v_notice_sent := false;
      insert into public.sol_health_events(event_type, detail, occurred_at) values ('halt', 'trade emails automatically resumed after two valid weekly evaluations', v_now);
    end if;
  end if;
  insert into public.sol_runtime_status(status_key, status_value, updated_at)
  values ('lifecycle', jsonb_build_object('halted', v_halted, 'consecutiveValidWeeklyEvaluations', v_valid_weeks, 'haltNoticeSent', v_notice_sent, 'updatedAt', v_now), v_now)
  on conflict (status_key) do update set status_value = excluded.status_value, updated_at = excluded.updated_at;
  update public.sol_simulation_accounts set halted = v_halted, updated_at = v_now where id = true;
  return jsonb_build_object('halted', v_halted, 'consecutiveValidWeeklyEvaluations', v_valid_weeks, 'haltNoticeSent', v_notice_sent);
end;
$function$;

-- Publication is a weight/config activation, never generated code. The row
-- lock, rollback pointer, and champion uniqueness make promotion atomic.
create or replace function public.sol_publish_weight_challenger(p_publication jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $function$
declare
  v_champion public.sol_weight_versions%rowtype;
  v_id uuid;
  v_version integer;
  v_published boolean := coalesce((p_publication->>'published')::boolean, false);
  v_lifecycle jsonb;
  v_report_id uuid;
  v_evidence jsonb;
  v_expected_config text;
begin
  select config_value->>'configVersion' into v_expected_config from public.sol_app_configurations where config_key = 'active_strategy_config' and is_active order by updated_at desc limit 1;
  if v_expected_config is not null and v_expected_config <> p_publication->>'configVersion' then
    raise exception 'incompatible publication config version';
  end if;
  if p_publication->'reportLinkage'->>'configVersion' is distinct from p_publication->>'configVersion' then
    raise exception 'publication report linkage version mismatch';
  end if;
  if jsonb_typeof(p_publication->'weights') <> 'object'
     or not (p_publication->'weights' ? 'trend_breakout')
     or not (p_publication->'weights' ? 'trend_pullback')
     or not (p_publication->'weights' ? 'range_mean_reversion')
     or jsonb_typeof(p_publication->'selectedParameters') <> 'object'
     or jsonb_typeof(p_publication->'selectedParameters'->'trend_breakout') <> 'object'
     or jsonb_typeof(p_publication->'selectedParameters'->'trend_pullback') <> 'object'
     or jsonb_typeof(p_publication->'selectedParameters'->'range_mean_reversion') <> 'object'
     or not (p_publication->'selectedParameters'->'trend_breakout' ? 'long')
     or not (p_publication->'selectedParameters'->'trend_breakout' ? 'short')
     or not (p_publication->'selectedParameters'->'trend_pullback' ? 'long')
     or not (p_publication->'selectedParameters'->'trend_pullback' ? 'short')
     or not (p_publication->'selectedParameters'->'range_mean_reversion' ? 'long')
     or not (p_publication->'selectedParameters'->'range_mean_reversion' ? 'short')
     or jsonb_typeof(p_publication->'symbolWeights') <> 'object'
     or jsonb_typeof(p_publication->'symbolModes') <> 'object'
     or jsonb_typeof(p_publication->'evidenceRows') <> 'array' then
    raise exception 'incomplete publication payload';
  end if;
  if not v_published then
    v_lifecycle := public.sol_apply_lifecycle_evaluation(coalesce((p_publication->'lifecycle'->>'allStrategiesPass')::boolean, false), false, 'weekly Champion-Challenger evaluation failed');
    return jsonb_build_object('published', false, 'reason', 'champion-challenger gates failed', 'lifecycle', v_lifecycle);
  end if;
  select * into v_champion from public.sol_weight_versions where status = 'champion' order by version desc limit 1 for update;
  if not found and not coalesce((p_publication->>'bootstrapChampion')::boolean, false) then
    raise exception 'champion is required unless explicit bootstrap publication is requested';
  end if;
  select coalesce(max(version), 0) + 1 into v_version from public.sol_weight_versions;
  insert into public.sol_weight_versions(version, config_version, status, weights, symbol_weights, family_modes, symbol_modes, selected_parameters, source, rollback_of, out_of_sample_max_drawdown)
  values (v_version, p_publication->>'configVersion', 'challenger', p_publication->'weights', p_publication->'symbolWeights', p_publication->'modes', p_publication->'symbolModes', p_publication->'selectedParameters', case when coalesce((p_publication->>'bootstrapChampion')::boolean, false) then 'bootstrap_historical_champion' else 'automatic_weekly_challenger' end, v_champion.id, coalesce((p_publication->'reportSummary'->>'portfolioMaxDrawdown')::numeric, 0.1))
  returning id into v_id;
  update public.sol_weight_versions set status = 'rolled_back' where status = 'champion';
  update public.sol_weight_versions set status = 'champion', activated_at = now() where id = v_id;
  update public.sol_research_reports set status = 'rolled_back' where status = 'champion';
  insert into public.sol_research_reports(report_version, status, report, weight_version_id)
  values (coalesce(p_publication->'challengerReport'->>'report_version', p_publication->>'configVersion'), 'champion', p_publication->'challengerReport', v_id)
  returning id into v_report_id;
  update public.sol_weight_versions set report_id = v_report_id where id = v_id;
  for v_evidence in select value from jsonb_array_elements(p_publication->'evidenceRows') loop
    insert into public.sol_strategy_evidence(symbol, strategy_family, config_version, evaluated_at, history_days, formal_eligible, untouched_test_trades, untouched_mean_r, untouched_max_drawdown, mean_r_lower_bound_95, robustness_score, evidence)
    values (v_evidence->>'symbol', v_evidence->>'family', p_publication->>'configVersion', now(), (v_evidence->>'history_days')::integer, coalesce((v_evidence->>'formal_eligible')::boolean, false), (v_evidence->>'untouched_test_trades')::integer, (v_evidence->>'untouched_mean_r')::numeric, (v_evidence->>'untouched_max_drawdown')::numeric, (v_evidence->>'mean_r_lower_bound_95')::numeric, (v_evidence->>'robustness_score')::numeric, v_evidence)
    on conflict (symbol, strategy_family, config_version) do update set evaluated_at = excluded.evaluated_at, history_days = excluded.history_days, formal_eligible = excluded.formal_eligible, untouched_test_trades = excluded.untouched_test_trades, untouched_mean_r = excluded.untouched_mean_r, untouched_max_drawdown = excluded.untouched_max_drawdown, mean_r_lower_bound_95 = excluded.mean_r_lower_bound_95, robustness_score = excluded.robustness_score, evidence = excluded.evidence;
  end loop;
  if p_publication ? 'lifecycle' then
    v_lifecycle := public.sol_apply_lifecycle_evaluation(coalesce((p_publication->'lifecycle'->>'allStrategiesPass')::boolean, false), false, 'weekly Champion-Challenger publication');
  end if;
  return jsonb_build_object('published', true, 'weightVersionId', v_id, 'researchReportId', v_report_id, 'version', v_version, 'rollbackPointer', v_champion.id, 'lifecycle', v_lifecycle);
end;
$function$;

create or replace function public.sol_get_active_weight_version()
returns jsonb
language sql
security definer
set search_path = public
as $function$
  select jsonb_build_object(
    'id', id,
    'version', version,
    'configVersion', config_version,
    'weights', weights,
    'symbolWeights', symbol_weights,
    'modes', family_modes,
    'symbolModes', symbol_modes,
    'selectedParameters', selected_parameters,
    'reportId', report_id,
    'report', (select report from public.sol_research_reports where id = sol_weight_versions.report_id),
    'rollbackOf', rollback_of,
    'outOfSampleMaxDrawdown', out_of_sample_max_drawdown
  )
  from public.sol_weight_versions
  where status = 'champion'
  order by version desc
  limit 1;
$function$;

do $block$
declare table_name text;
begin
  foreach table_name in array array['sol_simulation_accounts','sol_research_reports','sol_app_configurations','sol_instruments','sol_scan_runs','sol_signal_states','sol_signals','sol_simulated_positions','sol_simulated_trades','sol_strategy_evidence','sol_weight_versions','sol_outbox','sol_delivery_attempts','sol_health_events','sol_runtime_status'] loop
    execute format('alter table public.%I enable row level security', table_name);
    execute format('revoke all on table public.%I from anon, authenticated', table_name);
  end loop;
end;
$block$;
revoke all on function public.sol_reconcile_simulation(jsonb, timestamptz) from public, anon, authenticated;
revoke all on function public.sol_get_simulation_state() from public, anon, authenticated;
revoke all on function public.sol_commit_scan(jsonb) from public, anon, authenticated;
revoke all on function public.sol_claim_due_outbox(integer, timestamptz) from public, anon, authenticated;
revoke all on function public.sol_mark_outbox_failed(uuid, timestamptz, text, boolean) from public, anon, authenticated;
revoke all on function public.sol_apply_lifecycle_evaluation(boolean, boolean, text) from public, anon, authenticated;
revoke all on function public.sol_publish_weight_challenger(jsonb) from public, anon, authenticated;
revoke all on function public.sol_get_champion_research_report() from public, anon, authenticated;
revoke all on function public.sol_get_active_weight_version() from public, anon, authenticated;

commit;
