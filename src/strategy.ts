import { atr, bollinger, ema, emaSeries, median, rsi } from "./indicators.js";
import { scoreCandidate } from "./score.js";
import type {
  Candle,
  Direction,
  HistoricalEvidence,
  MarketIndicators,
  MarketState,
  SignalCandidate,
  StrategyConfig,
  StrategyFamily,
  WeightMap,
} from "./types.js";

export function classifyMarketState(indicators: Pick<MarketIndicators, "emaFast" | "emaSlow" | "adx" | "atr">, trendThreshold: number, rangeThreshold: number): MarketState {
  if (indicators.atr <= 0) return "ambiguous";
  if (indicators.adx >= trendThreshold && indicators.emaFast > indicators.emaSlow) return "trend_up";
  if (indicators.adx >= trendThreshold && indicators.emaFast < indicators.emaSlow) return "trend_down";
  if (indicators.adx <= rangeThreshold) return "range";
  return "ambiguous";
}

export function emptyEvidence(): HistoricalEvidence {
  return {
    historyDays: 0,
    formalEligible: false,
    untouchedTestTrades: 0,
    untouchedMeanR: 0,
    untouchedMaxDrawdown: 1,
    meanRLowerBound95: 0,
    robustnessScore: 0,
  };
}

function supportsDirection(state: MarketState, direction: Direction, family: StrategyFamily): boolean {
  if (family === "range_mean_reversion") return state === "range";
  return direction === "long" ? state === "trend_up" : state === "trend_down";
}

function riskReward(
  direction: Direction,
  entry: number,
  stop: number,
  target: number,
  config: StrategyConfig,
): number {
  const grossReward = direction === "long" ? target - entry : entry - target;
  const grossRisk = direction === "long" ? entry - stop : stop - entry;
  const roundTripRate = config.execution.takerFeeRatePerSide + config.execution.baseAdverseSlippageRatePerSide;
  const costs = (entry + target) * roundTripRate;
  const netReward = grossReward - costs;
  const netRisk = grossRisk + costs;
  return netRisk <= 0 ? 0 : netReward / netRisk;
}

export function solveChaseBoundary(
  direction: Direction,
  entryLow: number,
  entryHigh: number,
  stop: number,
  target: number,
  minimumNetRewardRisk: number,
  config: StrategyConfig,
): number {
  const currentEntry = direction === "long" ? entryLow : entryHigh;
  if (riskReward(direction, currentEntry, stop, target, config) < minimumNetRewardRisk) return currentEntry;
  let low = direction === "long" ? entryLow : entryHigh;
  let high = direction === "long" ? entryHigh : entryLow;
  for (let iteration = 0; iteration < 40; iteration += 1) {
    const middle = (low + high) / 2;
    const valid = riskReward(direction, middle, stop, target, config) >= minimumNetRewardRisk;
    if (direction === "long") {
      if (valid) low = middle;
      else high = middle;
    } else if (valid) {
      high = middle;
    } else {
      low = middle;
    }
  }
  return direction === "long" ? low : high;
}

export function nextStructureTarget(direction: Direction, currentPrice: number, oneHour: Candle[]): number | null {
  // The target must be an observed, completed 1h structure. A volatility
  // multiple is intentionally not used as a synthetic fallback.
  const lookback = oneHour.slice(-21, -1);
  const levels = lookback.map((candle) => direction === "long" ? candle.high : candle.low);
  const candidates = direction === "long"
    ? levels.filter((level) => level > currentPrice).sort((left, right) => left - right)
    : levels.filter((level) => level < currentPrice).sort((left, right) => right - left);
  return candidates[0] ?? null;
}

