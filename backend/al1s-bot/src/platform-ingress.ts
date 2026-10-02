import { createCipheriv, createDecipheriv, randomBytes } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

export type RuleForwardMessage = {
  kind: "rule_match"; event_id: string; rule_id: string; rule_version: number;
  guild_id: string; channel_id: string; message_ids: string[]; body: string;
};

export type IngressFlushResult = { delivered: number; failureCode: string | null };

export class EncryptedIngressQueue {
  private constructor(private readonly database: DatabaseSync, private readonly key: Buffer) {}

  static async open(directory: string): Promise<EncryptedIngressQueue> {
    await mkdir(directory, { recursive: true, mode: 0o700 });
    const path = join(directory, "ingress.key");
    try { await writeFile(path, randomBytes(32), { flag: "wx", mode: 0o600 }); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error; }
    const key = await readFile(path);
    if (key.length !== 32) throw new Error("invalid_ingress_key");
    const database = new DatabaseSync(join(directory, "ingress.sqlite"));
    try {
      database.exec("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, body BLOB NOT NULL, received INTEGER NOT NULL, blocked INTEGER NOT NULL DEFAULT 0)");
      const columns = database.prepare("PRAGMA table_info(messages)").all() as Array<{ name: string }>;
      if (!columns.some(column => column.name === "blocked_at")) {
        database.exec("ALTER TABLE messages ADD COLUMN blocked_at INTEGER");
      }
      if (!columns.some(column => column.name === "reason_code")) {
        database.exec("ALTER TABLE messages ADD COLUMN reason_code TEXT");
      }
      return new EncryptedIngressQueue(database, key);
    } catch (error) {
      database.close();
      throw error;
    }
  }

  enqueue(message: RuleForwardMessage): void {
    if (Buffer.byteLength(JSON.stringify(message)) > 512_000) throw new Error("ingress_message_too_large");
    const id = message.event_id;
    const existing = this.database.prepare("SELECT id FROM messages WHERE id=?").get(id);
    if (existing) return;
    const count = this.database.prepare("SELECT count(*) AS n FROM messages").get() as { n: number };
    if (count.n >= 10_000) throw new Error("ingress_queue_full");
    const iv = randomBytes(12);
    const cipher = createCipheriv("aes-256-gcm", this.key, iv);
    cipher.setAAD(Buffer.from(id));
    const encrypted = Buffer.concat([cipher.update(JSON.stringify(message), "utf8"), cipher.final()]);
    const body = Buffer.concat([iv, cipher.getAuthTag(), encrypted]);
    this.database.prepare("INSERT OR IGNORE INTO messages(id,body,received) VALUES(?,?,?)")
      .run(id, body, Date.now());
  }

  async flush(send: (message: RuleForwardMessage) => Promise<number | { status: number; code?: string }>): Promise<IngressFlushResult> {
    let delivered = 0;
    this.purgeExpired();
    const rows = this.database.prepare("SELECT id,body FROM messages WHERE blocked=0 ORDER BY received,id LIMIT 10")
      .all() as Array<{ id: string; body: Uint8Array }>;
    for (const row of rows) {
      let message: unknown;
      try {
        const raw = Buffer.from(row.body);
        const decipher = createDecipheriv("aes-256-gcm", this.key, raw.subarray(0, 12));
        decipher.setAAD(Buffer.from(row.id));
        decipher.setAuthTag(raw.subarray(12, 28));
        message = JSON.parse(Buffer.concat([decipher.update(raw.subarray(28)), decipher.final()]).toString());
      } catch {
        this.database.prepare("UPDATE messages SET blocked=1,blocked_at=?,reason_code=? WHERE id=?")
          .run(Date.now(), "unreadable_payload", row.id);
        continue;
      }
      if (!message || typeof message !== "object" || !("kind" in message) || message.kind !== "rule_match") {
        // Historical encrypted ingress is retained for diagnosis but must never use the retired route.
        this.database.prepare("UPDATE messages SET blocked=1,blocked_at=?,reason_code=? WHERE id=?")
          .run(Date.now(), "legacy_path_retired", row.id);
        continue;
      }
      const response = await send(message as RuleForwardMessage);
      const status = typeof response === "number" ? response : response.status;
      const permanentCode = typeof response !== "number" && (
        (status === 403 && ["source_not_allowed", "discord_rule_no_destination"].includes(response.code ?? ""))
        || (status === 409 && ["discord_rule_stale", "source_content_conflict"].includes(response.code ?? ""))
      ) ? response.code : undefined;
      if (status === 202) {
        this.database.prepare("DELETE FROM messages WHERE id=?").run(row.id);
        delivered += 1;
      }
      else if ([400, 422].includes(status) || permanentCode) {
        // Keep encrypted data for diagnosis; do not retry permanent rejection forever.
        this.database.prepare("UPDATE messages SET blocked=1,blocked_at=?,reason_code=? WHERE id=?")
          .run(Date.now(), permanentCode ?? `upstream_${status}`, row.id);
      } else {
        const failureCode = status === 429 ? "upstream_rate_limited"
          : status >= 500 ? "upstream_unavailable" : "upstream_rejected";
        return { delivered, failureCode };
      }
    }
    return { delivered, failureCode: null };
  }
  purgeExpired(now = Date.now()): number {
    // Legacy blocked rows have no finalization timestamp or reliable reason.
    // Keep them encrypted until an operator inventories and resolves their provenance.
    const result = this.database.prepare(
      "DELETE FROM messages WHERE blocked=1 AND blocked_at IS NOT NULL AND blocked_at <= ?"
    ).run(now - 30 * 24 * 60 * 60 * 1000);
    return Number(result.changes);
  }
  stats(): { ingress_pending: number; ingress_blocked: number } {
    const row = this.database.prepare(
      "SELECT count(CASE WHEN blocked=0 THEN 1 END) AS ingress_pending, count(CASE WHEN blocked=1 THEN 1 END) AS ingress_blocked FROM messages"
    ).get() as { ingress_pending: number; ingress_blocked: number };
    return { ingress_pending: row.ingress_pending, ingress_blocked: row.ingress_blocked };
  }
  close(): void { this.database.close(); }
}
