import { adx, aggregate15mTo1h, atr, ema, median, returnsFromCloses, validateClosedCandles } from "./indicators.js";
import { clusterSymbols, combineSameDirectionCandidates, evaluateDirectionalDepthGate, evaluateUniverseGate, isHardEligible, limitClusterAlerts, rescoreForExecution, shouldPauseGlobalRisk, suppressConflictingDirections } from "./gates.js";
import { priorityForScore, scoreCandidate } from "./score.js";
import { independentBlockLowerBound, shouldEmergencyRollback, shouldStatisticalRollback } from "./lifecycle.js";
import { classifyMarketState, emptyEvidence, evaluateStrategies } from "./strategy.js";
import { reconcileSimulation, reserveFormalSignals, simulationCounts } from "./live-simulation.js";
import type { Candle, Direction, FundingEvent, Mailer, PublicMarketData, Repository, ScanDiagnostics, SignalCandidate, SignalState, SimulationState, StrategyConfig, WeightMap } from "./types.js";

const RISK_BASELINE_VERSION = "risk-v1";
const RISK_BASELINE_OBSERVATIONS = 90 * 24 * 4;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message.slice(0, 300) : "unknown runtime failure";
}

async function recordFailure(repository: Repository, now: number, error: unknown): Promise<void> {
  // Consecutive-failure counting and the single-event transition live in the
  // durable sol_runtime_status row, so concurrent Vercel instances share state.
  await repository.recordHealthEvent({ kind: "scan_failure", detail: errorMessage(error), occurredAt: now });
}

async function recordRecovery(repository: Repository, now: number): Promise<void> {
  await repository.recordHealthEvent({ kind: "scan_recovery", detail: "validated scan recovered after consecutive failures", occurredAt: now });
}

function withDeadline<T>(operation: Promise<T>, deadlineAt: number): Promise<T> {
  const remaining = deadlineAt - Date.now();
  if (remaining <= 0) return Promise.reject(new Error("scan deadline exceeded"));
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<T>((_, reject) => {
    timer = setTimeout(() => reject(new Error("scan deadline exceeded")), remaining);
  });
  return Promise.race([operation, timeout]).finally(() => {
    if (timer !== undefined) clearTimeout(timer);
  });
}

class ScanRequestBudget {
  private used = 0;

  public constructor(private readonly limit: number, private readonly deadlineAt: number) {}

  public async call<T>(label: string, operation: () => Promise<T>): Promise<T> {
    if (this.used >= this.limit) throw new Error(`scan request budget exhausted before ${label}`);
    if (Date.now() >= this.deadlineAt) throw new Error(`scan deadline exceeded before ${label}`);
    this.used += 1;
    return withDeadline(operation(), this.deadlineAt);
  }

  public guard(): void {
    if (Date.now() >= this.deadlineAt) throw new Error("scan deadline exceeded");
  }

  public get deadline(): number {
    return this.deadlineAt;
  }
}

async function mapWithConcurrency<T, U>(items: T[], concurrency: number, mapper: (item: T) => Promise<U>, deadlineAt: number): Promise<U[]> {
  const output: U[] = [];
  let cursor = 0;
  const worker = async (): Promise<void> => {
    while (cursor < items.length) {
      if (Date.now() >= deadlineAt) throw new Error("scan deadline exceeded");
      const index = cursor;
      cursor += 1;
      const item = items[index];
      if (item !== undefined) output[index] = await mapper(item);
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, Math.max(1, items.length)) }, () => worker()));
  return output;
}

function baseWeights(config: StrategyConfig, active: Awaited<ReturnType<Repository["getActiveWeights"]>>, symbol?: string): WeightMap {
  return (symbol ? active?.symbolWeights?.[symbol] : undefined) ?? active?.weights ?? config.weights.globalBase;
}

function familyMode(active: Awaited<ReturnType<Repository["getActiveWeights"]>>, symbol: string, family: SignalCandidate["family"]): "active" | "shadow" | undefined {
  return active?.symbolModes?.[symbol]?.[family] ?? active?.modes?.[family];
}

