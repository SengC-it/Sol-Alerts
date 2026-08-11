import { correlation, percentile } from "./indicators.js";
import { scoreCandidate } from "./score.js";
import type {
  DepthSnapshot,
  Direction,
  Instrument,
  SignalCandidate,
  StrategyConfig,
  Ticker24h,
  UniverseGateResult,
} from "./types.js";

export function calculateSpreadBps(ticker: Pick<Ticker24h, "bidPrice" | "askPrice">): number {
  if (ticker.bidPrice <= 0 || ticker.askPrice <= 0 || ticker.askPrice < ticker.bidPrice) return Number.POSITIVE_INFINITY;
  return ((ticker.askPrice - ticker.bidPrice) / ((ticker.askPrice + ticker.bidPrice) / 2)) * 10_000;
}

export function entrySideDepthNotional(depth: DepthSnapshot, direction: Direction, bandPct: number): number {
  const best = direction === "long" ? depth.asks[0]?.[0] : depth.bids[0]?.[0];
  if (!best || best <= 0) return 0;
  const levels = direction === "long" ? depth.asks : depth.bids;
  return levels.reduce((total, [price, quantity]) => {
    const withinBand = direction === "long" ? price <= best * (1 + bandPct) : price >= best * (1 - bandPct);
    return withinBand ? total + price * quantity : total;
  }, 0);
}

export function evaluateUniverseGate(
  instrument: Instrument,
  ticker: Ticker24h | undefined,
  _depth: DepthSnapshot | undefined,
  config: StrategyConfig,
  _now: number,
): UniverseGateResult {
  if (
    instrument.status !== "TRADING" ||
    instrument.contractType !== config.universe.contractType ||
    instrument.quoteAsset !== config.universe.quoteAsset ||
    instrument.marginAsset !== config.universe.quoteAsset
  ) {
    return { eligible: false, reason: "instrument_not_usdt_perpetual", spreadBps: Number.POSITIVE_INFINITY, entryDepthNotionalUsdt: 0, quoteVolume24h: 0 };
  }
  if (!ticker) return { eligible: false, reason: "missing_ticker", spreadBps: Number.POSITIVE_INFINITY, entryDepthNotionalUsdt: 0, quoteVolume24h: 0 };
  const spreadBps = calculateSpreadBps(ticker);
  if (!Number.isFinite(spreadBps) || spreadBps > config.universe.maxSpreadBps || ticker.quoteVolume < config.universe.minQuoteVolume24h) {
    return { eligible: false, reason: "volume_or_spread", spreadBps, entryDepthNotionalUsdt: 0, quoteVolume24h: ticker.quoteVolume };
  }
  // Depth is deliberately not a universe-wide gate. It is fetched only after a
  // directional raw candidate exists and evaluated on that candidate's entry side.
  return { eligible: true, spreadBps, entryDepthNotionalUsdt: 0, quoteVolume24h: ticker.quoteVolume };
}

export function evaluateDirectionalDepthGate(
  instrument: Instrument,
  ticker: Ticker24h | undefined,
  depth: DepthSnapshot | undefined,
  direction: Direction,
  config: StrategyConfig,
  now: number,
): UniverseGateResult {
  const staticGate = evaluateUniverseGate(instrument, ticker, undefined, config, now);
  if (!staticGate.eligible) return staticGate;
  if (!depth || depth.symbol !== instrument.symbol || now - depth.fetchedAt > 60_000) {
    return { ...staticGate, eligible: false, reason: "missing_or_stale_depth" };
  }
  const entryDepthNotional = entrySideDepthNotional(depth, direction, config.universe.depthBandPct);
  if (entryDepthNotional < config.universe.minDepthNotionalUsdt) {
    return { ...staticGate, eligible: false, reason: "directional_depth", entryDepthNotionalUsdt: entryDepthNotional };
  }
  return { ...staticGate, entryDepthNotionalUsdt: entryDepthNotional };
}

export function rescoreForExecution(candidate: SignalCandidate, spreadBps: number, depthRatio: number, config: StrategyConfig): SignalCandidate {
  const markDeviationBps = Math.abs(candidate.markPrice / candidate.currentPrice - 1) * 10_000;
  return {
    ...candidate,
    score: scoreCandidate({
      evidence: candidate.evidence,
      state: candidate.state,
      direction: candidate.direction,
      family: candidate.family,
      netRewardRisk: candidate.netRewardRisk,
      spreadBps,
      depthRatio,
      triggerDistanceAtr: candidate.triggerDistanceAtr ?? 0,
      markDeviationBps,
      config,
    }),
  };
}

export function isHardEligible(candidate: SignalCandidate, now: number, config: StrategyConfig): boolean {
  return (
    !candidate.shadowOnly &&
    candidate.evidence.formalEligible &&
    candidate.evidence.historyDays >= config.universe.minFormalHistoryDays &&
    candidate.evidence.untouchedTestTrades >= config.research.minimumTestTradesPerSymbolStrategy &&
    candidate.evidence.meanRLowerBound95 > 0 &&
    candidate.evidence.untouchedMaxDrawdown <= config.research.maxPortfolioDrawdown &&
    candidate.netRewardRisk >= config.execution.minimumNetRewardRisk &&
    candidate.expiresAt >= now &&
    (candidate.direction === "long" ? candidate.currentPrice <= candidate.chaseBoundary : candidate.currentPrice >= candidate.chaseBoundary)
  );
}

