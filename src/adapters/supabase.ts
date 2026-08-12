import { redactSecrets } from "../security.js";
import type { HistoricalEvidence, Instrument, OutboxMessage, Repository, SignalCandidate, SignalState, SimulationState, WeightVersion } from "../types.js";

interface SupabaseRow {
  [key: string]: unknown;
}

export class SupabaseRestRepository implements Repository {
  public constructor(private readonly baseUrl: string, private readonly serviceRoleKey: string) {}

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("apikey", this.serviceRoleKey);
    headers.set("Content-Type", "application/json");
    if (this.serviceRoleKey.startsWith("sb_secret_")) headers.delete("Authorization");
    else headers.set("Authorization", `Bearer ${this.serviceRoleKey}`);
    const response = await fetch(`${this.baseUrl}/rest/v1/${path}`, {
      ...init,
      headers,
    });
    if (!response.ok) {
      const detail = redactSecrets(await response.text(), [this.serviceRoleKey]);
      throw new Error(`Supabase request failed (${response.status}): ${detail.slice(0, 300)}`);
    }
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  private async rpc<T>(name: string, payload: Record<string, unknown>): Promise<T> {
    return this.request<T>(`rpc/${name}`, { method: "POST", body: JSON.stringify(payload) });
  }

  async getPublicActivity(limit = 10): Promise<{
    emails: Array<{ subject: string; status: string; sentAt: string | null; createdAt: string }>;
    settlements: Array<{ symbol: string; direction: string; entryTime: string; exitTime: string; entryPrice: number; exitPrice: number; resultR: number; exitReason: string }>;
  }> {
    const safeLimit = Math.max(1, Math.min(20, Math.floor(limit)));
    const [emails, settlements] = await Promise.all([
      this.request<SupabaseRow[]>(`sol_outbox?select=subject,status,delivered_at,created_at&order=created_at.desc&limit=${safeLimit}`),
      this.request<SupabaseRow[]>(`sol_simulated_trades?select=symbol,direction,entry_time,exit_time,entry_price,exit_price,net_r,exit_reason&shadow_only=eq.false&order=exit_time.desc&limit=${safeLimit}`),
    ]);
    return {
      emails: emails.map((row) => ({ subject: String(row.subject), status: String(row.status), sentAt: row.delivered_at ? String(row.delivered_at) : null, createdAt: String(row.created_at) })),
      settlements: settlements.map((row) => ({ symbol: String(row.symbol), direction: String(row.direction), entryTime: String(row.entry_time), exitTime: String(row.exit_time), entryPrice: Number(row.entry_price), exitPrice: Number(row.exit_price), resultR: Number(row.net_r), exitReason: String(row.exit_reason) })),
    };
  }

  async getActiveWeights(): Promise<WeightVersion | null> {
    const rows = await this.request<SupabaseRow[]>("sol_weight_versions?status=eq.champion&order=version.desc&limit=1");
    const row = rows[0];
    if (!row) return null;
    return {
      id: String(row.id),
      version: Number(row.version),
      configVersion: String(row.config_version ?? ""),
      status: "champion",
      weights: row.weights as WeightVersion["weights"],
      symbolWeights: (row.symbol_weights ?? {}) as Record<string, WeightVersion["weights"]>,
      modes: (row.family_modes ?? {}) as NonNullable<WeightVersion["modes"]>,
      symbolModes: (row.symbol_modes ?? {}) as NonNullable<WeightVersion["symbolModes"]>,
      selectedParameters: (row.selected_parameters ?? {}) as NonNullable<WeightVersion["selectedParameters"]>,
      reportId: row.report_id === null || row.report_id === undefined ? null : String(row.report_id),
      source: String(row.source),
      evidenceId: row.evidence_id === null ? null : String(row.evidence_id),
      activatedAt: row.activated_at === null ? null : Date.parse(String(row.activated_at)),
      rollbackOf: row.rollback_of === null ? null : String(row.rollback_of),
      outOfSampleMaxDrawdown: Number(row.out_of_sample_max_drawdown ?? 0.1),
    };
  }

