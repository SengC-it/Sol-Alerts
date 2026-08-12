begin;

create table if not exists public.sol_risk_bootstrap_sessions (
  upload_id uuid primary key,
  baseline_version text not null,
  last_observation_at bigint not null,
  observations integer not null check (observations > 0),
  symbol_count integer not null check (symbol_count > 0),
  manifest_hash text not null check (manifest_hash ~ '^[0-9a-f]{64}$'),
  status text not null default 'uploading' check (status in ('uploading', 'committed', 'failed')),
  created_at timestamptz not null default now(),
  committed_at timestamptz,
  expires_at timestamptz not null default now() + interval '6 hours'
);

create table if not exists public.sol_risk_bootstrap_chunks (
  upload_id uuid not null references public.sol_risk_bootstrap_sessions(upload_id) on delete cascade,
  series_key text not null,
  chunk_index integer not null check (chunk_index >= 0),
  chunk_count integer not null check (chunk_count > 0),
  value_count integer not null check (value_count > 0),
  chunk_hash text not null check (chunk_hash ~ '^[0-9a-f]{64}$'),
  chunk_value jsonb not null check (jsonb_typeof(chunk_value) = 'array'),
  created_at timestamptz not null default now(),
  primary key (upload_id, series_key, chunk_index),
  check (chunk_index < chunk_count)
);

create index if not exists sol_risk_bootstrap_sessions_expires_at_idx on public.sol_risk_bootstrap_sessions(expires_at);
create index if not exists sol_risk_bootstrap_chunks_upload_id_idx on public.sol_risk_bootstrap_chunks(upload_id);

alter table public.sol_risk_bootstrap_sessions enable row level security;
alter table public.sol_risk_bootstrap_chunks enable row level security;
revoke all on table public.sol_risk_bootstrap_sessions, public.sol_risk_bootstrap_chunks from anon, authenticated;
grant select, insert, update, delete on table public.sol_risk_bootstrap_sessions, public.sol_risk_bootstrap_chunks to service_role;

create or replace function public.sol_begin_risk_bootstrap(p_upload_id uuid, p_baseline_version text, p_last_observation_at bigint, p_observations integer, p_symbol_count integer, p_manifest_hash text)
returns void language plpgsql security invoker set search_path = public as $function$
begin
  delete from public.sol_risk_bootstrap_sessions where expires_at < now() and status <> 'committed';
  insert into public.sol_risk_bootstrap_sessions(upload_id, baseline_version, last_observation_at, observations, symbol_count, manifest_hash)
  values (p_upload_id, p_baseline_version, p_last_observation_at, p_observations, p_symbol_count, p_manifest_hash)
  on conflict (upload_id) do update set expires_at = now() + interval '6 hours'
  where sol_risk_bootstrap_sessions.status = 'uploading'
    and sol_risk_bootstrap_sessions.baseline_version = excluded.baseline_version
    and sol_risk_bootstrap_sessions.last_observation_at = excluded.last_observation_at
    and sol_risk_bootstrap_sessions.observations = excluded.observations
    and sol_risk_bootstrap_sessions.symbol_count = excluded.symbol_count
    and sol_risk_bootstrap_sessions.manifest_hash = excluded.manifest_hash;
  if not found then raise exception 'risk bootstrap session metadata conflict'; end if;
end;
$function$;

create or replace function public.sol_upload_risk_bootstrap_chunk(p_upload_id uuid, p_series_key text, p_chunk_index integer, p_chunk_count integer, p_chunk_text text, p_chunk_hash text)
returns void language plpgsql security invoker set search_path = public, extensions as $function$
declare v_chunk jsonb;
begin
  if not exists (select 1 from public.sol_risk_bootstrap_sessions where upload_id = p_upload_id and status = 'uploading' and expires_at >= now()) then raise exception 'active risk bootstrap session not found'; end if;
  if p_series_key in ('baselineVersion', 'lastObservationAt', 'pauseUntil', 'returnsBySymbol') or length(p_series_key) > 100 then raise exception 'invalid risk bootstrap series key'; end if;
  if encode(extensions.digest(convert_to(p_chunk_text, 'UTF8'), 'sha256'), 'hex') <> p_chunk_hash then raise exception 'risk bootstrap chunk hash mismatch'; end if;
  begin v_chunk := p_chunk_text::jsonb; exception when others then raise exception 'risk bootstrap chunk is not valid JSON'; end;
  if jsonb_typeof(v_chunk) <> 'array' or jsonb_array_length(v_chunk) = 0 then raise exception 'risk bootstrap chunk must be a non-empty array'; end if;
  if exists (select 1 from jsonb_array_elements(v_chunk) value where jsonb_typeof(value) <> 'number') then raise exception 'risk bootstrap chunk must contain only numbers'; end if;
  insert into public.sol_risk_bootstrap_chunks(upload_id, series_key, chunk_index, chunk_count, value_count, chunk_hash, chunk_value)
  values (p_upload_id, p_series_key, p_chunk_index, p_chunk_count, jsonb_array_length(v_chunk), p_chunk_hash, v_chunk)
  on conflict (upload_id, series_key, chunk_index) do update set chunk_count = excluded.chunk_count, value_count = excluded.value_count, chunk_hash = excluded.chunk_hash, chunk_value = excluded.chunk_value, created_at = now();