export function combineSameDirectionCandidates(candidates: SignalCandidate[]): SignalCandidate[] {
  const grouped = new Map<string, SignalCandidate[]>();
  for (const candidate of candidates) {
    const key = `${candidate.symbol}:${candidate.direction}`;
    const group = grouped.get(key) ?? [];
    group.push(candidate);
    grouped.set(key, group);
  }
  const output: SignalCandidate[] = [];
  for (const group of grouped.values()) {
    const byFamily = new Map<string, SignalCandidate>();
    for (const candidate of group) {
      const previous = byFamily.get(candidate.family);
      if (!previous || candidate.score.total > previous.score.total || (candidate.score.total === previous.score.total && candidate.parameterKey < previous.parameterKey)) {
        byFamily.set(candidate.family, candidate);
      }
    }
    const selected = [...byFamily.values()].sort((left, right) => right.score.total - left.score.total || left.family.localeCompare(right.family));
    const primary = selected[0];
    if (!primary) continue;
    const confluenceFamilies = selected.map((candidate) => candidate.family);
    const confluence = Math.min(5, confluenceFamilies.length);
    const totalWeight = selected.reduce((sum, candidate) => sum + Math.max(candidate.weight, 0.0001), 0);
    const weightedScore = (selector: (candidate: SignalCandidate) => number): number => selected.reduce((sum, candidate) => sum + selector(candidate) * Math.max(candidate.weight, 0.0001), 0) / totalWeight;
    output.push({
      ...primary,
      confluence,
      confluenceFamilies,
      // One validated parameter set per family supplies the coherent execution
      // plan. Only the vote score is combined; price/stop/target are not averaged.
      weight: totalWeight / selected.length,
      score: {
        historicalRobustness: weightedScore((candidate) => candidate.score.historicalRobustness),
        currentSetup: weightedScore((candidate) => candidate.score.currentSetup),
        marketStateMatch: weightedScore((candidate) => candidate.score.marketStateMatch),
        executionQuality: weightedScore((candidate) => candidate.score.executionQuality),
        total: Math.min(100, weightedScore((candidate) => candidate.score.total) + confluence),
      },
    });
  }
  return output;
}

export function suppressConflictingDirections(candidates: SignalCandidate[]): { kept: SignalCandidate[]; suppressed: number } {
  const bySymbol = new Map<string, Set<string>>();
  for (const candidate of candidates) {
    const directions = bySymbol.get(candidate.symbol) ?? new Set<string>();
    directions.add(candidate.direction);
    bySymbol.set(candidate.symbol, directions);
  }
  const kept = candidates.filter((candidate) => (bySymbol.get(candidate.symbol)?.size ?? 0) < 2);
  return { kept, suppressed: candidates.length - kept.length };
}

export function clusterSymbols(
  symbols: string[],
  returnsBySymbol: Map<string, number[]>,
  threshold: number,
): Map<string, string> {
  const parent = new Map<string, string>(symbols.map((symbol) => [symbol, symbol]));
  const find = (symbol: string): string => {
    const root = parent.get(symbol) ?? symbol;
    if (root === symbol) return root;
    const next = find(root);
    parent.set(symbol, next);
    return next;
  };
  const union = (left: string, right: string): void => {
    const leftRoot = find(left);
    const rightRoot = find(right);
    if (leftRoot !== rightRoot) parent.set(rightRoot, leftRoot < rightRoot ? leftRoot : rightRoot);
  };
  for (let leftIndex = 0; leftIndex < symbols.length; leftIndex += 1) {
    for (let rightIndex = leftIndex + 1; rightIndex < symbols.length; rightIndex += 1) {
      const left = symbols[leftIndex];
      const right = symbols[rightIndex];
      if (!left || !right) continue;
      if (correlation(returnsBySymbol.get(left) ?? [], returnsBySymbol.get(right) ?? []) >= threshold) union(left, right);
    }
  }
  return new Map(symbols.map((symbol) => [symbol, find(symbol)]));
}

export function limitClusterAlerts(candidates: SignalCandidate[], clusterBySymbol: Map<string, string>, perDirection: number): SignalCandidate[] {
  const counts = new Map<string, number>();
  return [...candidates]
    .sort((left, right) => right.score.total - left.score.total)
    .filter((candidate) => {
      const cluster = clusterBySymbol.get(candidate.symbol) ?? candidate.symbol;
      const key = `${cluster}:${candidate.direction}`;
      const current = counts.get(key) ?? 0;
      if (current >= perDirection) return false;
      counts.set(key, current + 1);
      return true;
    });
}

export function shouldPauseGlobalRisk(input: {
  btcReturns: number[];
  eligibleMarketReturns: number[];
  now: number;
  pauseUntil: number | null;
  percentileLevel: number;
}): boolean {
  if (input.pauseUntil !== null && input.now < input.pauseUntil) return true;
  const absoluteBtc = input.btcReturns.slice(0, -1).map(Math.abs);
  const currentBtc = Math.abs(input.btcReturns.at(-1) ?? 0);
  const absoluteMarket = input.eligibleMarketReturns.slice(0, -1).map(Math.abs);
  const currentMarket = Math.abs(input.eligibleMarketReturns.at(-1) ?? 0);
  const btcBreach = absoluteBtc.length > 20 && currentBtc > percentile(absoluteBtc, input.percentileLevel);
  const marketBreach = absoluteMarket.length > 20 && currentMarket > percentile(absoluteMarket, input.percentileLevel);
  return btcBreach || marketBreach;
}
