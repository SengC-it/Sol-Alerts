import test from "node:test";
import assert from "node:assert/strict";
import { assertCompatibleConfig, loadStrategyConfig } from "../config.js";
import { clusterSymbols, combineSameDirectionCandidates, evaluateDirectionalDepthGate, evaluateUniverseGate, shouldPauseGlobalRisk, suppressConflictingDirections } from "../gates.js";
import { evaluateAllStrategiesHalt, weeklyWeightStep, weightMode } from "../lifecycle.js";
import { reconcileSimulation, reserveFormalSignals, simulationCounts } from "../live-simulation.js";
import { findExit } from "../portfolio.js";
import { scoreCandidate } from "../score.js";
import type { Candle, DepthSnapshot, FundingEvent, HistoricalEvidence, Instrument, SignalCandidate, SimulationState, Ticker24h } from "../types.js";

const config = loadStrategyConfig();

function evidence(overrides: Partial<HistoricalEvidence> = {}): HistoricalEvidence {
  return { historyDays: 365, formalEligible: true, untouchedTestTrades: 50, untouchedMeanR: 0.3, untouchedMaxDrawdown: 0.05, meanRLowerBound95: 0.1, robustnessScore: 20, ...overrides };
}

function candidate(overrides: Partial<SignalCandidate> = {}): SignalCandidate {
  const base: SignalCandidate = {
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
    score: scoreCandidate({ evidence: evidence(), state: "trend_up", direction: "long", family: "trend_breakout", netRewardRisk: 2.5, spreadBps: 2, depthRatio: 1, triggerDistanceAtr: 0, markDeviationBps: 0, config }),
    evidence: evidence(),
    confluence: 1,
    weight: 0.34,
    expiresAt: 1_700_000_000_000 + 1_800_000,
    shadowOnly: false,
    triggerAtr: 1,
    triggerDistanceAtr: 0,
    confluenceFamilies: ["trend_breakout"],
  };
  return { ...base, ...overrides };
}

test("score is bounded and decomposes to documented 100 points", () => {
  const score = scoreCandidate({ evidence: evidence(), state: "trend_up", direction: "long", family: "trend_breakout", netRewardRisk: 4, spreadBps: 0, depthRatio: 2, triggerDistanceAtr: 0, markDeviationBps: 0, config });
  assert.ok(score.total >= 0 && score.total <= 100);
  assert.equal(score.historicalRobustness <= 30, true);
  assert.equal(score.currentSetup <= 30, true);
  assert.equal(score.marketStateMatch <= 25, true);
  assert.equal(score.executionQuality <= 15, true);
});

test("universe gate uses contract metadata, not deliveryDate, and depth is directional", () => {
  const instrument: Instrument = { symbol: "BTCUSDT", baseAsset: "BTC", quoteAsset: "USDT", marginAsset: "USDT", contractType: "PERPETUAL", status: "TRADING", onboardDate: 0, deliveryDate: 4133404800000 };
  const ticker: Ticker24h = { symbol: "BTCUSDT", lastPrice: 100, bidPrice: 99.99, askPrice: 100.01, quoteVolume: 30_000_000 };
  const depth: DepthSnapshot = { symbol: "BTCUSDT", fetchedAt: 1_700_000_000_000, bids: [[99.99, 2_000]], asks: [[100.01, 2_000]] };
  assert.equal(evaluateUniverseGate(instrument, ticker, undefined, config, 1_700_000_000_001).eligible, true);
  assert.equal(evaluateDirectionalDepthGate(instrument, ticker, { ...depth, asks: [[100.01, 2_000_000]] }, "long", config, 1_700_000_000_001).eligible, true);
  assert.equal(evaluateDirectionalDepthGate(instrument, ticker, { ...depth, bids: [[99.99, 100]] }, "short", config, 1_700_000_000_001).eligible, false);
  assert.equal(evaluateUniverseGate({ ...instrument, quoteAsset: "USDC" }, ticker, depth, config, 1_700_000_000_001).eligible, false);
});