function candidateFor(
  symbol: string,
  direction: Direction,
  family: StrategyFamily,
  parameterKey: string,
  triggerTime: number,
  currentPrice: number,
  markPrice: number,
  state: MarketState,
  fifteenMinute: Candle[],
  oneHour: Candle[],
  config: StrategyConfig,
  evidence: HistoricalEvidence,
  weight: number,
): SignalCandidate | null {
  const current = fifteenMinute[fifteenMinute.length - 1];
  if (!current) return null;
  const currentAtr = atr(fifteenMinute, 14);
  if (!Number.isFinite(currentAtr) || currentAtr <= 0) return null;
  const prior = fifteenMinute.slice(0, -1);
  const structureLow = Math.min(...prior.slice(-40).map((candle) => candle.low));
  const structureHigh = Math.max(...prior.slice(-40).map((candle) => candle.high));
  const technicalInvalidation = direction === "long" ? structureLow - currentAtr * 0.05 : structureHigh + currentAtr * 0.05;
  const referenceStop = direction === "long" ? technicalInvalidation - currentAtr * 0.25 : technicalInvalidation + currentAtr * 0.25;
  const referenceTarget = nextStructureTarget(direction, currentPrice, oneHour);
  if (referenceTarget === null) return null;
  const entryRange = currentAtr * 0.1;
  const entryLow = direction === "long" ? currentPrice : currentPrice - entryRange;
  const entryHigh = direction === "long" ? currentPrice + entryRange : currentPrice;
  const chaseBoundary = solveChaseBoundary(
    direction,
    entryLow,
    entryHigh,
    referenceStop,
    referenceTarget,
    config.execution.minimumNetRewardRisk,
    config,
  );
  const entryForScore = direction === "long" ? entryLow : entryHigh;
  const netRewardRisk = riskReward(direction, entryForScore, referenceStop, referenceTarget, config);
  if (netRewardRisk < config.execution.minimumNetRewardRisk) return null;
  const score = scoreCandidate({
    evidence,
    state,
    direction,
    netRewardRisk,
    spreadBps: 0,
    depthRatio: 1,
    triggerDistanceAtr: Math.abs(currentPrice - entryForScore) / currentAtr,
    markDeviationBps: Math.abs(markPrice / currentPrice - 1) * 10_000,
    config,
  });
  return {
    symbol,
    direction,
    family,
    parameterKey,
    triggerTime,
    currentPrice,
    markPrice,
    entryLow,
    entryHigh,
    chaseBoundary,
    technicalInvalidation,
    referenceStop,
    referenceTarget,
    netRewardRisk,
    state,
    score,
    evidence,
    confluence: 1,
    weight,
    expiresAt: triggerTime + config.execution.entryValidityMinutes * 60_000,
    shadowOnly: !evidence.formalEligible || weight < config.weights.shadowBelow,
    triggerAtr: currentAtr,
    triggerDistanceAtr: Math.abs(currentPrice - entryForScore) / currentAtr,
    confluenceFamilies: [family],
  };
}

function evaluateBreakout(
  symbol: string,
  direction: Direction,
  state: MarketState,
  candles: Candle[],
  oneHour: Candle[],
  config: StrategyConfig,
  evidence: HistoricalEvidence,
  weight: number,
  currentPrice: number,
  markPrice: number,
): SignalCandidate[] {
  if (!supportsDirection(state, direction, "trend_breakout")) return [];
  const output: SignalCandidate[] = [];
  const last = candles[candles.length - 1];
  if (!last) return output;
  const currentAtr = atr(candles, 14);
  if (!Number.isFinite(currentAtr) || currentAtr <= 0) return output;
  for (const lookback of config.strategies.trend_breakout.lookbacks) {
    const prior = candles.slice(-(lookback + 1), -1);
    if (prior.length !== lookback) continue;
    const medianVolume = median(prior.slice(-20).map((candle) => candle.volume));
    for (const buffer of config.strategies.trend_breakout.closeBufferAtr) {
      for (const volumeMultiplier of config.strategies.trend_breakout.volumeMultipliers) {
        const high = Math.max(...prior.map((candle) => candle.high));
        const low = Math.min(...prior.map((candle) => candle.low));
        const isBreakout = direction === "long"
          ? last.close > high + buffer * currentAtr && last.volume >= medianVolume * volumeMultiplier
          : last.close < low - buffer * currentAtr && last.volume >= medianVolume * volumeMultiplier;
        if (!isBreakout) continue;
        const parameterKey = `lookback=${lookback};buffer=${buffer};volume=${volumeMultiplier}`;
        const candidate = candidateFor(symbol, direction, "trend_breakout", parameterKey, last.closeTime, currentPrice, markPrice, state, candles, oneHour, config, evidence, weight);
        if (candidate) output.push(candidate);
      }
    }
  }
  return output;
}

