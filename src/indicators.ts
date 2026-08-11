import type { Candle, MarketIndicators } from "./types.js";

export function mean(values: number[]): number {
  return values.length === 0 ? 0 : values.reduce((sum, value) => sum + value, 0) / values.length;
}

export function median(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? ((sorted[middle - 1] ?? 0) + (sorted[middle] ?? 0)) / 2 : sorted[middle] ?? 0;
}

export function percentile(values: number[], p: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const boundedP = Math.min(1, Math.max(0, p));
  const position = (sorted.length - 1) * boundedP;
  const lower = Math.floor(position);
  const upper = Math.ceil(position);
  const lowerValue = sorted[lower] ?? 0;
  const upperValue = sorted[upper] ?? lowerValue;
  return lowerValue + (upperValue - lowerValue) * (position - lower);
}

export function standardDeviation(values: number[]): number {
  if (values.length < 2) return 0;
  const average = mean(values);
  return Math.sqrt(mean(values.map((value) => (value - average) ** 2)));
}

export function correlation(left: number[], right: number[]): number {
  const length = Math.min(left.length, right.length);
  if (length < 2) return 0;
  const a = left.slice(-length);
  const b = right.slice(-length);
  const aMean = mean(a);
  const bMean = mean(b);
  const numerator = a.reduce((sum, value, index) => sum + (value - aMean) * ((b[index] ?? 0) - bMean), 0);
  const denominator = Math.sqrt(
    a.reduce((sum, value) => sum + (value - aMean) ** 2, 0) *
      b.reduce((sum, value) => sum + (value - bMean) ** 2, 0),
  );
  return denominator === 0 ? 0 : numerator / denominator;
}

export function emaSeries(values: number[], period: number): Array<number | null> {
  const output: Array<number | null> = Array.from({ length: values.length }, () => null);
  if (period <= 0 || values.length < period) return output;
  let previous = mean(values.slice(0, period));
  output[period - 1] = previous;
  const multiplier = 2 / (period + 1);
  for (let index = period; index < values.length; index += 1) {
    const value = values[index] ?? previous;
    previous = (value - previous) * multiplier + previous;
    output[index] = previous;
  }
  return output;
}

export function ema(values: number[], period: number): number {
  const series = emaSeries(values, period);
  return [...series].reverse().find((value): value is number => value !== null) ?? 0;
}

export function trueRanges(candles: Candle[]): number[] {
  return candles.map((candle, index) => {
    const previousClose = candles[index - 1]?.close ?? candle.open;
    return Math.max(candle.high - candle.low, Math.abs(candle.high - previousClose), Math.abs(candle.low - previousClose));
  });
}

export function atr(candles: Candle[], period: number): number {
  const ranges = trueRanges(candles);
  return mean(ranges.slice(-period));
}

export function adx(candles: Candle[], period: number): number {
  if (candles.length < period + 2) return 0;
  const ranges = trueRanges(candles);
  const plus: number[] = [];
  const minus: number[] = [];
  for (let index = 1; index < candles.length; index += 1) {
    const current = candles[index];
    const previous = candles[index - 1];
    if (!current || !previous) continue;
    const upMove = current.high - previous.high;
    const downMove = previous.low - current.low;
    plus.push(upMove > downMove && upMove > 0 ? upMove : 0);
    minus.push(downMove > upMove && downMove > 0 ? downMove : 0);
  }
  const recentRanges = ranges.slice(-period);
  const rangeAverage = mean(recentRanges);
  if (rangeAverage === 0) return 0;
  const plusDi = (mean(plus.slice(-period)) / rangeAverage) * 100;
  const minusDi = (mean(minus.slice(-period)) / rangeAverage) * 100;
  const denominator = plusDi + minusDi;
  return denominator === 0 ? 0 : (Math.abs(plusDi - minusDi) / denominator) * 100;
}

export function rsi(values: number[], period: number): number {
  if (values.length <= period) return 50;
  let gains = 0;
  let losses = 0;
  const start = values.length - period;
  for (let index = start; index < values.length; index += 1) {
    const change = (values[index] ?? 0) - (values[index - 1] ?? values[index] ?? 0);
    if (change >= 0) gains += change;
    else losses -= change;
  }
  if (losses === 0) return gains === 0 ? 50 : 100;
  const relativeStrength = gains / losses;
  return 100 - 100 / (1 + relativeStrength);
}

export function bollinger(values: number[], period: number, deviations: number): { middle: number; upper: number; lower: number } {
  const sample = values.slice(-period);
  const middle = mean(sample);
  const spread = standardDeviation(sample) * deviations;
  return { middle, upper: middle + spread, lower: middle - spread };
}

export function returnsFromCloses(closes: number[]): number[] {
  const output: number[] = [];
  for (let index = 1; index < closes.length; index += 1) {
    const previous = closes[index - 1] ?? 0;
    const current = closes[index] ?? previous;
    if (previous !== 0) output.push(current / previous - 1);
  }
  return output;
}

export function marketIndicators(candles: Candle[], config: {
  emaFast: number;
  emaSlow: number;
  adxPeriod: number;
  atrPeriod: number;
  rsiPeriod: number;
  bollingerPeriod: number;
}): MarketIndicators {
  const closes = candles.map((candle) => candle.close);
  const bands = bollinger(closes, config.bollingerPeriod, 2);
  return {
    emaFast: ema(closes, config.emaFast),
    emaSlow: ema(closes, config.emaSlow),
    adx: adx(candles, config.adxPeriod),
    atr: atr(candles, config.atrPeriod),
    rsi: rsi(closes, config.rsiPeriod),
    bollingerMiddle: bands.middle,
    bollingerUpper: bands.upper,
    bollingerLower: bands.lower,
  };
}

export function aggregate15mTo1h(candles: Candle[]): Candle[] {
  const groups = new Map<number, Candle[]>();
  for (const candle of candles) {
    const hour = Math.floor(candle.openTime / 3_600_000) * 3_600_000;
    const group = groups.get(hour) ?? [];
    group.push(candle);
    groups.set(hour, group);
  }
  return [...groups.entries()]
    .sort(([left], [right]) => left - right)
    .filter(([, group]) => group.length === 4)
    .map(([openTime, group]) => {
      const first = group[0];
      const last = group[group.length - 1];
      if (!first || !last) throw new Error("Invalid candle aggregation group");
      return {
        openTime,
        closeTime: last.closeTime,
        open: first.open,
        high: Math.max(...group.map((candle) => candle.high)),
        low: Math.min(...group.map((candle) => candle.low)),
        close: last.close,
        volume: group.reduce((sum, candle) => sum + candle.volume, 0),
        quoteVolume: group.reduce((sum, candle) => sum + candle.quoteVolume, 0),
        trades: group.reduce((sum, candle) => sum + candle.trades, 0),
      };
    });
}

export function validateClosedCandles(candles: Candle[], intervalMs: number, now: number, minimumBars: number): void {
  if (candles.length < minimumBars) throw new Error("Insufficient candle history");
  const last = candles[candles.length - 1];
  if (!last || now - last.closeTime > intervalMs * 2) throw new Error("Candle series is stale");
  for (let index = 0; index < candles.length; index += 1) {
    const candle = candles[index];
    if (!candle || !Number.isFinite(candle.close) || candle.close <= 0 || candle.closeTime >= now - 30_000) {
      throw new Error("Candle series contains an incomplete or future candle");
    }
    const next = candles[index + 1];
    if (next && next.openTime - candle.openTime !== intervalMs) {
      throw new Error("Candle series contains a gap or duplicate");
    }
  }
}
