import { constantTimeEqual } from "../src/security.js";
import { makeServerDependencies } from "../src/server.js";
import { runScan, deliverDueOutbox } from "../src/runtime.js";

interface ScanRequest {
  method?: string;
  headers?: Record<string, string | string[] | undefined>;
}

interface ScanResponse {
  status(code: number): ScanResponse;
  json(body: unknown): void;
}

function header(request: ScanRequest, name: string): string {
  const value = request.headers?.[name] ?? request.headers?.[name.toLowerCase()];
  return Array.isArray(value) ? value[0] ?? "" : value ?? "";
}

export default async function handler(request: ScanRequest, response: ScanResponse): Promise<void> {
  if (request.method !== "POST") {
    response.status(405).json({ error: "method_not_allowed" });
    return;
  }
  try {
    const dependencies = makeServerDependencies();
    if (!constantTimeEqual(header(request, "x-cron-secret"), dependencies.environment.cronSecret)) {
      response.status(401).json({ error: "unauthorized" });
      return;
    }
    const result = await runScan({
      market: dependencies.market,
      repository: dependencies.repository,
      config: dependencies.config,
      recipient: dependencies.environment.recipient,
    });
    let delivery: { delivered: number; skippedExpired: number; failed: number } | null = null;
    try {
      delivery = await deliverDueOutbox({ repository: dependencies.repository, mailer: dependencies.mailer, limit: dependencies.environment.outboxBatchSize });
    } catch {
      delivery = null;
    }
    response.status(200).json({ ok: true, scanRunId: result.scanRunId, hasEmail: result.email !== null, diagnostics: result.diagnostics, delivery });
  } catch {
    response.status(503).json({ ok: false, error: "scan_failed_closed" });
  }
}
