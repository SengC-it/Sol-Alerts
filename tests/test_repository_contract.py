from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]

PROJECT_TABLES = {
    "sol_app_configurations", "sol_instruments", "sol_scan_runs", "sol_signal_states", "sol_signals",
    "sol_simulated_positions", "sol_simulated_trades", "sol_strategy_evidence", "sol_weight_versions",
    "sol_outbox", "sol_delivery_attempts", "sol_health_events", "sol_runtime_status",
    "sol_simulation_accounts", "sol_research_reports",
}
PROJECT_RPCS = {
    "sol_commit_scan", "sol_claim_due_outbox", "sol_mark_outbox_delivered", "sol_mark_outbox_failed",
    "sol_set_runtime_status", "sol_record_health_event", "sol_publish_weight_version",
    "sol_rollback_weight_version", "sol_rollback_active_weight", "sol_get_champion_research_report",
    "sol_reconcile_simulation", "sol_get_simulation_state", "sol_apply_lifecycle_evaluation",
    "sol_publish_weight_challenger", "sol_get_active_weight_version",
}
UNPREFIXED_TABLES = tuple(name.removeprefix("sol_") for name in PROJECT_TABLES)
UNPREFIXED_RPCS = tuple(name.removeprefix("sol_") for name in PROJECT_RPCS)


def _migration_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted((ROOT / "supabase" / "migrations").glob("*.sql")))


def _project_foreign_keys(sql: str) -> set[tuple[str, str, str]]:
    """Extract direct project FK declarations for the schema-index contract."""
    foreign_keys: set[tuple[str, str, str]] = set()
    table_pattern = re.compile(
        r"create\s+table(?:\s+if\s+not\s+exists)?\s+public\.(sol_[a-z0-9_]+)\s*\((.*?)\);",
        flags=re.IGNORECASE | re.DOTALL,
    )
    column_pattern = re.compile(
        r"^\s*([a-z_][a-z0-9_]*)\s+[^,\n]*?\breferences\s+public\.(sol_[a-z0-9_]+)\s*\(",
        flags=re.IGNORECASE,
    )
    for table_match in table_pattern.finditer(sql):
        table = table_match.group(1).lower()
        for line in table_match.group(2).splitlines():
            column_match = column_pattern.search(line)
            if column_match:
                foreign_keys.add((table, column_match.group(1).lower(), column_match.group(2).lower()))

    alter_pattern = re.compile(
        r"alter\s+table\s+public\.(sol_[a-z0-9_]+)\s+add\s+column(?:\s+if\s+not\s+exists)?\s+"
        r"([a-z_][a-z0-9_]*)\s+[^;]*?\breferences\s+public\.(sol_[a-z0-9_]+)\s*\(",
        flags=re.IGNORECASE,
    )
    for alter_match in alter_pattern.finditer(sql):
        foreign_keys.add(
            (alter_match.group(1).lower(), alter_match.group(2).lower(), alter_match.group(3).lower())
        )
    return foreign_keys


