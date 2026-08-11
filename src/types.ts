export type Direction = "long" | "short";
export type StrategyFamily =
  | "trend_breakout"
  | "trend_pullback"
  | "range_mean_reversion";
export type MarketState = "trend_up" | "trend_down" | "range" | "ambiguous";
export type SignalPriority = "high" | "ordinary" | "shadow";

export interface Candle {
  openTime: number;
  closeTime: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  quoteVolume: number;
  trades: number;
}

export interface Ticker24h {
  symbol: string;
  lastPrice: number;
  bidPrice: number;
  askPrice: number;
  quoteVolume: number;
}

export interface DepthSnapshot {
  symbol: string;
  fetchedAt: number;
  bids: Array<[price: number, quantity: number]>;
  asks: Array<[price: number, quantity: number]>;
}

export interface Instrument {
  symbol: string;
  baseAsset: string;
  quoteAsset: string;
  marginAsset: string;
  contractType: string;
  status: string;
  onboardDate: number;
  deliveryDate: number;
}

export interface UniverseGateResult {
  eligible: boolean;
  reason?: string;
  spreadBps: number;
  entryDepthNotionalUsdt: number;
  quoteVolume24h: number;
}

export interface MarketIndicators {
  emaFast: number;
  emaSlow: number;
  adx: number;
  atr: number;
  rsi: number;
  bollingerMiddle: number;
  bollingerUpper: number;
  bollingerLower: number;
}

export interface MarketSnapshot {
  state: MarketState;
  indicators: MarketIndicators;
  closedAt: number;
  returns15m: number[];
  returns1h: number[];
}

export interface StrategyParameterSet {
  key: string;
  values: Record<string, number>;
}

export interface HistoricalEvidence {
  historyDays: number;
  formalEligible: boolean;
  untouchedTestTrades: number;
  untouchedMeanR: number;
  untouchedMaxDrawdown: number;
  meanRLowerBound95: number;
  robustnessScore: number;
}

export interface ScoreBreakdown {
  historicalRobustness: number;
  currentSetup: number;
  marketStateMatch: number;
  executionQuality: number;
  total: number;
}

export interface SignalCandidate {
  symbol: string;
  direction: Direction;
  family: StrategyFamily;
  parameterKey: string;
  triggerTime: number;
  currentPrice: number;
  markPrice: number;
  entryLow: number;
  entryHigh: number;
  chaseBoundary: number;
  technicalInvalidation: number;
  referenceStop: number;
  referenceTarget: number;
  netRewardRisk: number;
  state: MarketState;
  score: ScoreBreakdown;
  evidence: HistoricalEvidence;
  confluence: number;
  weight: number;
  expiresAt: number;
  shadowOnly: boolean;
  clusterKey?: string;
  /** ATR and trigger distance measured from the closed trigger bar. */
  triggerAtr?: number;
  triggerDistanceAtr?: number;
  /** Families represented in the confluence vote; parameter variants are excluded. */
  confluenceFamilies?: StrategyFamily[];
}

export interface SignalState {
  key: string;
  symbol: string;
  family: StrategyFamily;
  direction: Direction;
  active: boolean;
  shadowOnly?: boolean;
  lastInvalidatedAt: number | null;
  cooldownUntil: number | null;
}

export interface WeightMap {
  trend_breakout: number;
  trend_pullback: number;
  range_mean_reversion: number;
}

export interface WeightVersion {
  id: string;
  version: number;
  configVersion: string;
  status: "champion" | "challenger" | "rolled_back";
  weights: WeightMap;
  /** Published sample-size-shrunk overrides, keyed by symbol. */
  symbolWeights?: Record<string, WeightMap>;
  /** Hysteresis mode persisted by weekly lifecycle evaluation. */
  modes?: Partial<Record<StrategyFamily, "active" | "shadow">>;
  /** Per-symbol effective hysteresis modes; overrides the global family mode. */
  symbolModes?: Record<string, Partial<Record<StrategyFamily, "active" | "shadow">>>;
  /** One fixed-grid key per family and direction selected by research. */
  selectedParameters?: Partial<Record<StrategyFamily, Partial<Record<Direction, string>>>>;
  reportId?: string | null;
  source: string;
  evidenceId: string | null;
  activatedAt: number | null;
  rollbackOf: string | null;
  outOfSampleMaxDrawdown: number;
}

