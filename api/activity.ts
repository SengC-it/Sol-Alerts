import { makeServerDependencies } from "../src/server.js";

interface ActivityResponse {
  status(code: number): ActivityResponse;
  json(body: unknown): void;
  setHeader?(name: string, value: string): void;
}

export default async function handler(_request: unknown, response: ActivityResponse): Promise<void> {
  try {
    const { repository } = makeServerDependencies();
    const activity = await repository.getPublicActivity(10);
    response.setHeader?.("Cache-Control", "public, s-maxage=60, stale-while-revalidate=300");
    response.status(200).json({ ok: true, ...activity, checkedAt: new Date().toISOString() });
  } catch {
    response.status(503).json({ ok: false, emails: [], settlements: [] });
  }
}