  async getStrategyEvidence(symbols: string[]): Promise<Map<string, HistoricalEvidence>> {
    if (symbols.length === 0) return new Map();
    const encoded = symbols.map((symbol) => `"${symbol.replaceAll('"', "")}"`).join(",");
    const rows = await this.request<SupabaseRow[]>(`sol_strategy_evidence?symbol=in.(${encodeURIComponent(encoded)})&order=evaluated_at.desc`);
    const output = new Map<string, HistoricalEvidence>();
    for (const row of rows) {
      const key = `${String(row.symbol)}:${String(row.strategy_family)}`;
      if (output.has(key)) continue;
      output.set(key, {
        historyDays: Number(row.history_days),
        formalEligible: Boolean(row.formal_eligible),
        untouchedTestTrades: Number(row.untouched_test_trades),
        untouchedMeanR: Number(row.untouched_mean_r),
        untouchedMaxDrawdown: Number(row.untouched_max_drawdown),
        meanRLowerBound95: Number(row.mean_r_lower_bound_95),
        robustnessScore: Number(row.robustness_score),
      });
    }
    return output;
  }

  async getSignalStates(keys: string[]): Promise<Map<string, SignalState>> {
    if (keys.length === 0) return new Map();
    const encoded = keys.map((key) => `"${key.replaceAll('"', "")}"`).join(",");
    const rows = await this.request<SupabaseRow[]>(`sol_signal_states?state_key=in.(${encodeURIComponent(encoded)})`);
    return new Map(rows.map((row) => [String(row.state_key), {
      key: String(row.state_key),
      symbol: String(row.symbol),
      family: row.strategy_family as SignalState["family"],
      direction: row.direction as SignalState["direction"],
      active: Boolean(row.active),
      shadowOnly: Boolean(row.shadow_only ?? false),
      lastInvalidatedAt: row.last_invalidated_at === null ? null : Date.parse(String(row.last_invalidated_at)),
      cooldownUntil: row.cooldown_until === null ? null : Date.parse(String(row.cooldown_until)),
    }]));
  }

  async getSystemHalt(): Promise<boolean> {
    const [configRows, lifecycleRows, accountRows] = await Promise.all([
      this.request<SupabaseRow[]>("sol_app_configurations?config_key=eq.system_halt&select=config_value&limit=1"),
      this.request<SupabaseRow[]>("sol_runtime_status?status_key=eq.lifecycle&select=status_value&limit=1"),
      this.request<SupabaseRow[]>("sol_simulation_accounts?id=eq.true&select=halted&limit=1"),
    ]);
    const configured = Boolean((configRows[0]?.config_value as { halted?: boolean } | undefined)?.halted);
    const lifecycle = (lifecycleRows[0]?.status_value as { halted?: boolean } | undefined)?.halted === true;
    return configured || lifecycle || accountRows[0]?.halted === true;
  }

  async getLiveDrawdown(): Promise<number> {
    const rows = await this.request<SupabaseRow[]>("sol_simulation_accounts?id=eq.true&select=drawdown&limit=1");
    return Number(rows[0]?.drawdown ?? 0);
  }

  async getOpenPositionCounts(): Promise<{ total: number; long: number; short: number }> {
    const rows = await this.request<SupabaseRow[]>("sol_simulated_positions?status=in.(pending,open)&shadow_only=eq.false&select=direction");
    const long = rows.filter((row) => row.direction === "long").length;
    const short = rows.filter((row) => row.direction === "short").length;
    return { total: rows.length, long, short };
  }

  async getRiskReference(): Promise<{ btcReturns: number[]; eligibleMarketReturns: number[]; returnsBySymbol?: Record<string, number[]>; baselineVersion?: string; lastObservationAt?: number | null; pauseUntil: number | null }> {
    const rows = await this.request<SupabaseRow[]>("sol_runtime_status?status_key=eq.global_risk&select=status_value&limit=1");
    const value = rows[0]?.status_value as { btcReturns?: unknown; eligibleMarketReturns?: unknown; returnsBySymbol?: unknown; baselineVersion?: unknown; lastObservationAt?: unknown; pauseUntil?: unknown } | undefined;
    return {
      btcReturns: Array.isArray(value?.btcReturns) ? value.btcReturns.filter((item): item is number => typeof item === "number") : [],
      eligibleMarketReturns: Array.isArray(value?.eligibleMarketReturns) ? value.eligibleMarketReturns.filter((item): item is number => typeof item === "number") : [],
      returnsBySymbol: value?.returnsBySymbol && typeof value.returnsBySymbol === "object" ? Object.fromEntries(Object.entries(value.returnsBySymbol as Record<string, unknown>).filter(([, series]) => Array.isArray(series)).map(([symbol, series]) => [symbol, (series as unknown[]).filter((item): item is number => typeof item === "number")])) : {},
      baselineVersion: typeof value?.baselineVersion === "string" ? value.baselineVersion : "",
      lastObservationAt: typeof value?.lastObservationAt === "number" ? value.lastObservationAt : null,
      pauseUntil: typeof value?.pauseUntil === "number" ? value.pauseUntil : null,
    };
  }