export interface StrategyConfig {
  configVersion: string;
  schemaVersion: number;
  publishedAt: string;
  universe: {
    quoteAsset: string;
    contractType: string;
    minFormalHistoryDays: number;
    shadowStartDays: number;
    minQuoteVolume24h: number;
    maxSpreadBps: number;
    minDepthNotionalUsdt: number;
    depthBandPct: number;
    referenceMaxOrderNotionalUsdt: number;
  };
  marketState: {
    emaFast: number;
    emaSlow: number;
    adxPeriod: number;
    atrPeriod: number;
    trendThresholds: number[];
    rangeThresholds: number[];
  };
  strategies: {
    trend_breakout: {
      lookbacks: number[];
      closeBufferAtr: number[];
      volumeMultipliers: number[];
    };
    trend_pullback: {
      emaPeriod: number;
      structureLookback: number;
      maxDistanceAtr: number;
      contractionLookback: number;
    };
    range_mean_reversion: {
      bollingerPeriod: number;
      bollingerDeviations: number[];
      rsiPeriod: number;
      rsiThresholds: number[][];
    };
  };
  execution: {
    takerFeeRatePerSide: number;
    baseAdverseSlippageRatePerSide: number;
    entryValidityMinutes: number;
    maxHoldHours: number;
    minimumNetRewardRisk: number;
    riskPerSimulatedTrade: number;
    maxOpenPositions: number;
    maxLongPositions: number;
    maxShortPositions: number;
    maxSignalsPerEmail: number;
  };
  weights: {
    globalBase: WeightMap;
    symbolDeviationCap: number;
    shrinkagePrior: number;
    shadowBelow: number;
    recoveryAbove: number;
    weeklyChangeCap: number;
  };
  score: {
    highPriorityMinimum: number;
    ordinaryMinimum: number;
    historicalRobustnessMax: number;
    currentSetupMax: number;
    marketStateMatchMax: number;
    executionQualityMax: number;
  };
  risk: {
    correlationLookbackDays: number;
    correlationThreshold: number;
    maxClusterSignalsPerDirection: number;
    extremeReturnPercentile: number;
    extremePauseMinutes: number;
    emergencyDrawdownMultiple: number;
    statisticalRollbackMinimumClosedSignals: number;
    statisticalRollbackAlpha: number;
  };
  research: {
    trainFraction: number;
    validationFraction: number;
    testFraction: number;
    adaptiveLongWindowMonths: number;
    adaptiveRecentWindowDays: number;
    minimumTestTradesPerSymbolStrategy: number;
    minimumTestTradesPerFamily: number;
    bootstrapReplicates: number;
    randomSeed: number;
    maxPortfolioDrawdown: number;
  };
}

export interface CommittedScan {
  scanRunId: string;
  signals: SignalCandidate[];
  outboxId: string | null;
}

export type SimulationPositionStatus = "pending" | "open" | "closed" | "shadow" | "expired";

export interface SimulatedPosition {
  id: string;
  signalId: string | null;
  stateKey: string;
  symbol: string;
  direction: Direction;
  status: SimulationPositionStatus;
  shadowOnly: boolean;
  triggerTime: number;
  expiresAt: number;
  entryValidUntil: number;
  chaseBoundary: number;
  technicalInvalidation: number;
  referenceStop: number;
  referenceTarget: number;
  riskBudget: number;
  entryTime: number | null;
  entryPrice: number | null;
  exitTime: number | null;
  exitPrice: number | null;
  exitReason: "stop" | "target" | "time" | "invalidation" | "expired" | null;
  netR: number | null;
  feesR: number;
  /** Signed funding in R accumulated while this position was open. */
  accruedFundingR: number;
  /** Last funding event applied to this position; makes retries idempotent. */
  lastFundingTime: number | null;
  configVersion?: string | null;
  evidenceId?: string | null;
  weightVersionId?: string | null;
  lastEvaluatedCloseTime: number | null;
}

export interface SimulationState {
  equity: number;
  peakEquity: number;
  drawdown: number;
  closedSignals: number;
  positions: SimulatedPosition[];
  halt: boolean;
  /** Watermark of the last funding observation committed with a scan. */
  lastFundingObservationAt?: number | null;
  versionStats?: {
    currentNetRs: number[];
    priorNetRs: number[];
  };
}

