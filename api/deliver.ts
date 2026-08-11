import { constantTimeEqual } from "../src/security.js";
import { makeServerDependencies } from "../src/server.js";
import { deliverDueOutbox } from "../src/runtime.js";

interface DeliveryRequest {
  method?: string;
  headers?: Record<string, string | string[] | undefined>;
}

interface DeliveryResponse {
  status(code: number): DeliveryResponse;
  json(body: unknown): void;
}

function header(request: DeliveryRequest, name: string): string {
  const value = request.headers?.[name] ?? request.headers?.[name.toLowerCase()];
  return Array.isArray(value) ? value[0] ?? "" : value ?? "";
}

export default async function handler(request: DeliveryRequest, response: DeliveryResponse): Promise<void> {
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
    const result = await deliverDueOutbox({ repository: dependencies.repository, mailer: dependencies.mailer, limit: dependencies.environment.outboxBatchSize });
    response.status(200).json({ ok: true, ...result });
  } catch {
    response.status(503).json({ ok: false, error: "delivery_failed_closed" });
  }
}