test("same-bar stop is evaluated before target", () => {
  const candles: Candle[] = [{ openTime: 1, closeTime: 2, open: 100, high: 120, low: 90, close: 105, volume: 1, quoteVolume: 100, trades: 1 }];
  const exit = findExit("long", candles, 0, 95, 115, 1);
  assert.deepEqual(exit, { index: 0, price: 95, reason: "stop" });
});

test("conflicting directions suppress and confluence is capped at five", () => {
  const many = Array.from({ length: 7 }, (_, index) => candidate({ parameterKey: `p${index}`, confluence: 1 }));
  const combined = combineSameDirectionCandidates(many);
  assert.equal(combined.length, 1);
  assert.equal(combined[0]?.confluence, 1);
  const familyVotes = combineSameDirectionCandidates([
    candidate(),
    candidate({ family: "trend_pullback", parameterKey: "pullback" }),
    candidate({ family: "range_mean_reversion", parameterKey: "mean" }),
  ]);
  assert.equal(familyVotes[0]?.confluence, 3);
  assert.deepEqual(new Set(familyVotes[0]?.confluenceFamilies), new Set(["trend_breakout", "trend_pullback", "range_mean_reversion"]));
  const conflict = suppressConflictingDirections([candidate(), candidate({ direction: "short", family: "trend_pullback" })]);
  assert.equal(conflict.kept.length, 0);
  assert.equal(conflict.suppressed, 2);
});

test("negative correlation does not form a positive risk cluster", () => {
  const clusters = clusterSymbols(["A", "B"], new Map([["A", [1, 2, 3]], ["B", [-1, -2, -3]]]), 0.8);
  assert.notEqual(clusters.get("A"), clusters.get("B"));
});

test("capacity ranks first and keeps separate directional counters", () => {
  const result = reserveFormalSignals([
    candidate({ symbol: "A", direction: "short", score: { ...candidate().score, total: 100 } }),
    candidate({ symbol: "B", direction: "long", score: { ...candidate().score, total: 99 } }),
    candidate({ symbol: "C", direction: "long", score: { ...candidate().score, total: 98 } }),
    candidate({ symbol: "D", direction: "long", score: { ...candidate().score, total: 97 } }),
  ], { total: 0, long: 0, short: 0 }, { ...config, execution: { ...config.execution, maxOpenPositions: 3, maxLongPositions: 2, maxShortPositions: 3 } });
  assert.deepEqual(result.accepted.map((item) => item.symbol), ["A", "B", "C"]);
  assert.equal(result.suppressed[0]?.symbol, "D");
});

test("live simulation has pending/open/closed lifecycle and stop-first gap semantics", () => {
  const signal = candidate({ triggerTime: 1_700_000_000_000, expiresAt: 1_700_001_800_000, referenceStop: 95, referenceTarget: 115, chaseBoundary: 101 });
  const initial: SimulationState = { equity: 100_000, peakEquity: 100_000, drawdown: 0, closedSignals: 0, positions: [], halt: false };
  const pending = reconcileSimulation({ state: initial, formalSignals: [signal], allSignals: [signal], bars: [], now: signal.triggerTime, config });
  assert.equal(pending.positions[0]?.status, "pending");
  const closed = reconcileSimulation({ state: pending, formalSignals: [signal], allSignals: [signal], bars: [{ symbol: "BTCUSDT", candles: [{ openTime: signal.triggerTime + 900_000, closeTime: signal.triggerTime + 1_800_000, open: 100, high: 120, low: 90, close: 105, volume: 1, quoteVolume: 100, trades: 1 }], fundingEvents: [] }], now: signal.triggerTime + 1_800_000, config });
  assert.equal(closed.positions[0]?.status, "closed");
  assert.equal(closed.positions[0]?.exitReason, "stop");
  assert.ok(closed.equity < initial.equity);
});

