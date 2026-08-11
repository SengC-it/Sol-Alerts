import type { Candle, DepthSnapshot, FundingEvent, Instrument, PublicMarketData, Ticker24h } from "../types.js";

export class BinancePublicClient implements PublicMarketData {
  public constructor(private readonly baseUrl: string, private readonly timeoutMs = 12_000) {}

  private async getJson<T>(path: string, query: Record<string, string> = {}): Promise<T> {
    const url = new URL(path, `${this.baseUrl}/`);
    for (const [key, value] of Object.entries(query)) url.searchParams.set(key, value);
    const response = await fetch(url, { signal: AbortSignal.timeout(this.timeoutMs) });
    if (!response.ok) throw new Error(`Binance public endpoint ${path} returned HTTP ${response.status}`);
    return (await response.json()) as T;
  }

  async listInstruments(): Promise<Instrument[]> {
    const payload = await this.getJson<{ symbols: Array<Record<string, unknown>> }>("/fapi/v1/exchangeInfo");
    return payload.symbols.map((symbol) => ({
      symbol: String(symbol.symbol ?? ""),
      baseAsset: String(symbol.baseAsset ?? ""),
      quoteAsset: String(symbol.quoteAsset ?? ""),
      marginAsset: String(symbol.marginAsset ?? ""),
      contractType: String(symbol.contractType ?? ""),
      status: String(symbol.status ?? ""),
      onboardDate: Number(symbol.onboardDate ?? 0),
      deliveryDate: Number(symbol.deliveryDate ?? 0),
    }));
  }

  async get24hTickers(): Promise<Ticker24h[]> {
    const payload = await this.getJson<Array<Record<string, unknown>>>("/fapi/v1/ticker/24hr");
    return payload.map((ticker) => ({
      symbol: String(ticker.symbol ?? ""),
      lastPrice: Number(ticker.lastPrice ?? 0),
      bidPrice: Number(ticker.bidPrice ?? 0),
      askPrice: Number(ticker.askPrice ?? 0),
      quoteVolume: Number(ticker.quoteVolume ?? 0),
    }));
  }

  async getMarkPrices(symbols: string[]): Promise<Map<string, number>> {
    const payload = await this.getJson<Array<Record<string, unknown>>>('/fapi/v1/premiumIndex');
    const wanted = symbols.length === 0 ? null : new Set(symbols);
    return new Map(payload
      .filter((item) => wanted === null || wanted.has(String(item.symbol ?? '')))
      .map((item) => [String(item.symbol ?? ''), Number(item.markPrice ?? 0)]));
  }

  async getDepth(symbol: string): Promise<DepthSnapshot> {
    const payload = await this.getJson<{ bids: string[][]; asks: string[][] }>("/fapi/v1/depth", { symbol, limit: "1000" });
    return {
      symbol,
      fetchedAt: Date.now(),
      bids: payload.bids.map(([price, quantity]) => [Number(price), Number(quantity)]),
      asks: payload.asks.map(([price, quantity]) => [Number(price), Number(quantity)]),
    };
  }

  async getCandles(symbol: string, interval: "15m" | "1h", limit: number): Promise<Candle[]> {
    const intervalMs = interval === "15m" ? 15 * 60_000 : 60 * 60_000;
    const rows: unknown[][] = [];
    let startTime = Date.now() - limit * intervalMs;
    while (rows.length < limit) {
      const pageSize = Math.min(1500, limit - rows.length);
      const payload = await this.getJson<unknown[][]>("/fapi/v1/klines", { symbol, interval, limit: String(pageSize), startTime: String(startTime) });
      if (payload.length === 0) break;
      rows.push(...payload);
      const last = payload[payload.length - 1];
      if (!last || last.length < 7) throw new Error(`Malformed Binance kline for ${symbol}`);
      const nextStart = Number(last[6]) + 1;
      if (!Number.isFinite(nextStart) || nextStart <= startTime) throw new Error(`Binance kline pagination did not advance for ${symbol}`);
      startTime = nextStart;
      if (payload.length < pageSize) break;
    }
    return rows.slice(-limit).map((row) => {
      if (row.length < 9) throw new Error(`Malformed Binance kline for ${symbol}`);
      return {
        openTime: Number(row[0]), open: Number(row[1]), high: Number(row[2]), low: Number(row[3]), close: Number(row[4]),
        volume: Number(row[5]), closeTime: Number(row[6]), quoteVolume: Number(row[7]), trades: Number(row[8]),
      };
    });
  }

