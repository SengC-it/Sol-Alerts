import { mean } from "./indicators.js";
import { clamp } from "./score.js";
import type { StrategyConfig, StrategyFamily, WeightMap, WeightVersion } from "./types.js";

export interface FamilyPerformance {
  family: StrategyFamily;
  meanR: number;
  trades: number;
  maxDrawdown: number;
}

export interface ChallengerEvaluation {
  pairedDifferenceLowerBound95: number;
  overallImprovement: number;
  recentDegradation: number;
  challengerDrawdown: number;
  concentrationShare: number;
  passes: boolean;
}

export function adaptSymbolWeights(
  globalBase: WeightMap,
  performanceByFamily: Map<StrategyFamily, FamilyPerformance>,
  config: StrategyConfig,
): WeightMap {
  const families: StrategyFamily[] = ["trend_breakout", "trend_pullback", "range_mean_reversion"];
  const observed = families.map((family) => performanceByFamily.get(family)?.meanR ?? 0);
  const baseline = mean(observed);
  const raw = families.map((family) => {
    const evidence = performanceByFamily.get(family);
    const sample = evidence?.trades ?? 0;
    const shrinkage = sample / (sample + config.weights.shrinkagePrior);
    const adjustment = clamp((evidence?.meanR ?? baseline) - baseline, -config.weights.symbolDeviationCap, config.weights.symbolDeviationCap) * shrinkage;
    return clamp(globalBase[family] + adjustment, 0.0001, 1);
  });
  const total = raw.reduce((sum, value) => sum + value, 0);
  return {
    trend_breakout: (raw[0] ?? 0) / total,
    trend_pullback: (raw[1] ?? 0) / total,
    range_mean_reversion: (raw[2] ?? 0) / total,
  };
}

export function bootstrapLowerBound(values: number[], seed: number, replicates = 2000): number {
  if (values.length === 0) return 0;
  let state = seed >>> 0;
  const next = (): number => {
    state = (Math.imul(1664525, state) + 1013904223) >>> 0;
    return state / 0x1_0000_0000;
  };
  const means: number[] = [];
  for (let replicate = 0; replicate < replicates; replicate += 1) {
    let total = 0;
    for (let index = 0; index < values.length; index += 1) {
      total += values[Math.floor(next() * values.length)] ?? 0;
    }
    means.push(total / values.length);
  }
  means.sort((left, right) => left - right);
  return means[Math.floor(means.length * 0.025)] ?? mean(values);
}

/** Deterministic two-sample lower bound; samples are not paired by array index. */
export function independentBlockLowerBound(current: number[], prior: number[], seed: number, replicates = 2000): number {
  if (current.length === 0 || prior.length === 0) return 0;
  let state = seed >>> 0;
  const next = (): number => {
    state = (Math.imul(1664525, state) + 1013904223) >>> 0;
    return state / 0x1_0000_0000;
  };
  const differences: number[] = [];
  for (let replicate = 0; replicate < replicates; replicate += 1) {
    let currentSum = 0;
    let priorSum = 0;
    for (let index = 0; index < current.length; index += 1) currentSum += current[Math.floor(next() * current.length)] ?? 0;
    for (let index = 0; index < prior.length; index += 1) priorSum += prior[Math.floor(next() * prior.length)] ?? 0;
    differences.push(currentSum / current.length - priorSum / prior.length);
  }
  differences.sort((left, right) => left - right);
  return differences[Math.floor(differences.length * 0.025)] ?? 0;
}

