import { loadStrategyConfig } from "../src/config.js";

interface HealthRequest {
  method?: string;
}

interface HealthResponse {
  status(code: number): HealthResponse;
  json(body: unknown): void;
}

export default function handler(_request: HealthRequest, response: HealthResponse): void {
  try {
    const config = loadStrategyConfig();
    response.status(200).json({ ok: true, service: "sol-alerts", configVersion: config.configVersion, checkedAt: new Date().toISOString() });
  } catch {
    response.status(503).json({ ok: false, service: "sol-alerts" });
  }
}
