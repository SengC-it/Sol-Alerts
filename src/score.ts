import type { Direction, HistoricalEvidence, MarketState, ScoreBreakdown, StrategyConfig, StrategyFamily } from "./types.js";

export function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

export function scoreHistoricalRobustness(evidence: HistoricalEvidence, maximum: number): number {
  const sampleCoverage = clamp(evidence.untouchedTestTrades / 100, 0, 1);
  const lowerBoundQuality = clamp((evidence.meanRLowerBound95 + 0.25) / 1.25, 0, 1);
  const drawdownQuality = clamp(1 - evidence.untouchedMaxDrawdown / 0.1, 0, 1);
  return maximum * (sampleCoverage * 0.35 + lowerBoundQuality * 0.45 + drawdownQuality * 0.2);
}

export function scoreCurrentSetup(netRewardRisk: number, triggerDistanceAtr: number, maximum: number): number {
  const rewardQuality = clamp((netRewardRisk - 2) / 3, 0, 1);
  const triggerQuality = clamp(1 - triggerDistanceAtr, 0, 1);
  return maximum * (rewardQuality * 0.7 + triggerQuality * 0.3);
}

export function scoreMarketStateMatch(state: MarketState, direction: Direction, family: StrategyFamily, maximum: number): number {
  const matches = family === "range_mean_reversion"
    ? state === "range"
    : (direction === "long" && state === "trend_up") || (direction === "short" && state === "trend_down");
  return matches ? maximum : state === "ambiguous" ? 0 : maximum * 0.25;
}

export function scoreExecutionQuality(spreadBps: number, depthRatio: number, markDeviationBps: number, maximum: number): number {
  const spreadQuality = clamp(1 - spreadBps / 10, 0, 1);
  const depthQuality = clamp(depthRatio, 0, 1);
  const markQuality = clamp(1 - markDeviationBps / 100, 0, 1);
  return maximum * (spreadQuality * 0.4 + depthQuality * 0.4 + markQuality * 0.2);
}

export function scoreCandidate(input: {
  evidence: HistoricalEvidence;
  state: MarketState;
  direction: Direction;
  family?: StrategyFamily;
  netRewardRisk: number;
  spreadBps: number;
  depthRatio: number;
  triggerDistanceAtr: number;
  markDeviationBps: number;
  config: StrategyConfig;
}): ScoreBreakdown {
  const family = input.family ?? (input.state === "range" ? "range_mean_reversion" : "trend_breakout");
  const historicalRobustness = scoreHistoricalRobustness(input.evidence, input.config.score.historicalRobustnessMax);
  const currentSetup = scoreCurrentSetup(input.netRewardRisk, input.triggerDistanceAtr, input.config.score.currentSetupMax);
  const marketStateMatch = scoreMarketStateMatch(input.state, input.direction, family, input.config.score.marketStateMatchMax);
  const executionQuality = scoreExecutionQuality(input.spreadBps, input.depthRatio, input.markDeviationBps, input.config.score.executionQualityMax);
  return {
    historicalRobustness,
    currentSetup,
    marketStateMatch,
    executionQuality,
    total: clamp(historicalRobustness + currentSetup + marketStateMatch + executionQuality, 0, 100),
  };
}

export function priorityForScore(score: number, config: StrategyConfig): "high" | "ordinary" | "shadow" {
  if (score >= config.score.highPriorityMinimum) return "high";
  if (score >= config.score.ordinaryMinimum) return "ordinary";
  return "shadow";
}