function hasCompleteSelectedParameters(active: Awaited<ReturnType<Repository["getActiveWeights"]>>, config: StrategyConfig): boolean {
  if (!active?.selectedParameters) return false;
  const allowed = {
    trend_breakout: new Set(config.strategies.trend_breakout.lookbacks.flatMap((lookback) => config.strategies.trend_breakout.closeBufferAtr.flatMap((buffer) => config.strategies.trend_breakout.volumeMultipliers.map((volume) => `lookback=${lookback};buffer=${buffer};volume=${volume}`)))),
    trend_pullback: new Set([`ema=${config.strategies.trend_pullback.emaPeriod};structure=${config.strategies.trend_pullback.structureLookback}`]),
    range_mean_reversion: new Set(config.strategies.range_mean_reversion.bollingerDeviations.flatMap((deviation) => config.strategies.range_mean_reversion.rsiThresholds.map((threshold) => `bb=${deviation};rsi=${threshold[0]}/${threshold[1]}`))),
  };
  return (['trend_breakout', 'trend_pullback', 'range_mean_reversion'] as const).every((family) => {
    const values = active.selectedParameters?.[family];
    return values !== undefined && typeof values.long === "string" && typeof values.short === "string" && allowed[family].has(values.long) && allowed[family].has(values.short);
  });
}

function annotateEvidence(candidate: SignalCandidate, evidence: ReturnType<typeof emptyEvidence>, config: StrategyConfig, spreadBps: number, depthRatio: number): SignalCandidate {
  const score = scoreCandidate({
    evidence,
    state: candidate.state,
    direction: candidate.direction,
    family: candidate.family,
    netRewardRisk: candidate.netRewardRisk,
    spreadBps,
    depthRatio,
    triggerDistanceAtr: candidate.triggerDistanceAtr ?? 0,
    markDeviationBps: Math.abs(candidate.markPrice / candidate.currentPrice - 1) * 10_000,
    config,
  });
  return {
    ...candidate,
    evidence,
    score,
    shadowOnly: candidate.shadowOnly || !evidence.formalEligible || evidence.historyDays < config.universe.minFormalHistoryDays || evidence.untouchedTestTrades < config.research.minimumTestTradesPerSymbolStrategy,
  };
}

function activationStateKey(candidate: SignalCandidate): string {
  return `${candidate.symbol}:${candidate.direction}`;
}

function filterDeduplicated(candidates: SignalCandidate[], states: Map<string, SignalState>, now: number): SignalCandidate[] {
  return candidates.filter((candidate) => {
    const state = states.get(activationStateKey(candidate));
    // Formal and shadow setups share one activation state. The shadow flag is
    // classification metadata, not a permanent deduplication barrier: after a
    // vanished setup and its cooldown, a new activation may become formal.
    return !state?.active && !(state?.cooldownUntil !== null && state?.cooldownUntil !== undefined && state.cooldownUntil > now);
  });
}

function formatPrice(value: number): string {
  return value >= 100 ? value.toFixed(2) : value >= 1 ? value.toFixed(4) : value.toFixed(8);
}

export function validateRiskReference(reference: Awaited<ReturnType<Repository["getRiskReference"]>>): void {
  const validSeries = (values: number[]): boolean => values.length >= RISK_BASELINE_OBSERVATIONS && values.every((value) => Number.isFinite(value));
  const btcSeries = reference.returnsBySymbol?.BTCUSDT ?? reference.btcReturns;
  if (reference.baselineVersion !== RISK_BASELINE_VERSION || !validSeries(reference.btcReturns) || !validSeries(reference.eligibleMarketReturns) || !validSeries(btcSeries)) {
    throw new Error("missing or corrupt 90-day risk baseline");
  }
}

function appendSeries(existing: number[], value: number): number[] {
  return [...existing, value].slice(-RISK_BASELINE_OBSERVATIONS);
}

