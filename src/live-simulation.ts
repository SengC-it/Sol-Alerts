import type { Candle, Direction, FundingEvent, SimulatedPosition, SimulationBarBatch, SimulationState, SignalCandidate, StrategyConfig } from "./types.js";

export interface CapacityReservation {
  accepted: SignalCandidate[];
  suppressed: SignalCandidate[];
  counts: { total: number; long: number; short: number };
}

/** Rank before reserving capacity; opposite-direction candidates never share an index counter. */
export function reserveFormalSignals(
  candidates: SignalCandidate[],
  existing: { total: number; long: number; short: number },
  config: StrategyConfig,
): CapacityReservation {
  const counts = { ...existing };
  const accepted: SignalCandidate[] = [];
  const suppressed: SignalCandidate[] = [];
  for (const candidate of [...candidates].sort((left, right) => right.score.total - left.score.total || left.symbol.localeCompare(right.symbol) || left.direction.localeCompare(right.direction))) {
    const directionCount = candidate.direction === "long" ? counts.long : counts.short;
    if (counts.total >= config.execution.maxOpenPositions || directionCount >= (candidate.direction === "long" ? config.execution.maxLongPositions : config.execution.maxShortPositions)) {
      suppressed.push(candidate);
      continue;
    }
    accepted.push(candidate);
    counts.total += 1;
    if (candidate.direction === "long") counts.long += 1;
    else counts.short += 1;
  }
  return { accepted, suppressed, counts };
}

function adverseEntry(direction: Direction, open: number, slippageRate: number): number {
  return direction === "long" ? open * (1 + slippageRate) : open * (1 - slippageRate);
}

function adverseExit(direction: Direction, price: number, slippageRate: number): number {
  return direction === "long" ? price * (1 - slippageRate) : price * (1 + slippageRate);
}

function fee(entry: number, exit: number, rate: number): number {
  return (entry + exit) * rate;
}

function stateKey(signal: SignalCandidate): string {
  return `${signal.symbol}:${signal.direction}`;
}

function positionFromSignal(signal: SignalCandidate, equity: number, shadowOnly: boolean, config: StrategyConfig, weightVersionId: string | null | undefined): SimulatedPosition {
  const key = stateKey(signal);
  return {
    id: `position:${key}:${signal.triggerTime}`,
    signalId: null,
    stateKey: key,
    symbol: signal.symbol,
    direction: signal.direction,
    // Shadow positions use the same pending/open/closed lifecycle.  The
    // shadowOnly flag, rather than a special status, keeps reconciliation and
    // persisted SQL on exactly one price path.
    status: "pending",
    shadowOnly,
    triggerTime: signal.triggerTime,
    expiresAt: signal.expiresAt,
    entryValidUntil: signal.expiresAt,
    chaseBoundary: signal.chaseBoundary,
    technicalInvalidation: signal.technicalInvalidation,
    referenceStop: signal.referenceStop,
    referenceTarget: signal.referenceTarget,
    riskBudget: equity * 0.01,
    entryTime: null,
    entryPrice: null,
    exitTime: null,
    exitPrice: null,
    exitReason: null,
    netR: null,
    feesR: 0,
    accruedFundingR: 0,
    lastFundingTime: null,
    configVersion: config.configVersion,
    evidenceId: `${signal.symbol}:${signal.family}`,
    weightVersionId: weightVersionId ?? null,
    lastEvaluatedCloseTime: null,
  };
}

function findPendingEntry(position: SimulatedPosition, candles: Candle[]): { candle: Candle; index: number } | null {
  for (let index = 0; index < candles.length; index += 1) {
    const candle = candles[index];
    if (!candle || candle.openTime <= position.triggerTime || candle.openTime > position.entryValidUntil) continue;
    const valid = position.direction === "long" ? candle.open <= position.chaseBoundary : candle.open >= position.chaseBoundary;
    if (valid) return { candle, index };
  }
  return null;
}

