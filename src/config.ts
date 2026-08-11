import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { StrategyConfig } from "./types.js";

const SUPPORTED_SCHEMA_VERSION = 1;
const SUPPORTED_CONFIG_PREFIX = "1.0.";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredRecord(parent: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = parent[key];
  if (!isRecord(value)) {
    throw new Error(`Invalid strategy config: ${key} must be an object`);
  }
  return value;
}

function requiredString(parent: Record<string, unknown>, key: string): string {
  const value = parent[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`Invalid strategy config: ${key} must be a non-empty string`);
  }
  return value;
}

function requiredNumber(parent: Record<string, unknown>, key: string): number {
  const value = parent[key];
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`Invalid strategy config: ${key} must be a finite number`);
  }
  return value;
}

function requiredNumberArray(parent: Record<string, unknown>, key: string): number[] {
  const value = parent[key];
  if (!Array.isArray(value) || value.length === 0 || value.some((item) => typeof item !== "number" || !Number.isFinite(item))) {
    throw new Error(`Invalid strategy config: ${key} must be a non-empty number array`);
  }
  return value as number[];
}

function ensurePositiveNumbers(values: number[], label: string): void {
  if (values.some((value) => value <= 0)) {
    throw new Error(`Invalid strategy config: ${label} must be positive`);
  }
}

export function assertCompatibleConfig(value: unknown): StrategyConfig {
  if (!isRecord(value)) {
    throw new Error("Invalid strategy config: root must be an object");
  }
  const configVersion = requiredString(value, "configVersion");
  const schemaVersion = requiredNumber(value, "schemaVersion");
  requiredString(value, "publishedAt");
  const allowedKeys = new Set(["configVersion", "schemaVersion", "publishedAt", "universe", "marketState", "strategies", "execution", "weights", "score", "risk", "research"]);
  if (Object.keys(value).some((key) => !allowedKeys.has(key))) throw new Error("Invalid strategy config: unknown or partially written field");
  if (!configVersion.startsWith(SUPPORTED_CONFIG_PREFIX)) {
    throw new Error(`Unsupported strategy config version: ${configVersion}`);
  }
  if (schemaVersion !== SUPPORTED_SCHEMA_VERSION) {
    throw new Error(`Unsupported strategy schema version: ${schemaVersion}`);
  }

  const universe = requiredRecord(value, "universe");
  const marketState = requiredRecord(value, "marketState");
  const strategies = requiredRecord(value, "strategies");
  const execution = requiredRecord(value, "execution");
  const weights = requiredRecord(value, "weights");
  const score = requiredRecord(value, "score");
  const risk = requiredRecord(value, "risk");
  const research = requiredRecord(value, "research");

  if (requiredString(universe, "quoteAsset") !== "USDT" || requiredString(universe, "contractType") !== "PERPETUAL") {
    throw new Error("Only USDT perpetual contracts are supported");
  }
  if (requiredNumber(universe, "minQuoteVolume24h") !== 20_000_000) {
    throw new Error("Frozen v1 requires a 20,000,000 USDT 24h quote-volume gate");
  }
  ensurePositiveNumbers([
    requiredNumber(universe, "minFormalHistoryDays"),
    requiredNumber(universe, "shadowStartDays"),
    requiredNumber(universe, "minQuoteVolume24h"),
    requiredNumber(universe, "minDepthNotionalUsdt"),
    requiredNumber(universe, "referenceMaxOrderNotionalUsdt"),
  ], "universe");
  const maxSpreadBps = requiredNumber(universe, "maxSpreadBps");
  const depthBandPct = requiredNumber(universe, "depthBandPct");
  if (maxSpreadBps <= 0 || maxSpreadBps > 10 || depthBandPct <= 0 || depthBandPct > 0.01) {
    throw new Error("Invalid live liquidity gate bounds");
  }

  const marketNumbers = ["emaFast", "emaSlow", "adxPeriod", "atrPeriod"].map((key) => requiredNumber(marketState, key));
  ensurePositiveNumbers(marketNumbers, "marketState");
  const trendThresholds = requiredNumberArray(marketState, "trendThresholds");
  const rangeThresholds = requiredNumberArray(marketState, "rangeThresholds");
  if (trendThresholds.length !== 3 || rangeThresholds.length !== 2) {
    throw new Error("ADX grids must remain fixed at 3 trend and 2 range thresholds");
  }

  const breakout = requiredRecord(strategies, "trend_breakout");
  const pullback = requiredRecord(strategies, "trend_pullback");
  const meanReversion = requiredRecord(strategies, "range_mean_reversion");
  ensurePositiveNumbers(requiredNumberArray(breakout, "lookbacks"), "breakout.lookbacks");
  ensurePositiveNumbers(requiredNumberArray(breakout, "closeBufferAtr"), "breakout.closeBufferAtr");
  ensurePositiveNumbers(requiredNumberArray(breakout, "volumeMultipliers"), "breakout.volumeMultipliers");
  ensurePositiveNumbers([
    requiredNumber(pullback, "emaPeriod"),
    requiredNumber(pullback, "structureLookback"),
    requiredNumber(pullback, "maxDistanceAtr"),
    requiredNumber(pullback, "contractionLookback"),
  ], "pullback");
  ensurePositiveNumbers(requiredNumberArray(meanReversion, "bollingerDeviations"), "meanReversion.bollingerDeviations");
  ensurePositiveNumbers([
    requiredNumber(meanReversion, "bollingerPeriod"),
    requiredNumber(meanReversion, "rsiPeriod"),
  ], "meanReversion");
  const rsiThresholds = meanReversion.rsiThresholds;
  if (!Array.isArray(rsiThresholds) || rsiThresholds.length !== 2 || rsiThresholds.some((pair) => !Array.isArray(pair) || pair.length !== 2)) {
    throw new Error("Mean-reversion RSI grid must contain two [lower, upper] pairs");
  }

  const executionNumbers = [
    "takerFeeRatePerSide",
    "baseAdverseSlippageRatePerSide",
    "entryValidityMinutes",
    "maxHoldHours",
    "minimumNetRewardRisk",
    "riskPerSimulatedTrade",
    "maxOpenPositions",
    "maxLongPositions",
    "maxShortPositions",
    "maxSignalsPerEmail",
  ].map((key) => requiredNumber(execution, key));
  ensurePositiveNumbers(executionNumbers, "execution");
  if (requiredNumber(execution, "minimumNetRewardRisk") < 2 || requiredNumber(execution, "maxOpenPositions") > 6) {
    throw new Error("Execution safety minimums are not satisfied");
  }

  const globalBase = requiredRecord(weights, "globalBase");
  const weightValues = [
    requiredNumber(globalBase, "trend_breakout"),
    requiredNumber(globalBase, "trend_pullback"),
    requiredNumber(globalBase, "range_mean_reversion"),
  ];
  if (Math.abs(weightValues.reduce((sum, value) => sum + value, 0) - 1) > 1e-9) {
    throw new Error("Global strategy weights must sum to one");
  }
  ensurePositiveNumbers([
    requiredNumber(weights, "symbolDeviationCap"),
    requiredNumber(weights, "shrinkagePrior"),
    requiredNumber(weights, "shadowBelow"),
    requiredNumber(weights, "recoveryAbove"),
    requiredNumber(weights, "weeklyChangeCap"),
  ], "weights");

  const scoreNumbers = [
    "highPriorityMinimum",
    "ordinaryMinimum",
    "historicalRobustnessMax",
    "currentSetupMax",
    "marketStateMatchMax",
    "executionQualityMax",
  ].map((key) => requiredNumber(score, key));
  ensurePositiveNumbers(scoreNumbers, "score");
  if ((scoreNumbers.at(0) ?? 0) < 80 || (scoreNumbers.at(1) ?? 0) < 70 || scoreNumbers.slice(2).reduce((sum, value) => sum + value, 0) !== 100) {
    throw new Error("Score bounds must remain 80/70 and total 100");
  }

  const riskNumbers = [
    "correlationLookbackDays",
    "correlationThreshold",
    "maxClusterSignalsPerDirection",
    "extremeReturnPercentile",
    "extremePauseMinutes",
    "emergencyDrawdownMultiple",
    "statisticalRollbackMinimumClosedSignals",
    "statisticalRollbackAlpha",
  ].map((key) => requiredNumber(risk, key));
  ensurePositiveNumbers(riskNumbers, "risk");
  if (requiredNumber(risk, "correlationThreshold") < 0.8 || requiredNumber(risk, "extremeReturnPercentile") < 0.99) {
    throw new Error("Risk overlay bounds are too permissive");
  }

  const fractions = [
    requiredNumber(research, "trainFraction"),
    requiredNumber(research, "validationFraction"),
    requiredNumber(research, "testFraction"),
  ];
  if (fractions.some((value) => value <= 0) || Math.abs(fractions.reduce((sum, value) => sum + value, 0) - 1) > 1e-9) {
    throw new Error("Research split must be positive and sum to one");
  }
  ensurePositiveNumbers([
    requiredNumber(research, "adaptiveLongWindowMonths"),
    requiredNumber(research, "adaptiveRecentWindowDays"),
    requiredNumber(research, "minimumTestTradesPerSymbolStrategy"),
    requiredNumber(research, "minimumTestTradesPerFamily"),
    requiredNumber(research, "bootstrapReplicates"),
    requiredNumber(research, "randomSeed"),
    requiredNumber(research, "maxPortfolioDrawdown"),
  ], "research");
  if (requiredNumber(research, "maxPortfolioDrawdown") > 0.1) {
    throw new Error("Maximum portfolio drawdown cannot exceed 10%");
  }

  return value as unknown as StrategyConfig;
}