export function renderAggregatedAlert(signals: SignalCandidate[], scanRunId?: string, config?: StrategyConfig): { subject: string; bodyText: string } {
  const lines = [
    "Sol Alerts - research signal digest",
    ...(scanRunId ? [`Scan: ${scanRunId}`] : []),
    "",
    "提醒不是自动下单，需人工判断。",
    "This is personal decision-support research, not investment advice or a trade instruction. Never rely on this message alone.",
    "Keep any liquidation level safely beyond the reference stop; this system does not recommend leverage or calculate liquidation.",
    "",
  ];
  signals.forEach((signal, index) => {
    const priority = priorityForScore(signal.score.total, config ?? ({ score: { highPriorityMinimum: 80, ordinaryMinimum: 70, historicalRobustnessMax: 30, currentSetupMax: 30, marketStateMatchMax: 25, executionQualityMax: 15 } } as StrategyConfig));
    lines.push(
      `${index + 1}. ${priority.toUpperCase()} ${signal.symbol} ${signal.direction.toUpperCase()} - ${signal.family}`,
      `Score ${signal.score.total.toFixed(1)}/100 (history ${signal.score.historicalRobustness.toFixed(1)}, setup ${signal.score.currentSetup.toFixed(1)}, state ${signal.score.marketStateMatch.toFixed(1)}, execution ${signal.score.executionQuality.toFixed(1)})`,
      `Trigger ${new Date(signal.triggerTime).toISOString()} | current ${formatPrice(signal.currentPrice)} | mark ${formatPrice(signal.markPrice)}`,
      `Entry ${formatPrice(signal.entryLow)}-${formatPrice(signal.entryHigh)} | chase boundary ${formatPrice(signal.chaseBoundary)}`,
      `Technical invalidation ${formatPrice(signal.technicalInvalidation)} | reference stop ${formatPrice(signal.referenceStop)} | target ${formatPrice(signal.referenceTarget)}`,
      `Net R:R ${signal.netRewardRisk.toFixed(2)} | expires ${new Date(signal.expiresAt).toISOString()} | confluence ${signal.confluence}/5`,
      `Untouched test: ${signal.evidence.untouchedTestTrades} trades, mean R ${signal.evidence.untouchedMeanR.toFixed(3)}, max drawdown ${(signal.evidence.untouchedMaxDrawdown * 100).toFixed(2)}%`,
      "",
    );
  });
  lines.push("No account, balance, order, news, social, on-chain, liquidation-map, or price-prediction data is used.");
  return { subject: `Sol Alerts: ${signals.length} qualified signal${signals.length === 1 ? "" : "s"}`, bodyText: lines.join("\n") };
}

function defaultSimulationState(): SimulationState {
  return { equity: 100_000, peakEquity: 100_000, drawdown: 0, closedSignals: 0, positions: [], halt: false };
}