export interface SimulationBarBatch {
  symbol: string;
  candles: Candle[];
  fundingEvents?: FundingEvent[];
  /** Retained for migration compatibility; new runtime code uses fundingEvents. */
  fundingRates?: Record<string, number>;
}

export interface FundingEvent {
  symbol: string;
  fundingTime: number;
  fundingRate: number;
  markPrice?: number;
}

export interface SimulationCommit {
  stateKeysObserved: string[];
  bars: SimulationBarBatch[];
  weightVersionId?: string | null;
  fundingObservationAt?: number | null;
  riskReference?: {
    btcReturns: number[];
    eligibleMarketReturns: number[];
    returnsBySymbol: Record<string, number[]>;
    baselineVersion: string;
    lastObservationAt: number | null;
    pauseUntil: number | null;
  };
}

export interface Repository {
  getActiveWeights(): Promise<WeightVersion | null>;
  getStrategyEvidence(symbols: string[]): Promise<Map<string, HistoricalEvidence>>;
  getSignalStates(keys: string[]): Promise<Map<string, SignalState>>;
  getSystemHalt(): Promise<boolean>;
  getLiveDrawdown(): Promise<number>;
  getOpenPositionCounts(): Promise<{ total: number; long: number; short: number }>;
  getRiskReference(): Promise<{ btcReturns: number[]; eligibleMarketReturns: number[]; returnsBySymbol?: Record<string, number[]>; baselineVersion?: string; lastObservationAt?: number | null; pauseUntil: number | null }>;
  setRiskReference(input: { btcReturns: number[]; eligibleMarketReturns: number[]; returnsBySymbol?: Record<string, number[]>; baselineVersion?: string; lastObservationAt?: number | null; pauseUntil: number | null }): Promise<void>;
  setRiskPauseUntil(until: number): Promise<void>;
  rollbackActiveWeight(reason: string): Promise<void>;
  getCurrentConfigVersion(): Promise<string | null>;
  commitScan(input: {
    startedAt: number;
    completedAt: number;
    configVersion: string;
    instruments: Instrument[];
    signals: SignalCandidate[];
    diagnostics: Record<string, unknown>;
    email: { recipient: string; subject: string; bodyText: string } | null;
    observedStateKeys?: string[];
    simulation?: SimulationCommit;
  }): Promise<CommittedScan>;
  recordHealthEvent(input: {
    kind: "scan_failure" | "scan_recovery" | "halt" | "rollback";
    detail: string;
    occurredAt: number;
  }): Promise<void>;
  claimDueOutbox(limit: number, now: number): Promise<OutboxMessage[]>;
  markOutboxDelivered(id: string, deliveredAt: number): Promise<void>;
  markOutboxFailed(id: string, nextAttemptAt: number, error: string, terminal?: boolean): Promise<void>;
  getSimulationState?(): Promise<SimulationState>;
}

export interface OutboxMessage {
  id: string;
  idempotencyKey: string;
  recipient: string;
  subject: string;
  bodyText: string;
  attemptCount: number;
  expiresAt: number;
}

export interface PublicMarketData {
  listInstruments(): Promise<Instrument[]>;
  get24hTickers(): Promise<Ticker24h[]>;
  getDepth(symbol: string): Promise<DepthSnapshot>;
  getCandles(symbol: string, interval: "15m" | "1h", limit: number): Promise<Candle[]>;
  getMarkPrice(symbol: string): Promise<number>;
  getMarkPrices?(symbols: string[]): Promise<Map<string, number>>;
  getFundingEvents?(symbols: string[], startTime: number, endTime: number): Promise<FundingEvent[]>;
}

export interface Mailer {
  send(message: {
    recipient: string;
    subject: string;
    bodyText: string;
    idempotencyKey: string;
  }): Promise<void>;
}

export interface ScanDiagnostics {
  eligibleInstruments: number;
  formalCandidates: number;
  shadowCandidates: number;
  suppressedByRiskOverlay: boolean;
  suppressedByConflict: number;
  suppressedByCapacity: number;
  failedSymbols: string[];
  halted?: boolean;
}