  async setRiskReference(input: { btcReturns: number[]; eligibleMarketReturns: number[]; returnsBySymbol?: Record<string, number[]>; baselineVersion?: string; lastObservationAt?: number | null; pauseUntil: number | null }): Promise<void> {
    await this.rpc("sol_set_runtime_status", {
      p_key: "global_risk",
      p_value: {
        btcReturns: input.btcReturns.slice(-8640),
        eligibleMarketReturns: input.eligibleMarketReturns.slice(-8640),
        returnsBySymbol: Object.fromEntries(Object.entries(input.returnsBySymbol ?? {}).map(([symbol, values]) => [symbol, values.slice(-8640)])),
        baselineVersion: input.baselineVersion ?? "risk-v1",
        lastObservationAt: input.lastObservationAt ?? null,
        pauseUntil: input.pauseUntil,
      },
    });
  }

  async setRiskPauseUntil(until: number): Promise<void> {
    const current = await this.getRiskReference();
    await this.setRiskReference({ ...current, pauseUntil: until });
  }

  async rollbackActiveWeight(reason: string): Promise<void> {
    await this.rpc("sol_rollback_active_weight", { p_reason: reason.slice(0, 500) });
  }

  async getCurrentConfigVersion(): Promise<string | null> {
    const rows = await this.request<SupabaseRow[]>("sol_app_configurations?config_key=eq.active_strategy_config&select=config_value&limit=1");
    const value = rows[0]?.config_value as { configVersion?: unknown } | undefined;
    return typeof value?.configVersion === "string" ? value.configVersion : null;
  }

  async getSimulationState(): Promise<SimulationState> {
    const result = await this.rpc<SupabaseRow>("sol_get_simulation_state", {});
    const value = (Array.isArray(result) ? result[0] : result) as SupabaseRow | undefined;
    const rows = Array.isArray(value?.positions) ? value.positions as SupabaseRow[] : [];
    const positions = rows.map((row) => ({
      id: String(row.id ?? ""),
      signalId: row.signal_id === null || row.signal_id === undefined ? null : String(row.signal_id),
      stateKey: String(row.state_key ?? ""),
      symbol: String(row.symbol ?? ""),
      direction: row.direction as SimulationState["positions"][number]["direction"],
      status: row.status as SimulationState["positions"][number]["status"],
      shadowOnly: Boolean(row.shadow_only ?? false),
      triggerTime: row.trigger_time ? Date.parse(String(row.trigger_time)) : 0,
      expiresAt: row.expires_at ? Date.parse(String(row.expires_at)) : 0,
      entryValidUntil: row.entry_valid_until ? Date.parse(String(row.entry_valid_until)) : 0,
      chaseBoundary: Number(row.chase_boundary ?? 0),
      technicalInvalidation: Number(row.technical_invalidation ?? 0),
      referenceStop: Number(row.reference_stop ?? 0),
      referenceTarget: Number(row.reference_target ?? 0),
      riskBudget: Number(row.risk_budget ?? 0),
      entryTime: row.entry_time ? Date.parse(String(row.entry_time)) : null,
      entryPrice: row.entry_price === null || row.entry_price === undefined ? null : Number(row.entry_price),
      exitTime: row.exit_time ? Date.parse(String(row.exit_time)) : null,
      exitPrice: row.exit_price === null || row.exit_price === undefined ? null : Number(row.exit_price),
      exitReason: row.exit_reason as SimulationState["positions"][number]["exitReason"] ?? null,
      netR: row.net_r === null || row.net_r === undefined ? null : Number(row.net_r),
      feesR: Number(row.fees_r ?? 0),
      accruedFundingR: Number(row.accrued_funding_r ?? 0),
      lastFundingTime: row.last_funding_time ? Date.parse(String(row.last_funding_time)) : null,
      configVersion: row.config_version === null || row.config_version === undefined ? null : String(row.config_version),
      evidenceId: row.evidence_id === null || row.evidence_id === undefined ? null : String(row.evidence_id),
      weightVersionId: row.weight_version_id === null || row.weight_version_id === undefined ? null : String(row.weight_version_id),
      lastEvaluatedCloseTime: row.last_evaluated_close_time ? Date.parse(String(row.last_evaluated_close_time)) : null,
    }));
    const currentNetRs = Array.isArray(value?.current_net_rs) ? value.current_net_rs.filter((item): item is number => typeof item === "number") : [];
    const priorNetRs = Array.isArray(value?.prior_net_rs) ? value.prior_net_rs.filter((item): item is number => typeof item === "number") : [];
    return {
      equity: Number(value?.equity ?? 100_000),
      peakEquity: Number(value?.peak_equity ?? value?.peakEquity ?? 100_000),
      drawdown: Number(value?.drawdown ?? 0),
      closedSignals: Number(value?.closed_signals ?? value?.closedSignals ?? 0),
      positions,
      halt: Boolean(value?.halt ?? false),
      lastFundingObservationAt: value?.last_funding_observation_at ? Date.parse(String(value.last_funding_observation_at)) : null,
      versionStats: { currentNetRs, priorNetRs },
    };
  }