export async function runScan(input: {
  market: PublicMarketData;
  repository: Repository;
  config: StrategyConfig;
  now?: number;
  recipient?: string;
  strategyEvaluator?: typeof evaluateStrategies;
}): Promise<{ scanRunId: string; email: { subject: string; bodyText: string } | null; diagnostics: ScanDiagnostics; signals: SignalCandidate[] }> {
  const now = input.now ?? Date.now();
    const diagnostics: ScanDiagnostics = { eligibleInstruments: 0, formalCandidates: 0, shadowCandidates: 0, suppressedByRiskOverlay: false, suppressedByConflict: 0, suppressedByCapacity: 0, failedSymbols: [] };
  try {
    const deadlineMsRaw = Number(process.env.SCAN_DEADLINE_MS ?? "55000");
    const deadlineMs = Number.isFinite(deadlineMsRaw) ? Math.max(1_000, Math.min(55_000, deadlineMsRaw)) : 55_000;
    const requestLimitRaw = Number(process.env.SCAN_REQUEST_BUDGET ?? "2048");
    const requestLimit = Number.isFinite(requestLimitRaw) ? Math.max(3, Math.min(10_000, Math.floor(requestLimitRaw))) : 2_048;
    const concurrencyRaw = Number(process.env.SCAN_CONCURRENCY ?? "16");
    const concurrency = Number.isFinite(concurrencyRaw) ? Math.max(1, Math.min(32, Math.floor(concurrencyRaw))) : 16;
    const budget = new ScanRequestBudget(requestLimit, Date.now() + deadlineMs);
    const systemHalted = await input.repository.getSystemHalt();
    const [instruments, tickers, activeWeights, riskReference, openPositions, liveDrawdown, currentConfigVersion, simulationState] = await Promise.all([
      budget.call("exchangeInfo", () => input.market.listInstruments()),
      budget.call("24h tickers", () => input.market.get24hTickers()),
      input.repository.getActiveWeights(),
      input.repository.getRiskReference(),
      input.repository.getOpenPositionCounts(),
      input.repository.getLiveDrawdown(),
      input.repository.getCurrentConfigVersion(),
      input.repository.getSimulationState ? input.repository.getSimulationState() : Promise.resolve<SimulationState | null>(null),
    ]);
    if (currentConfigVersion !== null && currentConfigVersion !== input.config.configVersion) throw new Error("active strategy config version mismatch");
    if (activeWeights && activeWeights.configVersion !== input.config.configVersion) throw new Error("active weight config version mismatch");
    if (activeWeights && !hasCompleteSelectedParameters(activeWeights, input.config)) throw new Error("active weight selection is missing or partially written");
    validateRiskReference(riskReference);
    if (activeWeights && shouldEmergencyRollback(liveDrawdown, activeWeights.outOfSampleMaxDrawdown, input.config)) {
      await input.repository.rollbackActiveWeight(`live simulated drawdown ${liveDrawdown} exceeded emergency threshold`);
      await input.repository.recordHealthEvent({ kind: "rollback", detail: "automatic emergency weight rollback", occurredAt: now });
      const committed = await input.repository.commitScan({ startedAt: now, completedAt: Date.now(), configVersion: input.config.configVersion, instruments, signals: [], diagnostics: { ...diagnostics, emergencyRollback: true, liveDrawdown }, email: null, observedStateKeys: [] });
      await recordRecovery(input.repository, now);
      return { scanRunId: committed.scanRunId, email: null, diagnostics, signals: [] };
    }
    const versionStats = simulationState?.versionStats;
    if (activeWeights && versionStats && versionStats.currentNetRs.length >= input.config.risk.statisticalRollbackMinimumClosedSignals && versionStats.priorNetRs.length > 0) {
      const currentMean = versionStats.currentNetRs.reduce((sum, value) => sum + value, 0) / versionStats.currentNetRs.length;
      const priorMean = versionStats.priorNetRs.reduce((sum, value) => sum + value, 0) / versionStats.priorNetRs.length;
      const lowerBoundDifference = independentBlockLowerBound(versionStats.currentNetRs, versionStats.priorNetRs, input.config.research.randomSeed, Math.min(2_000, input.config.research.bootstrapReplicates));
      if (shouldStatisticalRollback({ closedLiveSignals: versionStats.currentNetRs.length, newVersionMeanR: currentMean, priorVersionMeanR: priorMean, lowerBoundDifference, config: input.config })) {
        await input.repository.rollbackActiveWeight("statistical live-simulation rollback after 30 closed signals");
        await input.repository.recordHealthEvent({ kind: "rollback", detail: "automatic statistical weight rollback", occurredAt: now });
        const committed = await input.repository.commitScan({ startedAt: now, completedAt: Date.now(), configVersion: input.config.configVersion, instruments, signals: [], diagnostics: { ...diagnostics, statisticalRollback: true, lowerBoundDifference }, email: null, observedStateKeys: [] });
        await recordRecovery(input.repository, now);
        return { scanRunId: committed.scanRunId, email: null, diagnostics, signals: [] };
      }
    }

    const tickerMap = new Map(tickers.map((ticker) => [ticker.symbol, ticker]));
    // Binance's deliveryDate is lifecycle metadata, not a perpetual discriminator.
    // PERPETUAL + USDT quote/margin + status are the contract gate; a large future
    // deliveryDate used by Binance for perpetuals remains eligible.
    const universe = instruments.filter((instrument) => instrument.quoteAsset === input.config.universe.quoteAsset && instrument.marginAsset === input.config.universe.quoteAsset && instrument.contractType === input.config.universe.contractType && instrument.status === "TRADING");
    const staticEligible = universe
      .map((instrument) => ({ instrument, ticker: tickerMap.get(instrument.symbol), gate: evaluateUniverseGate(instrument, tickerMap.get(instrument.symbol), undefined, input.config, now) }))
      .filter((item) => item.gate.eligible && item.ticker !== undefined)
      .sort((left, right) => (right.ticker?.quoteVolume ?? 0) - (left.ticker?.quoteVolume ?? 0));
    diagnostics.eligibleInstruments = staticEligible.length;
    // Every symbol passing the cheap point-in-time gates is evaluated on this
    // closed bar. Budget/deadline failure aborts the transaction rather than
    // silently rotating or dropping the lower-volume tail.
    const eligible = staticEligible;
    const boundedSymbols = eligible.map((item) => item.instrument.symbol);
    const entrySymbolSet = new Set(boundedSymbols);
    const currentState = simulationState ?? defaultSimulationState();
    if (!simulationState && openPositions.total > 0) throw new Error("simulation state unavailable for active position reconciliation");
    const activeSimulationPositions = currentState.positions.filter((position) => position.status === "pending" || position.status === "open" || position.status === "shadow");
    const activePositionSymbols = [...new Set(activeSimulationPositions.map((position) => position.symbol))];
    const instrumentSymbols = new Set(instruments.map((instrument) => instrument.symbol));
    for (const symbol of activePositionSymbols) {
      if (!instrumentSymbols.has(symbol)) throw new Error(`active simulation instrument missing: ${symbol}`);
    }
    const markPrices = input.market.getMarkPrices && boundedSymbols.length > 0 ? await budget.call("batch mark prices", () => input.market.getMarkPrices!(boundedSymbols)) : new Map<string, number>();
    const evidenceByKey = await input.repository.getStrategyEvidence(boundedSymbols);
    const perSymbol = await mapWithConcurrency(eligible, concurrency, async (item) => {
      try {
        const ticker = item.ticker;
        if (!ticker) throw new Error("missing ticker after static gate");
        const candles15 = (await budget.call(`${item.instrument.symbol} 15m candles`, () => input.market.getCandles(item.instrument.symbol, "15m", 300))).filter((candle) => candle.closeTime < now - 30_000);
        validateClosedCandles(candles15, 15 * 60_000, now, 220);
        const candles1h = aggregate15mTo1h(candles15);
        validateClosedCandles(candles1h, 60 * 60_000, now, 50);
        const closePrices = candles1h.map((candle) => candle.close);
        const state = classifyMarketState({ emaFast: ema(closePrices, input.config.marketState.emaFast), emaSlow: ema(closePrices, input.config.marketState.emaSlow), adx: adx(candles1h, input.config.marketState.adxPeriod), atr: atr(candles1h, input.config.marketState.atrPeriod) }, input.config.marketState.trendThresholds[1] ?? 25, input.config.marketState.rangeThresholds[0] ?? 15);
        const markPrice = input.market.getMarkPrices
          ? markPrices.get(item.instrument.symbol)
          : (input.market.getMarkPrice ? await budget.call(`${item.instrument.symbol} mark price`, () => input.market.getMarkPrice!(item.instrument.symbol)) : ticker.lastPrice);
        if (markPrice === undefined) throw new Error("missing mark price in batch");
        if (!Number.isFinite(markPrice) || markPrice <= 0 || Math.abs(markPrice / ticker.lastPrice - 1) > 0.01) throw new Error("mark price anomaly");
        const evaluate = input.strategyEvaluator ?? evaluateStrategies;
        const symbolWeights = baseWeights(input.config, activeWeights, item.instrument.symbol);
        const rawCandidates = (['long', 'short'] as Direction[]).flatMap((direction) => evaluate({ symbol: item.instrument.symbol, direction, state, fifteenMinute: candles15, oneHour: candles1h, config: input.config, evidence: emptyEvidence(), weights: symbolWeights, ...(activeWeights?.selectedParameters ? { selectedParameters: activeWeights.selectedParameters } : {}), currentPrice: ticker.lastPrice, markPrice }))
          .map((candidate) => familyMode(activeWeights, item.instrument.symbol, candidate.family) === "shadow" ? { ...candidate, shadowOnly: true } : candidate);
        const candidates: SignalCandidate[] = [];
        if (rawCandidates.length > 0) {
          // One depth request per symbol only after a directional raw strategy result.
          const depth = await budget.call(`${item.instrument.symbol} directional depth`, () => input.market.getDepth(item.instrument.symbol));
          for (const direction of ["long", "short"] as Direction[]) {
            const directional = rawCandidates.filter((candidate) => candidate.direction === direction);
            if (directional.length === 0) continue;
            const gate = evaluateDirectionalDepthGate(item.instrument, ticker, depth, direction, input.config, now);
            if (!gate.eligible) continue;
            const depthRatio = gate.entryDepthNotionalUsdt / input.config.universe.minDepthNotionalUsdt;
            for (const candidate of directional) {
              const evidence = evidenceByKey.get(`${candidate.symbol}:${candidate.family}`) ?? emptyEvidence();
              candidates.push(rescoreForExecution(annotateEvidence(candidate, evidence, input.config, gate.spreadBps, depthRatio), gate.spreadBps, depthRatio, input.config));
            }
          }
        }
        return { symbol: item.instrument.symbol, candidates, candles: candles15, returns: returnsFromCloses(candles1h.map((candle) => candle.close)), riskReturns: returnsFromCloses(candles15.map((candle) => candle.close)) };
      } catch (error) {
        diagnostics.failedSymbols.push(`${item.instrument.symbol}:${errorMessage(error)}`);
        return { symbol: item.instrument.symbol, candidates: [], candles: [] as Candle[], returns: [], riskReturns: [] };
      }
    }, budget.deadline);
    const reconciliationOnlySymbols = activePositionSymbols.filter((symbol) => !entrySymbolSet.has(symbol));
    const reconciliationOnly = await mapWithConcurrency(reconciliationOnlySymbols, concurrency, async (symbol) => {
      try {
        const candles = (await budget.call(`${symbol} active-position 15m candles`, () => input.market.getCandles(symbol, "15m", 300))).filter((candle) => candle.closeTime < now - 30_000);
        validateClosedCandles(candles, 15 * 60_000, now, 220);
        return { symbol, candidates: [] as SignalCandidate[], candles, returns: [] as number[], riskReturns: [] as number[] };
      } catch (error) {
        diagnostics.failedSymbols.push(`${symbol}:${errorMessage(error)}`);
        return { symbol, candidates: [], candles: [] as Candle[], returns: [], riskReturns: [] };
      }
    }, budget.deadline);
    if (diagnostics.failedSymbols.length > 0) throw new Error(`scan data validation failed for ${diagnostics.failedSymbols.length} symbols`);

    const reconciliationResults = [...perSymbol, ...reconciliationOnly];

    const allCandidates = perSymbol.flatMap((item) => item.candidates);
    const combined = combineSameDirectionCandidates(allCandidates);
    const conflicts = suppressConflictingDirections(combined);
    diagnostics.suppressedByConflict = conflicts.suppressed;
    // A direction conflict is suppressed, not observed. This prevents a
    // conflicting long/short pair from keeping either activation alive.
    const observedStateKeys = conflicts.kept.map(activationStateKey);
    const states = await input.repository.getSignalStates([...new Set(observedStateKeys)]);
    const deduplicated = filterDeduplicated(conflicts.kept, states, now);

    const currentBySymbol = new Map(perSymbol.map((item) => [item.symbol, item]));
    const btcItem = currentBySymbol.get("BTCUSDT");
    if (!btcItem || btcItem.riskReturns.length === 0) throw new Error("BTCUSDT observation missing for global risk overlay");
    const observationAt = Math.max(...perSymbol.map((item) => item.candles.at(-1)?.closeTime ?? 0));
    const previousObservationAt = riskReference.lastObservationAt ?? 0;
    const shouldAppend = observationAt > previousObservationAt;
    const btcReturns = shouldAppend ? appendSeries(riskReference.btcReturns, btcItem.riskReturns.at(-1) ?? 0) : riskReference.btcReturns;
    const marketCurrent = median(perSymbol.map((item) => item.riskReturns.at(-1)).filter((value): value is number => value !== undefined));
    const eligibleMarketReturns = shouldAppend ? appendSeries(riskReference.eligibleMarketReturns, marketCurrent) : riskReference.eligibleMarketReturns;
    const returnsBySymbol: Record<string, number[]> = { ...(riskReference.returnsBySymbol ?? {}) };
    if (shouldAppend) {
      for (const item of perSymbol) {
        const current = item.returns.at(-1);
        if (current !== undefined) returnsBySymbol[item.symbol] = appendSeries(returnsBySymbol[item.symbol] ?? [], current);
      }
    }
    const risk = shouldPauseGlobalRisk({ btcReturns, eligibleMarketReturns, now, pauseUntil: riskReference.pauseUntil, percentileLevel: input.config.risk.extremeReturnPercentile });
    const fundingStart = currentState.lastFundingObservationAt ?? Math.max(0, observationAt - 7 * 24 * 60 * 60_000);
    const fundingSymbols = [...new Set([...boundedSymbols, ...activePositionSymbols])];
    let fundingEvents: FundingEvent[] = [];
    if (activeSimulationPositions.length > 0) {
      if (!input.market.getFundingEvents) throw new Error("funding adapter unavailable while simulated positions are active");
      if (fundingStart > observationAt) throw new Error("funding observation watermark is ahead of closed market data");
      if (fundingStart < observationAt) fundingEvents = await budget.call("batch funding events", () => input.market.getFundingEvents!(fundingSymbols, fundingStart, observationAt));
      if (!fundingEvents.every((event) => fundingSymbols.includes(event.symbol) && Number.isInteger(event.fundingTime) && event.fundingTime >= fundingStart && event.fundingTime <= observationAt && Number.isFinite(event.fundingRate) && Math.abs(event.fundingRate) <= 1 && (event.markPrice === undefined || (Number.isFinite(event.markPrice) && event.markPrice > 0)))) {
        throw new Error("malformed or out-of-window funding event");
      }
    } else if (openPositions.total > 0) {
      throw new Error("active positions are not present in simulation state");
    }
    const fundingBySymbol = new Map<string, FundingEvent[]>();
    for (const event of fundingEvents) fundingBySymbol.set(event.symbol, [...(fundingBySymbol.get(event.symbol) ?? []), event]);
    const simulationBars = reconciliationResults.filter((item) => item.candles.length > 0).map((item) => ({ symbol: item.symbol, candles: item.candles, fundingEvents: fundingBySymbol.get(item.symbol) ?? [] }));
    budget.guard();
    // Reconcile exits, expiries, and newly accrued funding before selecting
    // capacity. The second transition below only appends this scan's signals.
    const reconciledForCapacity = reconcileSimulation({ state: currentState, formalSignals: [], allSignals: [], bars: simulationBars, now, config: input.config, weightVersionId: activeWeights?.id ?? null });
    const formal = systemHalted ? [] : deduplicated.filter((candidate) => candidate.score.total >= input.config.score.ordinaryMinimum && isHardEligible(candidate, now, input.config));
    const capacity = reserveFormalSignals(formal, simulationState ? simulationCounts(reconciledForCapacity) : openPositions, input.config);
    diagnostics.suppressedByCapacity = capacity.suppressed.length;
    const clusterReturns = new Map<string, number[]>(Object.entries(returnsBySymbol));
    const clusters = clusterSymbols(boundedSymbols, clusterReturns, input.config.risk.correlationThreshold);
    const qualified = systemHalted || risk ? [] : limitClusterAlerts(capacity.accepted, clusters, input.config.risk.maxClusterSignalsPerDirection).slice(0, input.config.execution.maxSignalsPerEmail);
    diagnostics.suppressedByRiskOverlay = risk;
    (diagnostics as ScanDiagnostics & { halted?: boolean }).halted = systemHalted;
    diagnostics.formalCandidates = qualified.length;
    diagnostics.shadowCandidates = Math.max(0, allCandidates.length - qualified.length);
    const pauseUntil = risk ? Math.max(riskReference.pauseUntil ?? 0, now + input.config.risk.extremePauseMinutes * 60_000) : riskReference.pauseUntil !== null && riskReference.pauseUntil > now ? riskReference.pauseUntil : null;
    const emailDraft = qualified.length > 0 && input.recipient ? renderAggregatedAlert(qualified, undefined, input.config) : null;
    const qualifiedKeys = new Set(qualified.map(activationStateKey));
    // Only a fresh activation creates a signal/position. Current candidates
    // still feed observedStateKeys below so a sustained setup remains active;
    // persisting them again would inflate shadow evidence on every scan.
    const persistedSignals = deduplicated.map((candidate) => qualifiedKeys.has(activationStateKey(candidate)) && !risk ? candidate : { ...candidate, shadowOnly: true });
    budget.guard();
    const simulation = reconcileSimulation({ state: reconciledForCapacity, formalSignals: qualified, allSignals: persistedSignals, bars: simulationBars, now, config: input.config, weightVersionId: activeWeights?.id ?? null });
    budget.guard();
    const committed = await input.repository.commitScan({
      startedAt: now,
      completedAt: Date.now(),
      configVersion: input.config.configVersion,
      instruments: universe,
      signals: persistedSignals,
      observedStateKeys,
      simulation: { stateKeysObserved: observedStateKeys, bars: simulationBars, fundingObservationAt: observationAt, weightVersionId: activeWeights?.id ?? null, riskReference: { btcReturns, eligibleMarketReturns, returnsBySymbol, baselineVersion: riskReference.baselineVersion ?? RISK_BASELINE_VERSION, lastObservationAt: shouldAppend ? observationAt : previousObservationAt, pauseUntil } },
      diagnostics: { ...diagnostics, riskOverlay: risk, recipient: input.recipient ?? null, simulationDrawdown: simulation.drawdown, simulationClosedSignals: simulation.closedSignals },
      email: emailDraft && input.recipient ? { recipient: input.recipient, ...emailDraft } : null,
    });
    await recordRecovery(input.repository, now);
    return { scanRunId: committed.scanRunId, email: qualified.length > 0 ? renderAggregatedAlert(qualified, committed.scanRunId, input.config) : null, diagnostics, signals: persistedSignals };
  } catch (error) {
    await recordFailure(input.repository, now, error);
    throw new Error(`Scan failed closed: ${errorMessage(error)}`);
  }
}

