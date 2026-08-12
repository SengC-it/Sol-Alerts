import test from "node:test";
import assert from "node:assert/strict";
import { loadStrategyConfig } from "../config.js";
import { reconcileSimulation } from "../live-simulation.js";
import { deliverDueOutbox, renderAggregatedAlert, runScan } from "../runtime.js";
import type { CommittedScan, HistoricalEvidence, Instrument, Mailer, OutboxMessage, PublicMarketData, Repository, SignalCandidate, SignalState, SimulatedPosition, SimulationState, Ticker24h, WeightVersion } from "../types.js";

const config = loadStrategyConfig();

class FakeRepository implements Repository {
  public delivered: string[] = [];
  public failed: string[] = [];
  public health: string[] = [];
  public messages: OutboxMessage[] = [];
  public halted = false;
  public retryTimes: number[] = [];
  public terminalFailures: boolean[] = [];
  public failCommit = false;
  public commits: Array<{ observedStateKeys: string[] | undefined; signalCount: number }> = [];

  async getActiveWeights(): Promise<WeightVersion | null> { return null; }
  async getStrategyEvidence(_symbols: string[]): Promise<Map<string, HistoricalEvidence>> { return new Map(); }
  async getSignalStates(_keys: string[]): Promise<Map<string, SignalState>> { return new Map(); }
  async getSystemHalt(): Promise<boolean> { return this.halted; }
  async getLiveDrawdown(): Promise<number> { return 0; }
  async getOpenPositionCounts(): Promise<{ total: number; long: number; short: number }> { return { total: 0, long: 0, short: 0 }; }
  async getRiskReference(): Promise<{ btcReturns: number[]; eligibleMarketReturns: number[]; returnsBySymbol: Record<string, number[]>; baselineVersion: string; lastObservationAt: number | null; pauseUntil: number | null }> { return { btcReturns: Array.from({ length: 8640 }, () => 0.001), eligibleMarketReturns: Array.from({ length: 8640 }, () => 0.001), returnsBySymbol: { BTCUSDT: Array.from({ length: 8640 }, () => 0.001) }, baselineVersion: "risk-v1", lastObservationAt: null, pauseUntil: null }; }
  async setRiskReference(_input: { btcReturns: number[]; eligibleMarketReturns: number[]; returnsBySymbol?: Record<string, number[]>; baselineVersion?: string; lastObservationAt?: number | null; pauseUntil: number | null }): Promise<void> {}
  async setRiskPauseUntil(_until: number): Promise<void> {}
  async rollbackActiveWeight(_reason: string): Promise<void> {}
  async getCurrentConfigVersion(): Promise<string | null> { return config.configVersion; }
  async commitScan(input: { startedAt: number; completedAt: number; configVersion: string; instruments: Instrument[]; signals: SignalCandidate[]; diagnostics: Record<string, unknown>; email: { recipient: string; subject: string; bodyText: string } | null }): Promise<CommittedScan> {
    if (this.failCommit) throw new Error("durable commit failed");
    this.commits.push({ observedStateKeys: (input as typeof input & { observedStateKeys?: string[] }).observedStateKeys, signalCount: input.signals.length });
    return { scanRunId: "fake-scan", signals: input.signals, outboxId: input.email ? "fake-outbox" : null };
  }
  async recordHealthEvent(input: { kind: "scan_failure" | "scan_recovery" | "halt" | "rollback"; detail: string; occurredAt: number }): Promise<void> { this.health.push(input.kind); }
  async claimDueOutbox(_limit: number, _now: number): Promise<OutboxMessage[]> { return this.messages; }
  async markOutboxDelivered(id: string, _deliveredAt: number): Promise<void> { this.delivered.push(id); }
  async markOutboxFailed(id: string, nextAttemptAt: number, _error: string, terminal = false): Promise<void> { this.failed.push(id); this.retryTimes.push(nextAttemptAt); this.terminalFailures.push(terminal); }
}