  async commitScan(input: {
    startedAt: number;
    completedAt: number;
    configVersion: string;
    instruments: Instrument[];
    signals: SignalCandidate[];
    diagnostics: Record<string, unknown>;
    email: { recipient: string; subject: string; bodyText: string } | null;
    observedStateKeys?: string[];
    simulation?: Parameters<Repository["commitScan"]>[0]["simulation"];
  }): Promise<{ scanRunId: string; signals: SignalCandidate[]; outboxId: string | null }> {
    const result = await this.rpc<unknown>("sol_commit_scan", {
      p_payload: {
        started_at: new Date(input.startedAt).toISOString(),
        completed_at: new Date(input.completedAt).toISOString(),
        config_version: input.configVersion,
        instruments: input.instruments,
        signals: input.signals,
        diagnostics: input.diagnostics,
        email: input.email,
        observed_state_keys: input.observedStateKeys ?? [],
        simulation: input.simulation ? { ...input.simulation, weightVersionId: input.simulation.weightVersionId ?? null } : null,
      },
    });
    const row = Array.isArray(result) ? result[0] as SupabaseRow | undefined : result as SupabaseRow;
    if (!row?.scan_run_id) throw new Error("Supabase sol_commit_scan returned no scan id");
    return { scanRunId: String(row.scan_run_id), signals: input.signals, outboxId: row.outbox_id ? String(row.outbox_id) : null };
  }

  async recordHealthEvent(input: { kind: "scan_failure" | "scan_recovery" | "halt" | "rollback"; detail: string; occurredAt: number }): Promise<void> {
    await this.rpc("sol_record_health_event", { p_type: input.kind, p_detail: input.detail.slice(0, 2000), p_occurred_at: new Date(input.occurredAt).toISOString() });
  }

  async claimDueOutbox(limit: number, now: number): Promise<OutboxMessage[]> {
    const result = await this.rpc<SupabaseRow[]>("sol_claim_due_outbox", { p_limit: limit, p_now: new Date(now).toISOString() });
    return result.map((row) => ({
      id: String(row.id),
      idempotencyKey: String(row.idempotency_key),
      recipient: String(row.recipient),
      subject: String(row.subject),
      bodyText: String(row.body_text),
      attemptCount: Number(row.attempt_count),
      expiresAt: Date.parse(String(row.expires_at)),
    }));
  }

  async markOutboxDelivered(id: string, deliveredAt: number): Promise<void> {
    await this.rpc("sol_mark_outbox_delivered", { p_id: id, p_delivered_at: new Date(deliveredAt).toISOString() });
  }

  async markOutboxFailed(id: string, nextAttemptAt: number, error: string, terminal = false): Promise<void> {
    await this.rpc("sol_mark_outbox_failed", { p_id: id, p_next_attempt_at: new Date(nextAttemptAt).toISOString(), p_error: error.slice(0, 1000), p_terminal: terminal });
  }
}
