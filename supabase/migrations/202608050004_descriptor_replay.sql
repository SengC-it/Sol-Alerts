begin;

-- Fail closed if the hosted/local database does not expose pgcrypto in the
-- schema used by the qualified calls below.  The extension is installed by
-- the core migration; do not fall back to an unqualified, search_path-based
-- lookup inside SECURITY DEFINER functions.
do $$
begin
  if not exists (
    select 1
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'extensions'
      and p.proname = 'digest'
  ) then
    raise exception 'pgcrypto.digest must be installed in extensions schema';
  end if;
end
$$;

-- The evaluated descriptor is immutable evidence: the database stores the
-- exact hash and payload that produced the untouched Champion/Challenger
-- comparison, not a post-hoc recomputation of weights.
alter table public.sol_weight_versions add column if not exists descriptor_hash text not null default '';
alter table public.sol_weight_versions add column if not exists descriptor jsonb not null default '{}'::jsonb;
alter table public.sol_weight_versions add column if not exists descriptor_content_hash text not null default '';
alter table public.sol_research_reports add column if not exists descriptor_hash text not null default '';
alter table public.sol_research_reports add column if not exists descriptor_content_hash text not null default '';

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
  v_descriptor_hash text := p_publication->>'descriptorHash';
  v_descriptor_content_hash text;
begin
  v_descriptor_content_hash := encode(extensions.digest(coalesce((p_publication->'candidateDescriptor')::text, ''), 'sha256'), 'hex');
  select config_value->>'configVersion' into v_expected_config from public.sol_app_configurations where config_key = 'active_strategy_config' and is_active order by updated_at desc limit 1;
  if v_expected_config is not null and v_expected_config <> p_publication->>'configVersion' then
    raise exception 'incompatible publication config version';
  end if;
  if p_publication->'reportLinkage'->>'configVersion' is distinct from p_publication->>'configVersion' then
    raise exception 'publication report linkage version mismatch';
  end if;
  if jsonb_typeof(p_publication->'candidateDescriptor') <> 'object'
     or v_descriptor_hash is null
     or v_descriptor_hash is distinct from p_publication->'candidateDescriptor'->>'descriptorHash'
     or v_descriptor_hash is distinct from p_publication->'challengerReport'->>'descriptorHash'
     or v_descriptor_hash is distinct from p_publication->'reportLinkage'->>'evaluatedDescriptorHash'
     or jsonb_typeof(p_publication->'challengerReport') <> 'object'
     or p_publication->'challengerReport'->'candidate_descriptor' is distinct from p_publication->'candidateDescriptor'
     or p_publication->'weights' is distinct from p_publication->'candidateDescriptor'->'weights'
     or p_publication->'modes' is distinct from p_publication->'candidateDescriptor'->'modes'
     or p_publication->'symbolWeights' is distinct from p_publication->'candidateDescriptor'->'symbolWeights'
     or p_publication->'symbolModes' is distinct from p_publication->'candidateDescriptor'->'symbolModes'
     or p_publication->'selectedParameters' is distinct from p_publication->'candidateDescriptor'->'selectedParameters'
     or jsonb_typeof(p_publication->'evaluation') <> 'object'
     or jsonb_typeof(p_publication->'weights') <> 'object'
     or jsonb_typeof(p_publication->'modes') <> 'object'
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
    raise exception 'incomplete or non-frozen descriptor publication payload';
  end if;
  if not v_published then
    v_lifecycle := public.sol_apply_lifecycle_evaluation(coalesce((p_publication->'lifecycle'->>'allStrategiesPass')::boolean, false), false, 'weekly Champion-Challenger evaluation failed');
    return jsonb_build_object('published', false, 'reason', 'champion-challenger gates failed', 'lifecycle', v_lifecycle);
  end if;
  select * into v_champion from public.sol_weight_versions where status = 'champion' order by version desc limit 1 for update;
  if found and (
       jsonb_typeof(v_champion.descriptor) <> 'object'
       or v_champion.descriptor->>'descriptorHash' is distinct from v_champion.descriptor_hash
       or v_champion.descriptor_content_hash is distinct from encode(extensions.digest(v_champion.descriptor::text, 'sha256'), 'hex')
       or v_champion.descriptor->'weights' is distinct from v_champion.weights
       or v_champion.descriptor->'modes' is distinct from v_champion.family_modes
       or v_champion.descriptor->'symbolWeights' is distinct from v_champion.symbol_weights
       or v_champion.descriptor->'symbolModes' is distinct from v_champion.symbol_modes
       or v_champion.descriptor->'selectedParameters' is distinct from v_champion.selected_parameters
     ) then
    raise exception 'active champion descriptor content or hash is inconsistent';
  end if;
  if found and p_publication->'evaluation'->>'champion_descriptor_hash' is distinct from v_champion.descriptor_hash then
    raise exception 'evaluated champion descriptor does not match active champion';
  end if;
  if not found and not coalesce((p_publication->>'bootstrapChampion')::boolean, false) then
    raise exception 'champion is required unless explicit bootstrap publication is requested';
  end if;
  select coalesce(max(version), 0) + 1 into v_version from public.sol_weight_versions;
  insert into public.sol_weight_versions(version, config_version, status, weights, symbol_weights, family_modes, symbol_modes, selected_parameters, descriptor_hash, descriptor, descriptor_content_hash, source, rollback_of, out_of_sample_max_drawdown)
  values (v_version, p_publication->>'configVersion', 'challenger', p_publication->'weights', p_publication->'symbolWeights', p_publication->'modes', p_publication->'symbolModes', p_publication->'selectedParameters', v_descriptor_hash, p_publication->'candidateDescriptor', v_descriptor_content_hash, case when coalesce((p_publication->>'bootstrapChampion')::boolean, false) then 'bootstrap_historical_champion' else 'automatic_weekly_challenger' end, v_champion.id, coalesce((p_publication->'reportSummary'->>'portfolioMaxDrawdown')::numeric, 0.1))
  returning id into v_id;
  update public.sol_weight_versions set status = 'rolled_back' where status = 'champion';
  update public.sol_weight_versions set status = 'champion', activated_at = now() where id = v_id;
  update public.sol_research_reports set status = 'rolled_back' where status = 'champion';
  insert into public.sol_research_reports(report_version, status, report, descriptor_hash, descriptor_content_hash, weight_version_id)
  values (coalesce(p_publication->'challengerReport'->>'report_version', p_publication->>'configVersion'), 'champion', p_publication->'challengerReport', v_descriptor_hash, v_descriptor_content_hash, v_id)
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
  return jsonb_build_object('published', true, 'weightVersionId', v_id, 'researchReportId', v_report_id, 'version', v_version, 'rollbackPointer', v_champion.id, 'descriptorHash', v_descriptor_hash, 'lifecycle', v_lifecycle);
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
    'descriptorHash', descriptor_hash,
    'descriptorContentHash', descriptor_content_hash,
    'descriptor', descriptor,
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

revoke all on function public.sol_publish_weight_challenger(jsonb) from public, anon, authenticated;
revoke all on function public.sol_get_active_weight_version() from public, anon, authenticated;

commit;