test("shadow simulation enters and closes without consuming formal account state", () => {
  const signal = candidate({ shadowOnly: true, expiresAt: 1_700_001_800_000 });
  const initial: SimulationState = { equity: 100_000, peakEquity: 100_000, drawdown: 0, closedSignals: 0, positions: [], halt: false };
  const pending = reconcileSimulation({ state: initial, formalSignals: [], allSignals: [signal], bars: [], now: signal.triggerTime, config });
  assert.equal(pending.positions[0]?.status, "pending");
  assert.equal(pending.positions[0]?.shadowOnly, true);
  const entryBar: Candle = { openTime: signal.triggerTime + 900_000, closeTime: signal.triggerTime + 1_800_000, open: 100, high: 102, low: 99, close: 101, volume: 1, quoteVolume: 100, trades: 1 };
  const opened = reconcileSimulation({ state: pending, formalSignals: [], allSignals: [signal], bars: [{ symbol: signal.symbol, candles: [entryBar], fundingEvents: [] }], now: entryBar.closeTime, config });
  assert.equal(opened.positions[0]?.status, "open");
  assert.equal(opened.equity, initial.equity);
  const exitBar: Candle = { openTime: entryBar.closeTime, closeTime: entryBar.closeTime + 900_000, open: 101, high: 116, low: 100, close: 114, volume: 1, quoteVolume: 100, trades: 1 };
  const closed = reconcileSimulation({ state: opened, formalSignals: [], allSignals: [signal], bars: [{ symbol: signal.symbol, candles: [exitBar], fundingEvents: [] }], now: exitBar.closeTime, config });
  assert.equal(closed.positions[0]?.status, "closed");
  assert.equal(closed.positions[0]?.shadowOnly, true);
  assert.notEqual(closed.positions[0]?.netR, null);
  assert.equal(closed.equity, initial.equity);
  assert.equal(closed.drawdown, 0);
  assert.equal(closed.closedSignals, 0);
  assert.deepEqual(simulationCounts(closed), { total: 0, long: 0, short: 0 });
  const replayed = reconcileSimulation({ state: closed, formalSignals: [], allSignals: [signal], bars: [{ symbol: signal.symbol, candles: [exitBar], fundingEvents: [] }], now: exitBar.closeTime, config });
  assert.deepEqual(replayed, closed);
});