function fundingContributionR(position: SimulatedPosition, event: FundingEvent): number {
  const entry = position.entryPrice ?? 0;
  const riskDistance = position.direction === "long" ? entry - position.referenceStop : position.referenceStop - entry;
  const mark = event.markPrice ?? entry;
  if (!Number.isFinite(riskDistance) || riskDistance <= 0 || !Number.isFinite(mark) || mark <= 0 || !Number.isFinite(event.fundingRate)) return 0;
  // Binance's positive funding payment is paid by longs and received by
  // shorts.  Dividing the notional payment by the fixed risk budget gives R:
  // (riskBudget / riskDistance * mark * rate) / riskBudget.
  const directionSign = position.direction === "long" ? -1 : 1;
  return directionSign * event.fundingRate * mark / riskDistance;
}

function accrueFunding(position: SimulatedPosition, events: FundingEvent[], through: number): SimulatedPosition {
  if (position.entryTime === null || position.entryPrice === null) return position;
  let current = position;
  const lastApplied = current.lastFundingTime ?? Number.NEGATIVE_INFINITY;
  for (const event of events
    .filter((item) => item.symbol === position.symbol && item.fundingTime > (current.entryTime ?? 0) && item.fundingTime <= through && item.fundingTime > (current.lastFundingTime ?? lastApplied))
    .sort((left, right) => left.fundingTime - right.fundingTime)) {
    current = {
      ...current,
      accruedFundingR: current.accruedFundingR + fundingContributionR(current, event),
      lastFundingTime: event.fundingTime,
    };
  }
  return current;
}

function closePosition(position: SimulatedPosition, candle: Candle, price: number, reason: SimulatedPosition["exitReason"], config: StrategyConfig): SimulatedPosition {
  const entry = position.entryPrice ?? candle.open;
  const exit = adverseExit(position.direction, price, config.execution.baseAdverseSlippageRatePerSide);
  const risk = position.direction === "long" ? entry - position.referenceStop : position.referenceStop - entry;
  const pnl = position.direction === "long" ? exit - entry : entry - exit;
  const grossR = risk > 0 ? pnl / risk : 0;
  const costs = risk > 0 ? fee(entry, exit, config.execution.takerFeeRatePerSide) / risk : 0;
  return {
    ...position,
    status: "closed",
    exitTime: candle.closeTime,
    exitPrice: exit,
    exitReason: reason,
    netR: grossR - costs + position.accruedFundingR,
    feesR: costs,
    lastEvaluatedCloseTime: candle.closeTime,
  };
}

function reconcileOpenPosition(position: SimulatedPosition, batch: SimulationBarBatch, now: number, config: StrategyConfig): SimulatedPosition {
  const entryTime = position.entryTime ?? position.triggerTime;
  const holdDeadline = entryTime + config.execution.maxHoldHours * 60 * 60_000;
  // A closed bar whose close is after the hold deadline is not allowed to
  // extend the position. The last bar at or before the deadline is the time
  // exit, so a 48h position can never be closed on bar 193.
  const candles = batch.candles.filter((candle) => candle.closeTime > (position.lastEvaluatedCloseTime ?? entryTime) && candle.closeTime <= now && candle.closeTime <= holdDeadline);
  const fundingEvents = batch.fundingEvents ?? [];
  let current = position;
  for (const candle of candles) {
    current = accrueFunding(current, fundingEvents, candle.closeTime);
    const stopHit = current.direction === "long" ? candle.low <= current.referenceStop : candle.high >= current.referenceStop;
    const targetHit = current.direction === "long" ? candle.high >= current.referenceTarget : candle.low <= current.referenceTarget;
    // Stop is intentionally checked first when both levels occur in one bar.
    if (stopHit) {
      const gapPrice = current.direction === "long" ? Math.min(candle.open, current.referenceStop) : Math.max(candle.open, current.referenceStop);
      return closePosition(current, candle, gapPrice, "stop", config);
    }
    if (targetHit) return closePosition(current, candle, current.referenceTarget, "target", config);
    const invalidated = current.direction === "long" ? candle.close <= current.technicalInvalidation : candle.close >= current.technicalInvalidation;
    if (invalidated) return closePosition(current, candle, candle.close, "invalidation", config);
    current = { ...current, lastEvaluatedCloseTime: candle.closeTime };
  }
  current = accrueFunding(current, fundingEvents, now);
  if (current.status === "open" && now >= holdDeadline) {
    const last = candles.at(-1);
    if (last) return closePosition(current, last, last.close, "time", config);
  }
  return current;
}