class StatefulSignalRepository extends FakeRepository {
  public readonly states = new Map<string, SignalState>();
  public readonly persistedSignals: SignalCandidate[][] = [];
  public formalEvidence = false;

  override async getSignalStates(keys: string[]): Promise<Map<string, SignalState>> {
    return new Map(keys.map((key) => [key, this.states.get(key)] as const).filter((entry): entry is [string, SignalState] => entry[1] !== undefined));
  }

  override async getStrategyEvidence(symbols: string[]): Promise<Map<string, HistoricalEvidence>> {
    if (!this.formalEvidence) return new Map();
    const formal: HistoricalEvidence = { historyDays: 365, formalEligible: true, untouchedTestTrades: 100, untouchedMeanR: 0.5, untouchedMaxDrawdown: 0.01, meanRLowerBound95: 0.3, robustnessScore: 30 };
    return new Map(symbols.map((symbol) => [`${symbol}:trend_breakout`, formal]));
  }

  override async commitScan(input: Parameters<Repository["commitScan"]>[0]): Promise<CommittedScan> {
    const observed = new Set(input.observedStateKeys ?? []);
    for (const [key, state] of this.states) {
      if (state.active && !observed.has(key)) {
        this.states.set(key, { ...state, active: false, shadowOnly: false, lastInvalidatedAt: input.startedAt, cooldownUntil: input.startedAt + 60 * 60_000 });
      }
    }
    for (const signal of input.signals) {
      const key = `${signal.symbol}:${signal.direction}`;
      this.states.set(key, { key, symbol: signal.symbol, family: signal.family, direction: signal.direction, active: true, shadowOnly: signal.shadowOnly, lastInvalidatedAt: null, cooldownUntil: null });
    }
    this.persistedSignals.push(input.signals);
    return super.commitScan(input);
  }
}

class ActivePositionRepository extends FakeRepository {
  public readonly state: SimulationState;
  public lastSimulation: Parameters<Repository["commitScan"]>[0]["simulation"] | undefined;
  public reconciledState: SimulationState | undefined;

  public constructor(position: SimulatedPosition) {
    super();
    this.state = { equity: 100_000, peakEquity: 100_000, drawdown: 0, closedSignals: 0, positions: [position], halt: false };
  }

  async getSimulationState(): Promise<SimulationState> { return this.state; }

  override async commitScan(input: Parameters<Repository["commitScan"]>[0]): Promise<CommittedScan> {
    this.lastSimulation = input.simulation;
    if (input.simulation) {
      this.reconciledState = reconcileSimulation({ state: this.state, formalSignals: [], allSignals: input.signals, bars: input.simulation.bars, now: input.completedAt, config });
    }
    return super.commitScan(input);
  }
}

class NoopMarket implements PublicMarketData {
  public calls = 0;
  async listInstruments(): Promise<Instrument[]> { this.calls += 1; return [{ symbol: "BTCUSDT", baseAsset: "BTC", quoteAsset: "USDT", marginAsset: "USDT", contractType: "PERPETUAL", status: "TRADING", onboardDate: 0, deliveryDate: 4_133_404_800_000 }]; }
  async get24hTickers(): Promise<Ticker24h[]> { this.calls += 1; return [{ symbol: "BTCUSDT", lastPrice: 100, bidPrice: 99.99, askPrice: 100.01, quoteVolume: 30_000_000 }]; }
  async getDepth(_symbol: string): Promise<never> { throw new Error("not called while halted"); }
  async getCandles(_symbol: string, _interval: "15m" | "1h", _limit: number): Promise<import("../types.js").Candle[]> { this.calls += 1; return candlesForBudget(1_700_000_000_000); }
  async getMarkPrice(_symbol: string): Promise<number> { this.calls += 1; return 100; }
  async getMarkPrices(_symbols: string[]): Promise<Map<string, number>> { this.calls += 1; return new Map([["BTCUSDT", 100]]); }
}