export function loadStrategyConfig(configPath = process.env.CONFIG_PATH ?? resolve(process.cwd(), "config/strategy-v1.json")): StrategyConfig {
  const raw = readFileSync(configPath, "utf8");
  return assertCompatibleConfig(JSON.parse(raw) as unknown);
}

export function requiredEnvironment(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required server environment variable: ${name}`);
  }
  return value;
}

export function runtimeEnvironment(): {
  supabaseUrl: string;
  supabaseServiceRoleKey: string;
  cronSecret: string;
  gmailUser: string;
  gmailAppPassword: string;
  recipient: string;
  binanceBaseUrl: string;
  outboxBatchSize: number;
} {
  return {
    supabaseUrl: requiredEnvironment("SUPABASE_URL").replace(/\/$/, ""),
    supabaseServiceRoleKey: requiredEnvironment("SUPABASE_SERVICE_ROLE_KEY"),
    cronSecret: requiredEnvironment("CRON_SECRET"),
    gmailUser: requiredEnvironment("GMAIL_SMTP_USER"),
    gmailAppPassword: requiredEnvironment("GMAIL_SMTP_APP_PASSWORD"),
    recipient: requiredEnvironment("ALERT_RECIPIENT"),
    binanceBaseUrl: (process.env.BINANCE_FAPI_BASE_URL ?? "https://fapi.binance.com").replace(/\/$/, ""),
    outboxBatchSize: Math.max(1, Number(process.env.OUTBOX_BATCH_SIZE ?? "10")),
  };
}