function evaluatePullback(
  symbol: string,
  direction: Direction,
  state: MarketState,
  candles: Candle[],
  oneHour: Candle[],
  config: StrategyConfig,
  evidence: HistoricalEvidence,
  weight: number,
  currentPrice: number,
  markPrice: number,
): SignalCandidate[] {
  if (!supportsDirection(state, direction, "trend_pullback")) return [];
  const period = config.strategies.trend_pullback.emaPeriod;
  const series = emaSeries(candles.map((candle) => candle.close), period);
  const lastIndex = candles.length - 1;
  const last = candles[lastIndex];
  const previous = candles[lastIndex - 1];
  const emaValue = series[lastIndex] ?? 0;
  if (!last || !previous || emaValue === 0) return [];
  const currentAtr = atr(candles, 14);
  if (!Number.isFinite(currentAtr) || currentAtr <= 0) return [];
  const contraction = candles.slice(-(config.strategies.trend_pullback.contractionLookback + 1), -1);
  const averagePreviousRange = contraction.length === 0 ? 0 : median(contraction.map((candle) => candle.high - candle.low));
  const previousWasPullback = direction === "long"
    ? Math.abs(previous.low - emaValue) <= currentAtr * config.strategies.trend_pullback.maxDistanceAtr
    : Math.abs(previous.high - emaValue) <= currentAtr * config.strategies.trend_pullback.maxDistanceAtr;
  const expansionAndRecovery = direction === "long"
    ? last.close > previous.high && last.high - last.low > averagePreviousRange
    : last.close < previous.low && last.high - last.low > averagePreviousRange;
  if (!previousWasPullback || !expansionAndRecovery) return [];
  const candidate = candidateFor(symbol, direction, "trend_pullback", `ema=${period};structure=${config.strategies.trend_pullback.structureLookback}`, last.closeTime, currentPrice, markPrice, state, candles, oneHour, config, evidence, weight);
  return candidate ? [candidate] : [];
}

function evaluateMeanReversion(
  symbol: string,
  direction: Direction,
  state: MarketState,
  candles: Candle[],
  oneHour: Candle[],
  config: StrategyConfig,
  evidence: HistoricalEvidence,
  weight: number,
  currentPrice: number,
  markPrice: number,
): SignalCandidate[] {
  if (!supportsDirection(state, direction, "range_mean_reversion")) return [];
  const strategy = config.strategies.range_mean_reversion;
  const last = candles[candles.length - 1];
  const previous = candles[candles.length - 2];
  if (!last || !previous) return [];
  const closes = candles.map((candle) => candle.close);
  const output: SignalCandidate[] = [];
  for (const deviation of strategy.bollingerDeviations) {
    const bands = bollinger(closes.slice(0, -1), strategy.bollingerPeriod, deviation);
    for (const threshold of strategy.rsiThresholds) {
      const lowerRsi = threshold[0] ?? 30;
      const upperRsi = threshold[1] ?? 70;
      const currentRsi = rsi(closes, strategy.rsiPeriod);
      const isSignal = direction === "long"
        ? previous.low < bands.lower && last.close > bands.lower && currentRsi <= lowerRsi
        : previous.high > bands.upper && last.close < bands.upper && currentRsi >= upperRsi;
      if (!isSignal) continue;
      const parameterKey = `bb=${deviation};rsi=${lowerRsi}/${upperRsi}`;
      const candidate = candidateFor(symbol, direction, "range_mean_reversion", parameterKey, last.closeTime, currentPrice, markPrice, state, candles, oneHour, config, evidence, weight);
      if (candidate) output.push(candidate);
    }
  }
  return output;
}

export function evaluateStrategies(input: {
  symbol: string;
  direction: Direction;
  state: MarketState;
  fifteenMinute: Candle[];
  oneHour: Candle[];
  config: StrategyConfig;
  evidence: HistoricalEvidence;
  weights: WeightMap;
  currentPrice: number;
  markPrice: number;
  selectedParameters?: Partial<Record<StrategyFamily, Partial<Record<Direction, string>>>>;
}): SignalCandidate[] {
  const weight = input.weights[familyForState(input.state, input.direction, input.config) ?? "trend_breakout"];
  return [
    ...evaluateBreakout(input.symbol, input.direction, input.state, input.fifteenMinute, input.oneHour, input.config, input.evidence, input.weights.trend_breakout, input.currentPrice, input.markPrice),
    ...evaluatePullback(input.symbol, input.direction, input.state, input.fifteenMinute, input.oneHour, input.config, input.evidence, input.weights.trend_pullback, input.currentPrice, input.markPrice),
    ...evaluateMeanReversion(input.symbol, input.direction, input.state, input.fifteenMinute, input.oneHour, input.config, input.evidence, input.weights.range_mean_reversion, input.currentPrice, input.markPrice),
  ].filter((candidate) => input.selectedParameters?.[candidate.family]?.[input.direction] === undefined || input.selectedParameters[candidate.family]?.[input.direction] === candidate.parameterKey)
    .map((candidate) => ({ ...candidate, weight: candidate.weight || weight }));
}

function familyForState(state: MarketState, direction: Direction, config: StrategyConfig): StrategyFamily | null {
  if (state === "range") return "range_mean_reversion";
  if (direction === "long" && state === "trend_up") return config.weights.globalBase.trend_breakout >= config.weights.globalBase.trend_pullback ? "trend_breakout" : "trend_pullback";
  if (direction === "short" && state === "trend_down") return config.weights.globalBase.trend_breakout >= config.weights.globalBase.trend_pullback ? "trend_breakout" : "trend_pullback";
  return null;
}