  async getMarkPrice(symbol: string): Promise<number> {
    const payload = await this.getJson<{ markPrice: string }>("/fapi/v1/premiumIndex", { symbol });
    return Number(payload.markPrice);
  }

  async getFundingEvents(symbols: string[], startTime: number, endTime: number): Promise<FundingEvent[]> {
    const maxWindowMs = 7 * 24 * 60 * 60_000;
    if (!Number.isInteger(startTime) || !Number.isInteger(endTime) || startTime < 0 || endTime <= startTime) {
      throw new Error("Binance funding window is invalid");
    }
    if (symbols.length === 0) return [];
    const wanted = new Set(symbols);
    const output: FundingEvent[] = [];
    const fetchRange = async (rangeStart: number, rangeEnd: number): Promise<Array<Record<string, unknown>>> => {
      const payload = await this.getJson<Array<Record<string, unknown>>>("/fapi/v1/fundingRate", {
        startTime: String(rangeStart),
        endTime: String(rangeEnd),
        limit: "1000",
      });
      if (!Array.isArray(payload) || payload.length > 1000) throw new Error("Malformed Binance funding response");
      let previousFundingTime = rangeStart - 1;
      for (const row of payload) {
        const fundingTime = Number(row.fundingTime);
        const fundingRate = Number(row.fundingRate);
        const markPrice = row.markPrice === undefined || row.markPrice === null ? undefined : Number(row.markPrice);
        if (!String(row.symbol ?? "") || !Number.isInteger(fundingTime) || fundingTime < rangeStart || fundingTime > rangeEnd || !Number.isFinite(fundingRate) || Math.abs(fundingRate) > 1 || (markPrice !== undefined && (!Number.isFinite(markPrice) || markPrice <= 0))) {
          throw new Error("Malformed Binance funding event");
        }
        if (fundingTime < previousFundingTime) throw new Error("Binance funding pagination is not ordered");
        previousFundingTime = fundingTime;
      }
      if (payload.length < 1000) return payload;
      if (rangeStart === rangeEnd) throw new Error("Binance funding range is full at one millisecond");
      const midpoint = rangeStart + Math.floor((rangeEnd - rangeStart) / 2);
      const [left, right] = await Promise.all([fetchRange(rangeStart, midpoint), fetchRange(midpoint + 1, rangeEnd)]);
      return [...left, ...right];
    };
    let windowStart = startTime;
    while (windowStart < endTime) {
      const windowEnd = Math.min(endTime, windowStart + maxWindowMs);
      // The all-market endpoint can return a full page ending halfway through a
      // funding timestamp shared by many symbols. Split a full inclusive time
      // range instead of advancing past that tied timestamp.
      const payload = await fetchRange(windowStart, windowEnd);
      for (const row of payload) {
        const symbol = String(row.symbol ?? "");
        const fundingTime = Number(row.fundingTime);
        const fundingRate = Number(row.fundingRate);
        const markPrice = row.markPrice === undefined || row.markPrice === null ? undefined : Number(row.markPrice);
        if (wanted.has(symbol)) output.push({ symbol, fundingTime, fundingRate, ...(markPrice === undefined ? {} : { markPrice }) });
      }
      if (windowEnd === endTime) break;
      windowStart = windowEnd + 1;
    }
    const unique = new Map<string, FundingEvent>();
    for (const event of output) unique.set(`${event.symbol}:${event.fundingTime}`, event);
    return [...unique.values()].sort((left, right) => left.fundingTime - right.fundingTime || left.symbol.localeCompare(right.symbol));
  }
}