test("halted scan suppresses formal email but continues validated shadow telemetry", async () => {
  const repository = new FakeRepository();
  repository.halted = true;
  const market = new NoopMarket();
  const result = await runScan({ repository, market, config, now: 1_700_000_000_000, recipient: "recipient@example.com" });
  assert.equal(result.email, null);
  assert.equal(result.scanRunId, "fake-scan");
  assert.ok(market.calls > 0);
  assert.equal(repository.commits.length, 1);
});

test("aggregated email is concise Chinese and carries risk fields and disclaimer", () => {
  const signal = {
    symbol: "BTCUSDT",
    direction: "long",
    family: "trend_breakout",
    parameterKey: "fixture",
    triggerTime: 1_700_000_000_000,
    currentPrice: 100,
    markPrice: 100,
    entryLow: 100,
    entryHigh: 101,
    chaseBoundary: 101,
    technicalInvalidation: 95,
    referenceStop: 94,
    referenceTarget: 115,
    netRewardRisk: 2.5,
    state: "trend_up",
    score: { historicalRobustness: 25, currentSetup: 25, marketStateMatch: 25, executionQuality: 15, total: 90 },
    evidence: { historyDays: 365, formalEligible: true, untouchedTestTrades: 50, untouchedMeanR: 0.3, untouchedMaxDrawdown: 0.05, meanRLowerBound95: 0.1, robustnessScore: 20 },
    confluence: 2,
    weight: 0.34,
    expiresAt: 1_700_001_800_000,
    shadowOnly: false,
  } satisfies SignalCandidate;
  const email = renderAggregatedAlert([signal], "scan");
  assert.equal(email.subject, "【Sol Alerts】发现1个交易机会");
  assert.match(email.bodyText, /BTCUSDT · 看涨 · 90分（重点）/);
  assert.match(email.bodyText, /建议关注区间：100\.00～101\.00/);
  assert.match(email.bodyText, /参考止损：94\.0000｜参考止盈：115\.00/);
  assert.match(email.bodyText, /系统不会自动下单/);
  assert.doesNotMatch(email.bodyText, /Technical|invalidation|liquidation|Untouched/);
  assert.equal(config.execution.maxSignalsPerEmail, 10);
});

test("expired outbox messages are never sent and failed sends follow retry path", async () => {
  const repository = new FakeRepository();
  repository.messages = [
    { id: "expired", idempotencyKey: "a", recipient: "x@example.com", subject: "x", bodyText: "x", attemptCount: 0, expiresAt: 900 },
    { id: "live", idempotencyKey: "b", recipient: "x@example.com", subject: "x", bodyText: "x", attemptCount: 1, expiresAt: 2_000 },
  ];
  const mailer: Mailer = { send: async (message): Promise<void> => { if (message.idempotencyKey === "b") throw new Error("temporary"); } };
  const result = await deliverDueOutbox({ repository, mailer, limit: 10, now: 1_000 });
  assert.equal(result.skippedExpired, 1);
  assert.equal(result.failed, 1);
  assert.deepEqual(repository.delivered, []);
  assert.deepEqual(repository.failed, ["expired", "live"]);
});

test("outbox retries are exactly 1, 3, and 5 minutes then terminal after the fourth send", async () => {
  const repository = new FakeRepository();
  const mailer: Mailer = { send: async (): Promise<void> => { throw new Error("temporary"); } };
  for (const attempt of [1, 2, 3, 4]) {
    repository.messages = [{ id: `m${attempt}`, idempotencyKey: String(attempt), recipient: "x@example.com", subject: "x", bodyText: "x", attemptCount: attempt, expiresAt: 10_000_000 }];
    await deliverDueOutbox({ repository, mailer, limit: 1, now: 1_000_000 });
  }
  assert.deepEqual(repository.retryTimes.slice(-4).map((value) => value - 1_000_000), [60_000, 180_000, 300_000, 300_000]);
  assert.deepEqual(repository.terminalFailures.slice(-4), [false, false, false, true]);
});

