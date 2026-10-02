import { createCipheriv, randomBytes } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { afterEach, describe, expect, it } from "vitest";
import { EncryptedIngressQueue, type RuleForwardMessage } from "../src/platform-ingress.js";

const directories: string[] = [];
afterEach(async () => { for (const path of directories.splice(0)) await rm(path, { recursive: true, force: true }); });
async function directory() {
  const path = await mkdtemp(join(tmpdir(), "al1s-ingress-test-"));
  directories.push(path);
  return path;
}
const match: RuleForwardMessage = {
  kind: "rule_match", event_id: "event-1", rule_id: "rule-1", rule_version: 1,
  guild_id: "223456789012345678", channel_id: "323456789012345678",
  message_ids: ["123456789012345678"], body: "test-body-never-stored-plaintext",
};

describe("rule-only encrypted ingress", () => {
  it("deduplicates, encrypts, and acknowledges a rule match", async () => {
    const path = await directory();
    const queue = await EncryptedIngressQueue.open(path);
    try {
      queue.enqueue(match); queue.enqueue(match);
      expect(queue.stats()).toEqual({ ingress_pending: 1, ingress_blocked: 0 });
      const database = new DatabaseSync(join(path, "ingress.sqlite"));
      const stored = database.prepare("SELECT body FROM messages").get() as { body: Uint8Array };
      database.close();
      expect(Buffer.from(stored.body).includes(Buffer.from(match.body))).toBe(false);
      const sent: RuleForwardMessage[] = [];
      await queue.flush(async item => { sent.push(item); return 202; });
      expect(sent).toEqual([match]);
      expect(queue.stats()).toEqual({ ingress_pending: 0, ingress_blocked: 0 });
    } finally { queue.close(); }
  });

  it("quarantines historical legacy payloads without sending them", async () => {
    const path = await directory();
    const queue = await EncryptedIngressQueue.open(path);
    try {
      const id = "123456789012345678";
      const key = await readFile(join(path, "ingress.key"));
      const iv = randomBytes(12);
      const cipher = createCipheriv("aes-256-gcm", key, iv);
      cipher.setAAD(Buffer.from(id));
      const encrypted = Buffer.concat([cipher.update(JSON.stringify({ message_id: id, body: "old message" })), cipher.final()]);
      const database = new DatabaseSync(join(path, "ingress.sqlite"));
      database.prepare("INSERT INTO messages(id,body,received) VALUES(?,?,?)")
        .run(id, Buffer.concat([iv, cipher.getAuthTag(), encrypted]), Date.now());
      database.close();
      queue.enqueue(match);
      const sent: RuleForwardMessage[] = [];
      await queue.flush(async item => { sent.push(item); return 202; });
      expect(sent).toEqual([match]);
      expect(queue.stats()).toEqual({ ingress_pending: 0, ingress_blocked: 1 });
      const inspect = new DatabaseSync(join(path, "ingress.sqlite"));
      expect(inspect.prepare("SELECT reason_code FROM messages WHERE id=?").get(id)).toMatchObject({ reason_code: "legacy_path_retired" });
      inspect.close();
    } finally { queue.close(); }
  });

  it("blocks stale rules and retains retryable failures", async () => {
    const queue = await EncryptedIngressQueue.open(await directory());
    try {
      queue.enqueue(match);
      await queue.flush(async () => 503);
      expect(queue.stats()).toEqual({ ingress_pending: 1, ingress_blocked: 0 });
      await queue.flush(async () => ({ status: 409, code: "discord_rule_stale" }));
      expect(queue.stats()).toEqual({ ingress_pending: 0, ingress_blocked: 1 });
    } finally { queue.close(); }
  });
});