test("funding events are direction-aware, cumulative, and idempotent", () => {
  const initial: SimulationState = { equity: 100_000, peakEquity: 100_000, drawdown: 0, closedSignals: 0, positions: [], halt: false };
  const entryBar: Candle = { openTime: 1_700_000_900_000, closeTime: 1_700_001_800_000, open: 100, high: 102, low: 99, close: 101, volume: 1, quoteVolume: 100, trades: 1 };
  const exitBar: Candle = { openTime: 1_700_003_600_000, closeTime: 1_700_004_500_000, open: 101, high: 116, low: 100, close: 114, volume: 1, quoteVolume: 100, trades: 1 };
  const funding: FundingEvent[] = [
    { symbol: "BTCUSDT", fundingTime: 1_700_002_100_000, fundingRate: 0.001, markPrice: 100 },
    { symbol: "BTCUSDT", fundingTime: 1_700_003_000_000, fundingRate: 0.001, markPrice: 100 },
  ];
  const runClosed = (signal: SignalCandidate, events: FundingEvent[]) => {
    const pending = reconcileSimulation({ state: initial, formalSignals: signal.shadowOnly ? [] : [signal], allSignals: [signal], bars: [], now: signal.triggerTime, config });
    return reconcileSimulation({ state: pending, formalSignals: signal.shadowOnly ? [] : [signal], allSignals: [signal], bars: [{ symbol: signal.symbol, candles: [entryBar, exitBar], fundingEvents: events }], now: exitBar.closeTime, config }).positions[0];
  };
  const long = candidate({ shadowOnly: true });
  const short = candidate({ shadowOnly: true, direction: "short", chaseBoundary: 99, technicalInvalidation: 105, referenceStop: 106, referenceTarget: 85 });
  const longNoFunding = runClosed(long, []);
  const longPositive = runClosed(long, funding);
  const longNegative = runClosed(long, funding.map((event) => ({ ...event, fundingRate: -event.fundingRate })));
  const shortPositive = runClosed(short, funding);
  const shortNegative = runClosed(short, funding.map((event) => ({ ...event, fundingRate: -event.fundingRate })));
  assert.ok((longPositive?.netR ?? 0) < (longNoFunding?.netR ?? 0));
  assert.ok((longNegative?.netR ?? 0) > (longNoFunding?.netR ?? 0));
  assert.ok((shortPositive?.netR ?? 0) > (shortNegative?.netR ?? 0));
  assert.equal(longPositive?.lastFundingTime, funding[1]?.fundingTime);
  assert.ok((longPositive?.accruedFundingR ?? 0) < 0);

  const pending = reconcileSimulation({ state: initial, formalSignals: [], allSignals: [long], bars: [], now: long.triggerTime, config });
  const opened = reconcileSimulation({ state: pending, formalSignals: [], allSignals: [long], bars: [{ symbol: long.symbol, candles: [entryBar], fundingEvents: [] }], now: entryBar.closeTime, config });
  const held = { openTime: entryBar.closeTime, closeTime: 1_700_003_600_000, open: 101, high: 103, low: 99, close: 101, volume: 1, quoteVolume: 100, trades: 1 } satisfies Candle;
  const funded = reconcileSimulation({ state: opened, formalSignals: [], allSignals: [long], bars: [{ symbol: long.symbol, candles: [held], fundingEvents: funding }], now: held.closeTime, config });
  const retry = reconcileSimulation({ state: funded, formalSignals: [], allSignals: [long], bars: [{ symbol: long.symbol, candles: [held], fundingEvents: funding }], now: held.closeTime, config });
  assert.equal(retry.positions[0]?.accruedFundingR, funded.positions[0]?.accruedFundingR);
  assert.equal(retry.positions[0]?.lastFundingTime, funded.positions[0]?.lastFundingTime);
});

test("global risk overlay pauses on extreme return and holds for the minimum pause", () => {
  const base = Array.from({ length: 100 }, () => 0.001);
  const pause = shouldPauseGlobalRisk({ btcReturns: [...base, 0.2], eligibleMarketReturns: base, now: 1000, pauseUntil: null, percentileLevel: 0.995 });
  assert.equal(pause, true);
  assert.equal(shouldPauseGlobalRisk({ btcReturns: base, eligibleMarketReturns: base, now: 1000, pauseUntil: 2000, percentileLevel: 0.995 }), true);
});

test("weekly weights cap each change and all-strategy failure halts", () => {
  const previous = config.weights.globalBase;
  const stepped = weeklyWeightStep(previous, { trend_breakout: 1, trend_pullback: 0, range_mean_reversion: 0 }, config);
  assert.ok(Math.abs(stepped.trend_breakout - previous.trend_breakout) <= 0.11);
  const halt = evaluateAllStrategiesHalt([
    { family: "trend_breakout", meanR: -0.1, trades: 1, maxDrawdown: 0.2 },
    { family: "trend_pullback", meanR: -0.1, trades: 1, maxDrawdown: 0.2 },
    { family: "range_mean_reversion", meanR: -0.1, trades: 1, maxDrawdown: 0.2 },
  ], 0, config);
  assert.equal(halt.halted, true);
  assert.equal(weightMode(0.2, true, config), "shadow");
  assert.equal(weightMode(0.26, true, config), "active");
});

test("incompatible config versions fail closed", () => {
  assert.throws(() => assertCompatibleConfig({ configVersion: "2.0.0", schemaVersion: 2, publishedAt: "2026-08-05T00:00:00.000Z" }), /Unsupported strategy config/);
});