function candlesForBudget(now: number): import("../types.js").Candle[] {
  const interval = 15 * 60_000;
  return Array.from({ length: 300 }, (_, index) => {
    const openTime = now - (300 - index) * interval;
    return { openTime, closeTime: openTime + interval - 1, open: 100, high: 101, low: 99, close: 100, volume: 10, quoteVolume: 1_000, trades: 1 };
  });
}

test("530-symbol scan uses batch mark prices, bounded candles, and depth only for raw candidates", async () => {
  const now = 1_700_000_000_000;
  const repository = new FakeRepository();
  const instruments: Instrument[] = Array.from({ length: 530 }, (_, index) => ({ symbol: index === 0 ? "BTCUSDT" : `X${index}USDT`, baseAsset: "X", quoteAsset: "USDT", marginAsset: "USDT", contractType: "PERPETUAL", status: "TRADING", onboardDate: 0, deliveryDate: 4_133_404_800_000 }));
  const depthCalls: string[] = [];
  const candleSymbols: string[] = [];
  let endpointCalls = 0;
  let candleCalls = 0;
  let markBatchCalls = 0;
  let fundingCalls = 0;
  const market: PublicMarketData = {
    async listInstruments(): Promise<Instrument[]> { endpointCalls += 1; return instruments; },
    async get24hTickers(): Promise<Ticker24h[]> { endpointCalls += 1; return instruments.map((instrument, index) => ({ symbol: instrument.symbol, lastPrice: 100, bidPrice: 99.99, askPrice: 100.01, quoteVolume: 30_000_000 - index * 1_000 })); },
    async getMarkPrices(symbols: string[]): Promise<Map<string, number>> { endpointCalls += 1; markBatchCalls += 1; return new Map(symbols.map((symbol) => [symbol, 100])); },
    async getDepth(symbol: string): Promise<import("../types.js").DepthSnapshot> { endpointCalls += 1; depthCalls.push(symbol); return { symbol, fetchedAt: now, bids: [[99.99, 2_000]], asks: [[100.01, 2_000]] }; },
    async getCandles(symbol: string, _interval: "15m" | "1h", _limit: number): Promise<import("../types.js").Candle[]> { endpointCalls += 1; candleCalls += 1; candleSymbols.push(symbol); return candlesForBudget(now); },
    async getMarkPrice(): Promise<number> { throw new Error("per-symbol mark fallback is not allowed in budget fixture"); },
    async getFundingEvents(): Promise<import("../types.js").FundingEvent[]> { fundingCalls += 1; return []; },
  };
  const result = await runScan({
    market,
    repository,
    config,
    now,
    strategyEvaluator: (input) => input.symbol === "BTCUSDT" ? [{ ...({
      symbol: input.symbol, direction: input.direction, family: "trend_breakout", parameterKey: "fixture", triggerTime: now - 60_000, currentPrice: 100, markPrice: 100, entryLow: 100, entryHigh: 101, chaseBoundary: 101, technicalInvalidation: 95, referenceStop: 94, referenceTarget: 115, netRewardRisk: 2.5, state: "trend_up", score: { historicalRobustness: 0, currentSetup: 0, marketStateMatch: 0, executionQuality: 0, total: 0 }, evidence: { historyDays: 0, formalEligible: false, untouchedTestTrades: 0, untouchedMeanR: 0, untouchedMaxDrawdown: 1, meanRLowerBound95: 0, robustnessScore: 0 }, confluence: 1, weight: 0.34, expiresAt: now + 1_800_000, shadowOnly: false,
    } satisfies SignalCandidate), direction: input.direction }] : [],
  });
  assert.equal(markBatchCalls, 1);
  assert.equal(depthCalls.length, 1);
  assert.deepEqual(depthCalls, ["BTCUSDT"]);
  assert.equal(candleCalls, 530);
  assert.ok(candleSymbols.includes("X529USDT"));
  assert.equal(endpointCalls, 534);
  assert.equal(fundingCalls, 0);
  assert.equal(result.diagnostics.eligibleInstruments, 530);
});