def _project_indexes(sql: str) -> set[tuple[str, str, tuple[str, ...]]]:
    indexes: set[tuple[str, str, tuple[str, ...]]] = set()
    index_pattern = re.compile(
        r"create\s+(?:unique\s+)?index(?:\s+if\s+not\s+exists)?\s+"
        r"(sol_[a-z0-9_]+)\s+on\s+public\.(sol_[a-z0-9_]+)\s*\(([^)]*)\)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for index_match in index_pattern.finditer(sql):
        columns = tuple(part.strip().split()[0].lower() for part in index_match.group(3).split(","))
        indexes.add((index_match.group(1).lower(), index_match.group(2).lower(), columns))
    return indexes


def test_every_public_state_table_is_present_and_rls_enabled() -> None:
    migration = (ROOT / "supabase" / "migrations" / "202608050001_core.sql").read_text(encoding="utf-8")
    additive = (ROOT / "supabase" / "migrations" / "202608050003_live_simulation.sql").read_text(encoding="utf-8")
    tables = tuple(sorted(PROJECT_TABLES - {"sol_simulation_accounts", "sol_research_reports"}))
    for table in tables:
        assert f"create table if not exists public.{table}" in migration
        assert f"'{table}'" in migration
    assert "revoke all on table public.%I from anon, authenticated" in migration
    assert "security definer" in migration
    assert "create table if not exists public.sol_simulation_accounts" in additive
    assert "create table if not exists public.sol_research_reports" in additive
    assert "v_signal->>'symbol' || ':' || v_signal->>'direction'" in migration
    assert "v_signal->>'symbol' || ':' || v_signal->>'family' || ':' || v_signal->>'direction'" not in migration
    assert "alter table public.sol_simulated_positions enable row level security" not in additive
    assert "alter table public.%I enable row level security" in additive


def test_cron_schedule_has_required_scan_minutes_and_no_literal_credential() -> None:
    schedule = (ROOT / "supabase" / "migrations" / "202608050002_schedule.sql").read_text(encoding="utf-8")
    assert "1,16,31,46 * * * *" in schedule
    assert "* * * * *" in schedule
    assert "sol_alerts_cron_secret" in schedule
    assert "cron.schedule(\n      'sol_alerts_scan'" in schedule
    assert "cron.schedule(\n      'sol_alerts_delivery'" in schedule
    assert "sol-alerts-scan" not in schedule
    assert "sol-alerts-delivery" not in schedule
    assert "<long-random" not in schedule
    assert "sk-" not in schedule


def test_vercel_functions_use_default_node_runtime_and_no_obsolete_runtime_value() -> None:
    vercel_path = ROOT / "vercel.json"
    config = json.loads(vercel_path.read_text(encoding="utf-8"))
    functions = config["functions"]
    assert set(functions) == {"api/scan.ts", "api/deliver.ts", "api/health.ts"}
    assert [functions[path]["maxDuration"] for path in sorted(functions)] == [30, 10, 60]
    assert all("runtime" not in settings for settings in functions.values())
    assert "nodejs20.x" not in vercel_path.read_text(encoding="utf-8")

    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert package["engines"]["node"] == ">=20.0.0"


def test_transaction_contract_covers_rearm_simulation_capacity_and_delivery_attempts() -> None:
    additive = (ROOT / "supabase" / "migrations" / "202608050003_live_simulation.sql").read_text(encoding="utf-8")
    core = (ROOT / "supabase" / "migrations" / "202608050001_core.sql").read_text(encoding="utf-8")
    replay = (ROOT / "supabase" / "migrations" / "202608050004_descriptor_replay.sql").read_text(encoding="utf-8")
    assert "where active and not exists" in additive
    assert "cooldown_until =" in additive
    assert "where status in ('pending', 'open') and shadow_only = false" in additive
    assert "for update" in additive
    assert "weight_version_id" in additive
    assert "status = 'pending' and candidate.attempt_count < 4" in additive
    assert "p_terminal boolean default false" in additive
    assert "then 'failed' else 'pending' end" in additive
    assert "sol_publish_weight_challenger" in additive
    assert "sol_apply_lifecycle_evaluation" in additive
    assert "symbol_weights" in additive
    assert "family_modes" in additive
    assert "symbol_modes" in additive
    assert "selected_parameters" in additive
    assert "evidenceRows" in additive
    assert "claimed_at <= p_now - interval '5 minutes'" in additive
    assert "md5(coalesce((p_payload->>'config_version')" in additive
    assert "(value->>'triggerTime')" in additive
    assert "sol_get_active_weight_version" in additive
    assert "'report', (select report from public.sol_research_reports" in additive
    assert "v_state_key := v_signal->>'symbol' || ':' || v_signal->>'direction';" in additive
    assert "v_state_key := v_signal->>'symbol' || ':' || v_signal->>'family' || ':' || v_signal->>'direction';" not in additive
    assert "deliveryDate" not in (ROOT / "src" / "gates.ts").read_text(encoding="utf-8")
    assert "create schema if not exists extensions;" in core
    assert "create extension if not exists pgcrypto with schema extensions;" in core
    assert "descriptor_hash" in replay
    assert "descriptor_content_hash" in replay
    assert "extensions.digest(" in replay
    assert re.search(r"(?<![\w.])digest\s*\(", replay) is None
    assert "pgcrypto.digest must be installed in extensions schema" in replay
    assert "set search_path = public" in replay
    assert "set search_path = public," not in replay
    assert "candidateDescriptor" in replay
    assert "p_publication->'weights' is distinct from p_publication->'candidateDescriptor'->'weights'" in replay
    assert "p_publication->'modes' is distinct from p_publication->'candidateDescriptor'->'modes'" in replay
    assert "p_publication->'symbolWeights' is distinct from p_publication->'candidateDescriptor'->'symbolWeights'" in replay
    assert "p_publication->'symbolModes' is distinct from p_publication->'candidateDescriptor'->'symbolModes'" in replay
    assert "p_publication->'selectedParameters' is distinct from p_publication->'candidateDescriptor'->'selectedParameters'" in replay
    assert "p_publication->'challengerReport'->'candidate_descriptor' is distinct from p_publication->'candidateDescriptor'" in replay
    assert "active champion descriptor content or hash is inconsistent" in replay
    assert "evaluated champion descriptor does not match active champion" in replay
    assert "v_descriptor_hash is distinct from p_publication->'challengerReport'->>'descriptorHash'" in replay
    assert "'descriptor', descriptor" in replay


def test_sol_commit_scan_capacity_if_uses_parenthesized_case_expression() -> None:
    additive = (ROOT / "supabase" / "migrations" / "202608050003_live_simulation.sql").read_text(encoding="utf-8")
    start = additive.index("create or replace function public.sol_commit_scan")
    end = additive.index("create or replace function public.sol_claim_due_outbox", start)
    commit_scan = additive[start:end]
    assert "if v_formal and v_total < 6 and (case when v_signal->>'direction' = 'long' then v_long < 3 else v_short < 3 end) then" in commit_scan
    assert "if v_formal and v_total < 6 and case when" not in commit_scan
    assert commit_scan.count("begin") == commit_scan.count("end;")


def test_all_project_sql_objects_are_sol_prefixed_and_isolated() -> None:
    sql = _migration_text()
    table_references = re.findall(
        r"\b(?:create\s+table(?:\s+if\s+not\s+exists)?|alter\s+table|references|insert\s+into|update|delete\s+from|from|join)\s+public\.([a-z_][a-z0-9_]*)",
        sql,
        flags=re.IGNORECASE,
    )
    assert table_references
    assert set(table_references) <= PROJECT_TABLES
    assert all(name.startswith("sol_") for name in table_references)
    for old_name in UNPREFIXED_TABLES:
        assert re.search(rf"public\.{old_name}\b", sql, flags=re.IGNORECASE) is None
        assert re.search(rf"(?:\[|,)\s*'{old_name}'(?:\s*,|\])", sql, flags=re.IGNORECASE) is None

    declared_rpcs = re.findall(r"(?:create\s+or\s+replace\s+function|on\s+function)\s+public\.([a-z_][a-z0-9_]*)", sql, flags=re.IGNORECASE)
    assert set(declared_rpcs) == PROJECT_RPCS
    assert all(name.startswith("sol_") for name in declared_rpcs)
    for old_name in UNPREFIXED_RPCS:
        assert re.search(rf"public\.{old_name}\b", sql, flags=re.IGNORECASE) is None

    explicit_indexes = re.findall(r"create\s+(?:unique\s+)?index\s+(?:if\s+not\s+exists\s+)?([a-z_][a-z0-9_]*)", sql, flags=re.IGNORECASE)
    assert explicit_indexes and all(name.startswith("sol_") for name in explicit_indexes)
    explicit_constraints = re.findall(r"(?:add\s+constraint|drop\s+constraint(?:\s+if\s+exists)?)\s+([a-z_][a-z0-9_]*)", sql, flags=re.IGNORECASE)
    assert explicit_constraints and all(name.startswith("sol_") for name in explicit_constraints)

    schedule = (ROOT / "supabase" / "migrations" / "202608050002_schedule.sql").read_text(encoding="utf-8")
    cron_names = re.findall(r"cron\.(?:un)?schedule\(\s*'([^']+)'", schedule, flags=re.IGNORECASE)
    assert cron_names and all(name.startswith("sol_") for name in cron_names)

    adapter = (ROOT / "src" / "adapters" / "supabase.ts").read_text(encoding="utf-8")
    for table in ("sol_weight_versions", "sol_strategy_evidence", "sol_signal_states", "sol_app_configurations", "sol_runtime_status", "sol_simulation_accounts", "sol_simulated_positions"):
        assert table in adapter
    for old_name in UNPREFIXED_TABLES:
        assert f'"{old_name}?' not in adapter
    for rpc in ("sol_set_runtime_status", "sol_rollback_active_weight", "sol_get_simulation_state", "sol_commit_scan", "sol_record_health_event", "sol_claim_due_outbox", "sol_mark_outbox_delivered", "sol_mark_outbox_failed"):
        assert rpc in adapter

    publication = (ROOT / "python" / "sol_alerts" / "publish.py").read_text(encoding="utf-8")
    for rpc in ("sol_get_champion_research_report", "sol_get_active_weight_version", "sol_publish_weight_challenger"):
        assert f"rpc/{rpc}" in publication
    for old_name in ("get_champion_research_report", "get_active_weight_version", "publish_weight_challenger"):
        assert f"rpc/{old_name}" not in publication


def test_every_project_foreign_key_has_a_covering_index_and_005_repairs_existing_schemas() -> None:
    sql = _migration_text()
    foreign_keys = _project_foreign_keys(sql)
    expected_foreign_keys = {
        ("sol_signal_states", "symbol", "sol_instruments"),
        ("sol_signals", "scan_run_id", "sol_scan_runs"),
        ("sol_signals", "symbol", "sol_instruments"),
        ("sol_simulated_positions", "signal_id", "sol_signals"),
        ("sol_simulated_positions", "symbol", "sol_instruments"),
        ("sol_simulated_positions", "weight_version_id", "sol_weight_versions"),
        ("sol_simulated_trades", "position_id", "sol_simulated_positions"),
        ("sol_simulated_trades", "signal_id", "sol_signals"),
        ("sol_simulated_trades", "weight_version_id", "sol_weight_versions"),
        ("sol_weight_versions", "evidence_id", "sol_strategy_evidence"),
        ("sol_weight_versions", "rollback_of", "sol_weight_versions"),
        ("sol_outbox", "scan_run_id", "sol_scan_runs"),
        ("sol_delivery_attempts", "outbox_id", "sol_outbox"),
        ("sol_research_reports", "weight_version_id", "sol_weight_versions"),
    }
    assert foreign_keys == expected_foreign_keys

    indexes = _project_indexes(sql)
    for child, column, _parent in foreign_keys:
        assert any(table == child and columns and columns[0] == column for _name, table, columns in indexes)

    additive = (ROOT / "supabase" / "migrations" / "202608050005_foreign_key_indexes.sql").read_text(encoding="utf-8")
    expected_repair_indexes = {
        "sol_delivery_attempts_outbox_id_idx",
        "sol_outbox_scan_run_id_idx",
        "sol_research_reports_weight_version_id_idx",
        "sol_signal_states_symbol_idx",
        "sol_signals_scan_run_id_idx",
        "sol_signals_symbol_idx",
        "sol_simulated_positions_symbol_idx",
        "sol_simulated_positions_weight_version_id_idx",
        "sol_simulated_trades_position_id_idx",
        "sol_simulated_trades_signal_id_idx",
        "sol_simulated_trades_weight_version_id_idx",
        "sol_weight_versions_evidence_id_idx",
        "sol_weight_versions_rollback_of_idx",
    }
    declared_repairs = set(
        re.findall(
            r"create\s+index\s+if\s+not\s+exists\s+(sol_[a-z0-9_]+)", additive, flags=re.IGNORECASE
        )
    )
    assert declared_repairs == expected_repair_indexes
    assert "sol_simulated_positions_capacity_idx" in sql
    assert "create unique index if not exists sol_one_position_per_signal" in sql
    assert "sol_simulated_positions_signal_id_idx" not in additive
