import { connect } from "node:tls";
import { assertSafeHeader } from "../security.js";
import type { Mailer } from "../types.js";

export interface SmtpTransport {
  write(data: string): boolean;
  once(event: "data" | "error" | "timeout", listener: (...args: any[]) => void): this;
  off(event: "data" | "error" | "timeout", listener: (...args: any[]) => void): this;
  end(): void;
  destroy?(error?: Error): void;
}

function responseCode(response: string): number {
  const match = response.match(/^(\d{3})[ -]/m);
  return match ? Number(match[1]) : 0;
}

export class GmailSmtpMailer implements Mailer {
  public constructor(
    private readonly username: string,
    private readonly appPassword: string,
    private readonly transportFactory?: () => Promise<SmtpTransport>,
    private readonly timeoutMs = 15_000,
  ) {}

  private async openTransport(): Promise<SmtpTransport> {
    if (this.transportFactory) return this.transportFactory();
    return new Promise<SmtpTransport>((resolveSocket, reject) => {
      const connection = connect({ host: "smtp.gmail.com", port: 465, servername: "smtp.gmail.com" }, () => resolveSocket(connection));
      connection.setTimeout(this.timeoutMs, () => connection.destroy(new Error("SMTP timeout")));
      connection.once("error", reject);
    });
  }

  async send(message: { recipient: string; subject: string; bodyText: string; idempotencyKey: string }): Promise<void> {
    assertSafeHeader(message.recipient, "recipient");
    assertSafeHeader(message.subject, "subject");
    assertSafeHeader(message.idempotencyKey, "idempotency key");
    const socket = await this.openTransport();
    let buffer = "";
    const readResponse = async (): Promise<string> => {
      while (true) {
        const lineEnd = buffer.indexOf("\r\n");
        if (lineEnd >= 0) {
          const line = buffer.slice(0, lineEnd);
          buffer = buffer.slice(lineEnd + 2);
          if (/^\d{3} /.test(line) || !/^\d{3}-/.test(line)) return line;
          continue;
        }
        const chunk = await new Promise<Buffer>((resolveChunk, rejectChunk) => {
          const onData = (data: Buffer | string): void => {
            cleanup();
            resolveChunk(typeof data === "string" ? Buffer.from(data) : data);
          };
          const onError = (error: Error): void => {
            cleanup();
            rejectChunk(error);
          };
          const onTimeout = (): void => onError(new Error("SMTP timeout"));
          const cleanup = (): void => {
            socket.off("data", onData);
            socket.off("error", onError);
            socket.off("timeout", onTimeout);
          };
          socket.once("data", onData);
          socket.once("error", onError);
          socket.once("timeout", onTimeout);
        });
        buffer += chunk.toString("utf8");
      }
    };
    const command = async (line: string, expected: number): Promise<void> => {
      socket.write(`${line}\r\n`);
      const response = await readResponse();
      if (responseCode(response) !== expected) throw new Error(`SMTP command failed: ${response.slice(0, 120)}`);
    };
    try {
      await readResponse();
      await command("EHLO sol-alerts", 250);
      await command("AUTH LOGIN", 334);
      await command(Buffer.from(this.username).toString("base64"), 334);
      await command(Buffer.from(this.appPassword).toString("base64"), 235);
      await command(`MAIL FROM:<${this.username}>`, 250);
      await command(`RCPT TO:<${message.recipient}>`, 250);
      socket.write("DATA\r\n");
      if (responseCode(await readResponse()) !== 354) throw new Error("SMTP DATA command failed");
      const body = message.bodyText.replace(/\r?\n/g, "\r\n").replace(/^\./gm, "..");
      socket.write([
        `From: Sol Alerts <${this.username}>`,
        `To: ${message.recipient}`,
        `Subject: ${message.subject}`,
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=utf-8",
        "Content-Transfer-Encoding: 8bit",
        `X-Sol-Alerts-Idempotency: ${message.idempotencyKey}`,
        "",
        body,
        ".",
        "",
      ].join("\r\n"));
      if (responseCode(await readResponse()) !== 250) throw new Error("SMTP message delivery failed");
      await command("QUIT", 221);
    } finally {
      socket.end();
    }
  }
}