test("shadow setup state is one activation, rearms after disappearance, and can later become formal", async () => {
  const repository = new StatefulSignalRepository();
  const instrument: Instrument = { symbol: "BTCUSDT", baseAsset: "BTC", quoteAsset: "USDT", marginAsset: "USDT", contractType: "PERPETUAL", status: "TRADING", onboardDate: 0, deliveryDate: 4_133_404_800_000 };
  const initialScanTime = Date.now();
  let scanNow = initialScanTime;
  let setupPresent = true;
  let activeFamily: SignalCandidate["family"] = "trend_breakout";
  const market: PublicMarketData = {
    async listInstruments(): Promise<Instrument[]> { return [instrument]; },
    async get24hTickers(): Promise<Ticker24h[]> { return [{ symbol: "BTCUSDT", lastPrice: 100, bidPrice: 99.99, askPrice: 100.01, quoteVolume: 30_000_000 }]; },
    async getMarkPrices(): Promise<Map<string, number>> { return new Map([["BTCUSDT", 100]]); },
    async getDepth(symbol: string): Promise<import("../types.js").DepthSnapshot> { return { symbol, fetchedAt: scanNow, bids: [[99.99, 2_000]], asks: [[100.01, 2_000]] }; },
    async getCandles(): Promise<import("../types.js").Candle[]> { return candlesForBudget(scanNow); },
    async getMarkPrice(): Promise<number> { return 100; },
  };
  const evaluate = (input: Parameters<typeof import("../strategy.js").evaluateStrategies>[0]): SignalCandidate[] => {
    if (!setupPresent || input.direction !== "long") return [];
    return [{
      symbol: input.symbol,
      direction: "long",
      family: activeFamily,
      parameterKey: `${activeFamily}-shadow-formal-fixture`,
      triggerTime: scanNow - 60_000,
      currentPrice: 100,
      markPrice: 100,
      entryLow: 100,
      entryHigh: 101,
      chaseBoundary: 101,
      technicalInvalidation: 95,
      referenceStop: 94,
      referenceTarget: 118,
      netRewardRisk: 4,
      state: "trend_up",
      score: { historicalRobustness: 0, currentSetup: 0, marketStateMatch: 0, executionQuality: 0, total: 0 },
      evidence: { historyDays: 0, formalEligible: false, untouchedTestTrades: 0, untouchedMeanR: 0, untouchedMaxDrawdown: 1, meanRLowerBound95: 0, robustnessScore: 0 },
      confluence: 1,
      weight: 0.34,
      expiresAt: scanNow + 1_800_000,
      shadowOnly: false,
    } satisfies SignalCandidate];
  };
  const run = async (at: number) => {
    scanNow = at;
    return runScan({ market, repository, config, now: at, strategyEvaluator: evaluate });
  };

  const first = await run(initialScanTime);
  assert.equal(first.signals.length, 1);
  assert.equal(first.signals[0]?.shadowOnly, true);
  assert.equal(repository.states.get("BTCUSDT:long")?.active, true);
  activeFamily = "trend_pullback";
  const second = await run(initialScanTime + 15 * 60_000);
  assert.equal(second.signals.length, 0, JSON.stringify({ states: [...repository.states.values()], commits: repository.commits }));
  assert.equal(repository.states.get("BTCUSDT:long")?.active, true);
  assert.equal(repository.persistedSignals.flat().length, 1);

  setupPresent = false;
  await run(initialScanTime + 30 * 60_000);
  assert.equal(repository.states.get("BTCUSDT:long")?.active, false);
  assert.equal(repository.states.get("BTCUSDT:long")?.shadowOnly, false);

  setupPresent = true;
  repository.formalEvidence = true;
  activeFamily = "trend_breakout";
  const tooSoon = await run(initialScanTime + 75 * 60_000);
  assert.equal(tooSoon.signals.length, 0);
  const fourth = await run(initialScanTime + 90 * 60_000);
  assert.equal(fourth.signals.length, 1);
  assert.equal(fourth.signals[0]?.shadowOnly, false);
  assert.equal(repository.persistedSignals.flat().length, 2);
});