export async function deliverDueOutbox(input: { repository: Repository; mailer: Mailer; limit: number; now?: number }): Promise<{ delivered: number; skippedExpired: number; failed: number }> {
  const now = input.now ?? Date.now();
  const messages = await input.repository.claimDueOutbox(input.limit, now);
  let delivered = 0;
  let skippedExpired = 0;
  let failed = 0;
  for (const message of messages) {
    if (message.expiresAt <= now) {
      skippedExpired += 1;
      await input.repository.markOutboxFailed(message.id, now, "expired signal not delivered", true);
      continue;
    }
    try {
      await input.mailer.send({ recipient: message.recipient, subject: message.subject, bodyText: message.bodyText, idempotencyKey: message.idempotencyKey });
      delivered += 1;
      await input.repository.markOutboxDelivered(message.id, now);
    } catch (error) {
      failed += 1;
      const retryMinutes: Record<number, number> = { 1: 1, 2: 3, 3: 5 };
      const delay = retryMinutes[message.attemptCount] ?? 5;
      const nextAttemptAt = now + delay * 60_000;
      // attemptCount includes the current claim: attempt 1 is the initial
      // delivery, followed by retries at +1m, +3m, and +5m. Attempt 4 is the
      // last send and is terminal when it fails.
      const terminal = message.attemptCount >= 4 || nextAttemptAt >= message.expiresAt;
      await input.repository.markOutboxFailed(message.id, nextAttemptAt, errorMessage(error), terminal);
    }
  }
  return { delivered, skippedExpired, failed };
}