export function evaluateChallenger(input: {
  pairedBlockReturns: number[];
  championOverallMeanR: number;
  challengerOverallMeanR: number;
  championRecentMeanR: number;
  challengerRecentMeanR: number;
  challengerDrawdown: number;
  symbolContributions: number[];
  maxDrawdown: number;
  config: StrategyConfig;
}): ChallengerEvaluation {
  const differences = input.pairedBlockReturns;
  const pairedDifferenceLowerBound95 = bootstrapLowerBound(differences, input.config.research.randomSeed, Math.min(2000, input.config.research.bootstrapReplicates));
  const overallImprovement = input.challengerOverallMeanR - input.championOverallMeanR;
  const recentDegradation = input.championRecentMeanR - input.challengerRecentMeanR;
  const totalPositive = input.symbolContributions.filter((value) => value > 0).reduce((sum, value) => sum + value, 0);
  const largest = Math.max(...input.symbolContributions.map((value) => Math.max(0, value)), 0);
  const concentrationShare = totalPositive === 0 ? 0 : largest / totalPositive;
  const passes = (
    pairedDifferenceLowerBound95 > 0 &&
    overallImprovement > 0 &&
    recentDegradation <= 0.1 &&
    input.challengerDrawdown <= input.maxDrawdown &&
    input.challengerDrawdown <= configMaxDrawdown(input.config) &&
    concentrationShare < 0.5
  );
  return { pairedDifferenceLowerBound95, overallImprovement, recentDegradation, challengerDrawdown: input.challengerDrawdown, concentrationShare, passes };
}

function configMaxDrawdown(config: StrategyConfig): number {
  return config.research.maxPortfolioDrawdown;
}

export function weeklyWeightStep(previous: WeightMap, proposed: WeightMap, config: StrategyConfig): WeightMap {
  const cap = config.weights.weeklyChangeCap;
  const largestDelta = Math.max(
    Math.abs(proposed.trend_breakout - previous.trend_breakout),
    Math.abs(proposed.trend_pullback - previous.trend_pullback),
    Math.abs(proposed.range_mean_reversion - previous.range_mean_reversion),
  );
  const factor = largestDelta === 0 ? 0 : Math.min(1, cap / largestDelta);
  return {
    trend_breakout: previous.trend_breakout + (proposed.trend_breakout - previous.trend_breakout) * factor,
    trend_pullback: previous.trend_pullback + (proposed.trend_pullback - previous.trend_pullback) * factor,
    range_mean_reversion: previous.range_mean_reversion + (proposed.range_mean_reversion - previous.range_mean_reversion) * factor,
  };
}

export function shouldEmergencyRollback(liveDrawdown: number, versionOutOfSampleDrawdown: number, config: StrategyConfig): boolean {
  return liveDrawdown > versionOutOfSampleDrawdown * config.risk.emergencyDrawdownMultiple;
}

export function shouldStatisticalRollback(input: {
  closedLiveSignals: number;
  newVersionMeanR: number;
  priorVersionMeanR: number;
  lowerBoundDifference: number;
  config: StrategyConfig;
}): boolean {
  return input.closedLiveSignals >= input.config.risk.statisticalRollbackMinimumClosedSignals && input.newVersionMeanR < input.priorVersionMeanR && input.lowerBoundDifference < 0;
}

export function weightMode(weight: number, currentlyShadow: boolean, config: StrategyConfig): "shadow" | "active" {
  if (currentlyShadow) return weight > config.weights.recoveryAbove ? "active" : "shadow";
  return weight < config.weights.shadowBelow ? "shadow" : "active";
}

export function evaluateAllStrategiesHalt(
  evidence: FamilyPerformance[],
  consecutiveValidWeeklyEvaluations: number,
  config: StrategyConfig,
): { halted: boolean; canResume: boolean } {
  const allFail = evidence.length === 0 || evidence.every((item) => item.trades < config.research.minimumTestTradesPerFamily || item.meanR <= 0 || item.maxDrawdown > config.research.maxPortfolioDrawdown);
  const canResume = !allFail && consecutiveValidWeeklyEvaluations >= 2;
  return { halted: allFail && !canResume, canResume };
}

export function makeRollbackVersion(active: WeightVersion, rollbackId: string, now: number): WeightVersion {
  return {
    ...active,
    id: rollbackId,
    version: active.version + 1,
    status: "rolled_back",
    source: "automatic_rollback",
    rollbackOf: active.id,
    activatedAt: now,
  };
}