end;
$function$;

create or replace function public.sol_finalize_risk_bootstrap(p_upload_id uuid)
returns void language plpgsql security invoker set search_path = public, extensions as $function$
declare
  v_session public.sol_risk_bootstrap_sessions%rowtype;
  v_manifest_hash text;
  v_series_count integer;
  v_state jsonb;
begin
  perform pg_advisory_xact_lock(hashtext('sol_finalize_risk_bootstrap'));
  select * into v_session from public.sol_risk_bootstrap_sessions where upload_id = p_upload_id and status = 'uploading' and expires_at >= now() for update;
  if not found then raise exception 'active risk bootstrap session not found'; end if;
  if exists (select 1 from public.sol_risk_bootstrap_chunks where upload_id = p_upload_id group by series_key having count(*) <> max(chunk_count) or min(chunk_index) <> 0 or max(chunk_index) <> max(chunk_count) - 1 or min(chunk_count) <> max(chunk_count) or sum(value_count) <> v_session.observations) then raise exception 'risk bootstrap chunks are incomplete or inconsistent'; end if;
  select count(distinct series_key) into v_series_count from public.sol_risk_bootstrap_chunks where upload_id = p_upload_id;
  if v_series_count <> v_session.symbol_count + 2 or not exists (select 1 from public.sol_risk_bootstrap_chunks where upload_id = p_upload_id and series_key = 'btcReturns') or not exists (select 1 from public.sol_risk_bootstrap_chunks where upload_id = p_upload_id and series_key = 'eligibleMarketReturns') then raise exception 'risk bootstrap series set is incomplete'; end if;
  select encode(extensions.digest(convert_to(string_agg(series_key || ':' || chunk_index::text || ':' || chunk_hash, '|' order by convert_to(series_key, 'UTF8'), chunk_index), 'UTF8'), 'sha256'), 'hex') into v_manifest_hash from public.sol_risk_bootstrap_chunks where upload_id = p_upload_id;
  if v_manifest_hash is distinct from v_session.manifest_hash then raise exception 'risk bootstrap manifest hash mismatch'; end if;
  with elements as (
    select c.series_key, c.chunk_index, e.value, e.ordinality from public.sol_risk_bootstrap_chunks c cross join lateral jsonb_array_elements(c.chunk_value) with ordinality as e(value, ordinality) where c.upload_id = p_upload_id
  ), series as (
    select series_key, jsonb_agg(value order by chunk_index, ordinality) as values from elements group by series_key
  )
  select jsonb_build_object('baselineVersion', v_session.baseline_version, 'lastObservationAt', v_session.last_observation_at, 'pauseUntil', null, 'btcReturns', (select values from series where series_key = 'btcReturns'), 'eligibleMarketReturns', (select values from series where series_key = 'eligibleMarketReturns'), 'returnsBySymbol', (select jsonb_object_agg(series_key, values) from series where series_key not in ('btcReturns', 'eligibleMarketReturns'))) into v_state;
  insert into public.sol_runtime_status(status_key, status_value, updated_at) values ('global_risk', v_state, now()) on conflict (status_key) do update set status_value = excluded.status_value, updated_at = now();
  update public.sol_risk_bootstrap_sessions set status = 'committed', committed_at = now() where upload_id = p_upload_id;
  delete from public.sol_risk_bootstrap_chunks where upload_id = p_upload_id;
end;
$function$;

revoke all on function public.sol_begin_risk_bootstrap(uuid, text, bigint, integer, integer, text) from public, anon, authenticated;
revoke all on function public.sol_upload_risk_bootstrap_chunk(uuid, text, integer, integer, text, text) from public, anon, authenticated;
revoke all on function public.sol_finalize_risk_bootstrap(uuid) from public, anon, authenticated;
grant execute on function public.sol_begin_risk_bootstrap(uuid, text, bigint, integer, integer, text) to service_role;
grant execute on function public.sol_upload_risk_bootstrap_chunk(uuid, text, integer, integer, text, text) to service_role;
grant execute on function public.sol_finalize_risk_bootstrap(uuid) to service_role;

commit;