test("opposing direction conflict is suppressed and observes neither activation", async () => {
  const repository = new FakeRepository();
  const now = 1_700_000_000_000;
  const instrument: Instrument = { symbol: "BTCUSDT", baseAsset: "BTC", quoteAsset: "USDT", marginAsset: "USDT", contractType: "PERPETUAL", status: "TRADING", onboardDate: 0, deliveryDate: 4_133_404_800_000 };
  const market: PublicMarketData = {
    async listInstruments(): Promise<Instrument[]> { return [instrument]; },
    async get24hTickers(): Promise<Ticker24h[]> { return [{ symbol: "BTCUSDT", lastPrice: 100, bidPrice: 99.99, askPrice: 100.01, quoteVolume: 30_000_000 }]; },
    async getMarkPrices(): Promise<Map<string, number>> { return new Map([["BTCUSDT", 100]]); },
    async getDepth(symbol: string): Promise<import("../types.js").DepthSnapshot> { return { symbol, fetchedAt: now, bids: [[99.99, 2_000]], asks: [[100.01, 2_000]] }; },
    async getCandles(): Promise<import("../types.js").Candle[]> { return candlesForBudget(now); },
    async getMarkPrice(): Promise<number> { return 100; },
  };
  const strategyEvaluator = (input: Parameters<typeof import("../strategy.js").evaluateStrategies>[0]): SignalCandidate[] => [{
    symbol: input.symbol,
    direction: input.direction,
    family: "trend_breakout",
    parameterKey: "conflict",
    triggerTime: now - 60_000,
    currentPrice: 100,
    markPrice: 100,
    entryLow: 100,
    entryHigh: 101,
    chaseBoundary: 101,
    technicalInvalidation: 95,
    referenceStop: 94,
    referenceTarget: 115,
    netRewardRisk: 2.5,
    state: input.direction === "long" ? "trend_up" : "trend_down",
    score: { historicalRobustness: 0, currentSetup: 0, marketStateMatch: 0, executionQuality: 0, total: 0 },
    evidence: { historyDays: 0, formalEligible: false, untouchedTestTrades: 0, untouchedMeanR: 0, untouchedMaxDrawdown: 1, meanRLowerBound95: 0, robustnessScore: 0 },
    confluence: 1,
    weight: 0.34,
    expiresAt: now + 1_800_000,
    shadowOnly: false,
  } satisfies SignalCandidate];
  const result = await runScan({ repository, market, config, now, strategyEvaluator });
  assert.equal(result.signals.length, 0);
  assert.deepEqual(repository.commits.at(-1)?.observedStateKeys, []);
});

