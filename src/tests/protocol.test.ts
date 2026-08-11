import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { BinancePublicClient } from "../adapters/binance.js";
import { GmailSmtpMailer, type SmtpTransport } from "../adapters/smtp.js";
import { SupabaseRestRepository } from "../adapters/supabase.js";

class FakeTransport implements SmtpTransport {
  private readonly listeners = new Map<string, Set<(...args: any[]) => void>>();
  public readonly writes: string[] = [];

  public constructor(private readonly timeoutOnGreeting = false) {
    if (timeoutOnGreeting) setTimeout(() => this.emit("timeout"), 0);
    else setTimeout(() => this.emit("data", Buffer.from("220 smtp.example ESMTP\r\n")), 0);
  }

  once(event: "data" | "error" | "timeout", listener: (...args: any[]) => void): this {
    const listeners = this.listeners.get(event) ?? new Set();
    listeners.add(listener);
    this.listeners.set(event, listeners);
    return this;
  }

  off(event: "data" | "error" | "timeout", listener: (...args: any[]) => void): this {
    this.listeners.get(event)?.delete(listener);
    return this;
  }

  write(data: string): boolean {
    this.writes.push(data);
    const command = data.trim();
    let response = "250 ok\r\n";
    if (command.startsWith("EHLO")) response = "250-smtp.example\r\n250-AUTH LOGIN\r\n250 ready\r\n";
    else if (command === "AUTH LOGIN") response = "334 VXNlcm5hbWU=\r\n";
    else if (command === Buffer.from("user@example.com").toString("base64")) response = "334 UGFzc3dvcmQ=\r\n";
    else if (command === Buffer.from("app-password").toString("base64")) response = "235 authenticated\r\n";
    else if (command === "DATA") response = "354 end with dot\r\n";
    else if (command === "QUIT") response = "221 bye\r\n";
    setTimeout(() => this.emit("data", Buffer.from(response)), 0);
    return true;
  }

  end(): void {}

  private emit(event: "data" | "error" | "timeout", ...args: any[]): void {
    const listeners = [...(this.listeners.get(event) ?? [])];
    for (const listener of listeners) listener(...args);
  }
}

test("SMTP adapter accepts multiline replies and completes the Gmail protocol", async () => {
  let transport: FakeTransport | undefined;
  const mailer = new GmailSmtpMailer("user@example.com", "app-password", async () => {
    transport = new FakeTransport();
    return transport;
  }, 100);
  await mailer.send({ recipient: "recipient@example.com", subject: "test", bodyText: "line one\n.line two", idempotencyKey: "scan:1" });
  assert.ok(transport?.writes.some((write) => write.startsWith("EHLO")));
  assert.ok(transport?.writes.some((write) => write.includes("..line two")));
});

test("SMTP adapter fails closed on a socket timeout", async () => {
  const mailer = new GmailSmtpMailer("user@example.com", "app-password", async () => new FakeTransport(true), 100);
  await assert.rejects(mailer.send({ recipient: "recipient@example.com", subject: "test", bodyText: "body", idempotencyKey: "scan:timeout" }), /SMTP timeout/);
});

