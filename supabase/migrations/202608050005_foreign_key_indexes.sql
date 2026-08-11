begin;

-- PostgreSQL does not create indexes for foreign keys automatically.  Keep
-- these definitions independently idempotent so this migration repairs an
-- already-applied schema as well as a fresh installation.  The existing
-- sol_one_position_per_signal unique index is the covering leftmost index for
-- sol_simulated_positions.signal_id, so no duplicate index is created for it.
create index if not exists sol_delivery_attempts_outbox_id_idx
  on public.sol_delivery_attempts(outbox_id);
create index if not exists sol_outbox_scan_run_id_idx
  on public.sol_outbox(scan_run_id);
create index if not exists sol_research_reports_weight_version_id_idx
  on public.sol_research_reports(weight_version_id);
create index if not exists sol_signal_states_symbol_idx
  on public.sol_signal_states(symbol);
create index if not exists sol_signals_scan_run_id_idx
  on public.sol_signals(scan_run_id);
create index if not exists sol_signals_symbol_idx
  on public.sol_signals(symbol);
create index if not exists sol_simulated_positions_symbol_idx
  on public.sol_simulated_positions(symbol);
create index if not exists sol_simulated_positions_weight_version_id_idx
  on public.sol_simulated_positions(weight_version_id);
create index if not exists sol_simulated_trades_position_id_idx
  on public.sol_simulated_trades(position_id);
create index if not exists sol_simulated_trades_signal_id_idx
  on public.sol_simulated_trades(signal_id);
create index if not exists sol_simulated_trades_weight_version_id_idx
  on public.sol_simulated_trades(weight_version_id);
create index if not exists sol_weight_versions_evidence_id_idx
  on public.sol_weight_versions(evidence_id);
create index if not exists sol_weight_versions_rollback_of_idx
  on public.sol_weight_versions(rollback_of);

commit;