test("active positions outside entry eligibility are reconciled without new candidates", async () => {
  const now = Date.now();
  const position: SimulatedPosition = {
    id: "position:ETHUSDT:trend_breakout:long:1",
    signalId: null,
    stateKey: "ETHUSDT:long",
    symbol: "ETHUSDT",
    direction: "long",
    status: "pending",
    shadowOnly: false,
    triggerTime: now - 2 * 60 * 60_000,
    expiresAt: now - 30 * 60_000,
    entryValidUntil: now - 30 * 60_000,
    chaseBoundary: 99,
    technicalInvalidation: 95,
    referenceStop: 94,
    referenceTarget: 118,
    riskBudget: 1_000,
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
    evidenceId: "ETHUSDT:trend_breakout",
    weightVersionId: null,
    lastEvaluatedCloseTime: null,
  };
  const repository = new ActivePositionRepository(position);
  const candleSymbols: string[] = [];
  let fundingSymbols: string[] = [];
  const instruments: Instrument[] = [
    { symbol: "BTCUSDT", baseAsset: "BTC", quoteAsset: "USDT", marginAsset: "USDT", contractType: "PERPETUAL", status: "TRADING", onboardDate: 0, deliveryDate: 4_133_404_800_000 },
    { symbol: "ETHUSDT", baseAsset: "ETH", quoteAsset: "USDT", marginAsset: "USDT", contractType: "PERPETUAL", status: "TRADING", onboardDate: 0, deliveryDate: 4_133_404_800_000 },
  ];
  const market: PublicMarketData = {
    async listInstruments(): Promise<Instrument[]> { return instruments; },
    async get24hTickers(): Promise<Ticker24h[]> { return [
      { symbol: "BTCUSDT", lastPrice: 100, bidPrice: 99.99, askPrice: 100.01, quoteVolume: 30_000_000 },
      { symbol: "ETHUSDT", lastPrice: 100, bidPrice: 99.99, askPrice: 100.01, quoteVolume: 1_000_000 },
    ]; },
    async getMarkPrices(symbols: string[]): Promise<Map<string, number>> { return new Map(symbols.map((symbol) => [symbol, 100])); },
    async getDepth(): Promise<never> { throw new Error("no entry candidate should request depth"); },
    async getCandles(symbol: string): Promise<import("../types.js").Candle[]> { candleSymbols.push(symbol); return candlesForBudget(now); },
    async getMarkPrice(): Promise<number> { return 100; },
    async getFundingEvents(symbols: string[]): Promise<import("../types.js").FundingEvent[]> { fundingSymbols = [...symbols]; return []; },
  };
  const result = await runScan({
    market,
    repository,
    config,
    now,
    strategyEvaluator: (input) => {
      assert.notEqual(input.symbol, "ETHUSDT");
      return [];
    },
  });
  assert.equal(result.signals.length, 0);
  assert.ok(candleSymbols.includes("ETHUSDT"));
  assert.ok(fundingSymbols.includes("ETHUSDT"));
  assert.ok(repository.lastSimulation?.bars.some((batch) => batch.symbol === "ETHUSDT"));
  assert.equal(repository.lastSimulation?.bars.find((batch) => batch.symbol === "ETHUSDT")?.symbol, "ETHUSDT");
  assert.equal(repository.reconciledState?.positions[0]?.status, "expired");
  assert.equal(repository.commits[0]?.signalCount, 0);
});

test("durable commit failure fails closed and records persisted health transition", async () => {
  const now = 1_700_000_000_000;
  const repository = new FakeRepository();
  repository.failCommit = true;
  const candles = candlesForBudget(now);
  const market: PublicMarketData = {
    async listInstruments(): Promise<Instrument[]> { return [{ symbol: "BTCUSDT", baseAsset: "BTC", quoteAsset: "USDT", marginAsset: "USDT", contractType: "PERPETUAL", status: "TRADING", onboardDate: 0, deliveryDate: 4_133_404_800_000 }]; },
    async get24hTickers(): Promise<Ticker24h[]> { return [{ symbol: "BTCUSDT", lastPrice: 100, bidPrice: 99.99, askPrice: 100.01, quoteVolume: 30_000_000 }]; },
    async getMarkPrices(): Promise<Map<string, number>> { return new Map([["BTCUSDT", 100]]); },
    async getDepth(): Promise<never> { throw new Error("depth should not be needed for a flat fixture"); },
    async getCandles(): Promise<import("../types.js").Candle[]> { return candles; },
    async getMarkPrice(): Promise<number> { return 100; },
  };
  await assert.rejects(runScan({ market, repository, config, now }), /Scan failed closed/);
  assert.deepEqual(repository.health, ["scan_failure"]);
});