test("Supabase REST sends a new sb_secret key only as apikey", async () => {
  const originalFetch = globalThis.fetch;
  const secretKey = "sb_secret_protocol_fixture";
  let captured: Headers | undefined;
  globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    captured = new Headers(init?.headers);
    return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    await new SupabaseRestRepository("https://project.supabase.co", secretKey).getCurrentConfigVersion();
    assert.equal(captured?.get("apikey"), secretKey);
    assert.equal(captured?.get("authorization"), null);
    assert.equal(captured?.get("content-type"), "application/json");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Supabase REST keeps both headers for a legacy JWT service-role key", async () => {
  const originalFetch = globalThis.fetch;
  const legacyKey = "legacy.jwt.service-role.fixture";
  let captured: Headers | undefined;
  globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    captured = new Headers(init?.headers);
    return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    await new SupabaseRestRepository("https://project.supabase.co", legacyKey).getCurrentConfigVersion();
    assert.equal(captured?.get("apikey"), legacyKey);
    assert.equal(captured?.get("authorization"), `Bearer ${legacyKey}`);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Supabase REST redacts new and legacy keys from error details", async () => {
  const originalFetch = globalThis.fetch;
  const keys = ["sb_secret_redaction_fixture", "legacy.jwt.redaction.fixture"];
  try {
    for (const key of keys) {
      globalThis.fetch = (async (): Promise<Response> => new Response(`provider rejected ${key}`, { status: 401 })) as typeof fetch;
      await assert.rejects(
        new SupabaseRestRepository("https://project.supabase.co", key).getCurrentConfigVersion(),
        (error: unknown) => {
          assert.ok(error instanceof Error);
          assert.match(error.message, /Supabase request failed \(401\): provider rejected \[REDACTED\]/);
          assert.doesNotMatch(error.message, new RegExp(key.replaceAll(".", "\\.")));
          return true;
        },
      );
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Binance funding adapter uses bounded all-market pagination and validates events", async () => {
  const originalFetch = globalThis.fetch;
  const urls: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL): Promise<Response> => {
    urls.push(String(input));
    return new Response(JSON.stringify([{ symbol: "BTCUSDT", fundingTime: 1_700_000_900_000, fundingRate: "0.001", markPrice: "100" }]), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    const client = new BinancePublicClient("https://fapi.binance.com", 1_000);
    const events = await client.getFundingEvents(["BTCUSDT"], 1_700_000_000_000, 1_700_003_600_000);
    assert.deepEqual(events, [{ symbol: "BTCUSDT", fundingTime: 1_700_000_900_000, fundingRate: 0.001, markPrice: 100 }]);
    assert.equal(urls.length, 1);
    assert.match(urls[0] ?? "", /\/fapi\/v1\/fundingRate/);
    assert.doesNotMatch(urls[0] ?? "", /symbol=/);
    await assert.rejects(client.getFundingEvents(["BTCUSDT"], 1_700_000_000_000, 1_700_000_000_000), /funding window is invalid/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Binance funding pagination preserves every symbol at tied timestamps", async () => {
  const originalFetch = globalThis.fetch;
  const firstTime = 1_700_000_900_000;
  const secondTime = 1_700_001_800_000;
  const rows = [
    ...Array.from({ length: 501 }, (_, index) => ({ symbol: `A${String(index).padStart(4, "0")}USDT`, fundingTime: firstTime, fundingRate: "0.001", markPrice: "100" })),
    ...Array.from({ length: 501 }, (_, index) => ({ symbol: `B${String(index).padStart(4, "0")}USDT`, fundingTime: secondTime, fundingRate: "-0.001", markPrice: "100" })),
  ];
  globalThis.fetch = (async (input: RequestInfo | URL): Promise<Response> => {
    const url = new URL(String(input));
    const startTime = Number(url.searchParams.get("startTime"));
    const endTime = Number(url.searchParams.get("endTime"));
    const page = rows.filter((row) => row.fundingTime >= startTime && row.fundingTime <= endTime).slice(0, 1000);
    return new Response(JSON.stringify(page), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    const client = new BinancePublicClient("https://fapi.binance.com", 1_000);
    const events = await client.getFundingEvents(rows.map((row) => row.symbol), 1_700_000_000_000, 1_700_003_600_000);
    assert.equal(events.length, rows.length);
    assert.equal(new Set(events.map((event) => `${event.symbol}:${event.fundingTime}`)).size, rows.length);
    assert.equal(events.filter((event) => event.fundingTime === firstTime).length, 501);
    assert.equal(events.filter((event) => event.fundingTime === secondTime).length, 501);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("SQL simulation contract reconciles shadow rows and persists funding watermarks", () => {
  const sql = readFileSync(join(process.cwd(), "supabase", "migrations", "202608050003_live_simulation.sql"), "utf8");
  const reconcile = sql.slice(sql.indexOf("create or replace function public.sol_reconcile_simulation"), sql.indexOf("create or replace function public.sol_get_simulation_state"));
  assert.match(reconcile, /status in \('pending', 'open', 'shadow'\)/);
  assert.doesNotMatch(reconcile, /shadow_only = false/);
  assert.match(reconcile, /fundingEvents/);
  assert.match(reconcile, /funding events missing/);
  assert.match(reconcile, /accrued_funding_r/);
  assert.match(reconcile, /fees_r/);
  assert.match(reconcile, /v_position\.shadow_only/);
  assert.match(sql, /last_funding_observation_at/);
  assert.match(sql, /values \(v_state_key,[\s\S]*, true, not v_formal,/);
  assert.match(sql, /v_state_key := v_signal->>'symbol' \|\| ':' \|\| v_signal->>'direction';/);
  assert.doesNotMatch(sql, /v_state_key := v_signal->>'symbol' \|\| ':' \|\| v_signal->>'family' \|\| ':' \|\| v_signal->>'direction';/);
  assert.match(sql, /attempt_count < 4/);
  assert.match(sql, /attempt_count >= 4/);
  assert.match(sql, /values \(v_signal_id,[\s\S]*'pending',[\s\S]*true/);
});