export interface SimulationReconciliationInput {
  state: SimulationState;
  formalSignals: SignalCandidate[];
  allSignals: SignalCandidate[];
  bars: SimulationBarBatch[];
  now: number;
  config: StrategyConfig;
  weightVersionId?: string | null;
}

/** Pure state transition used by the scan transaction and deterministic tests. */
export function reconcileSimulation(input: SimulationReconciliationInput): SimulationState {
  const batches = new Map(input.bars.map((batch) => [batch.symbol, batch]));
  let equity = input.state.equity;
  let peakEquity = Math.max(input.state.peakEquity, equity);
  let closedSignals = input.state.closedSignals;
  const positions: SimulatedPosition[] = input.state.positions.map((position): SimulatedPosition => {
    const batch = batches.get(position.symbol);
    if (position.status === "pending" || position.status === "open" || position.status === "shadow") {
      if (!batch) throw new Error(`simulation batch missing for ${position.symbol}`);
      if (batch.fundingEvents === undefined) throw new Error(`funding events missing for ${position.symbol}`);
    }
    // Older rows used status=shadow. Treat them as pending once, then keep the
    // common lifecycle so a migration cannot strand shadow evidence forever.
    const normalized = {
      ...position,
      ...(position.status === "shadow" ? { status: "pending" as const } : {}),
      accruedFundingR: Number.isFinite(position.accruedFundingR) ? position.accruedFundingR : 0,
      feesR: Number.isFinite(position.feesR) ? position.feesR : 0,
      lastFundingTime: Number.isFinite(position.lastFundingTime) ? position.lastFundingTime : null,
    };
    if (normalized.status === "pending") {
      const entry = batch ? findPendingEntry(position, batch.candles) : null;
      if (entry && batch) {
        const opened = { ...normalized, status: "open" as const, entryTime: entry.candle.openTime, entryPrice: adverseEntry(normalized.direction, entry.candle.open, input.config.execution.baseAdverseSlippageRatePerSide), lastEvaluatedCloseTime: entry.candle.openTime - 1 };
        const reconciled = reconcileOpenPosition(opened, batch, input.now, input.config);
        if (!reconciled.shadowOnly && reconciled.status === "closed" && reconciled.netR !== null) {
          equity += reconciled.riskBudget * reconciled.netR;
          peakEquity = Math.max(peakEquity, equity);
          closedSignals += 1;
        }
        return reconciled;
      }
      if (input.now > normalized.entryValidUntil) return { ...normalized, status: "expired", exitTime: input.now, exitReason: "expired" };
      return normalized;
    }
    if (normalized.status !== "open" || !batch) return normalized;
    const reconciled = reconcileOpenPosition(normalized, batch, input.now, input.config);
    if (!reconciled.shadowOnly && reconciled.status === "closed" && reconciled.netR !== null) {
      equity += reconciled.riskBudget * reconciled.netR;
      peakEquity = Math.max(peakEquity, equity);
      closedSignals += 1;
    }
    return reconciled;
  });
  const existingKeys = new Set(input.state.positions.map((position) => `${position.stateKey}:${position.triggerTime}`));
  const formalSet = new Set(input.formalSignals.map((signal) => `${stateKey(signal)}:${signal.triggerTime}`));
  for (const signal of input.allSignals) {
    const key = `${stateKey(signal)}:${signal.triggerTime}`;
    if (existingKeys.has(key)) continue;
    positions.push(positionFromSignal(signal, equity, signal.shadowOnly || !formalSet.has(key), input.config, input.weightVersionId));
    existingKeys.add(key);
  }
  const drawdown = peakEquity > 0 ? (peakEquity - equity) / peakEquity : 0;
  return { ...input.state, equity, peakEquity, drawdown, closedSignals, positions, halt: input.state.halt };
}

export function simulationCounts(state: SimulationState): { total: number; long: number; short: number } {
  const active = state.positions.filter((position) => !position.shadowOnly && (position.status === "pending" || position.status === "open"));
  return {
    total: active.length,
    long: active.filter((position) => position.direction === "long").length,
    short: active.filter((position) => position.direction === "short").length,
  };
}
