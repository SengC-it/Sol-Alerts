import { timingSafeEqual } from "node:crypto";

export function constantTimeEqual(left: string, right: string): boolean {
  const leftBuffer = Buffer.from(left);
  const rightBuffer = Buffer.from(right);
  if (leftBuffer.length !== rightBuffer.length) return false;
  return timingSafeEqual(leftBuffer, rightBuffer);
}

export function redactSecrets(value: string, secrets: string[]): string {
  return secrets.filter((secret) => secret.length > 0).reduce((output, secret) => output.split(secret).join("[REDACTED]"), value);
}

export function assertSafeHeader(value: string, label: string): void {
  if (/[\r\n]/.test(value)) throw new Error(`Invalid ${label}`);
}
