import { describe, expect, it } from "vitest";

import { MessageDeduplicator } from "../src/bridge/dedupe.js";

describe("MessageDeduplicator", () => {
  it("blocks in-flight and completed duplicates, then expires them", () => {
    let now = 1_000;
    const dedupe = new MessageDeduplicator(500, () => now);

    expect(dedupe.tryAcquire("message-1")).toBe(true);
    expect(dedupe.tryAcquire("message-1")).toBe(false);
    dedupe.complete("message-1");
    expect(dedupe.tryAcquire("message-1")).toBe(false);
    now = 1_501;
    expect(dedupe.tryAcquire("message-1")).toBe(true);
  });

  it("allows retry after a failed delivery", () => {
    const dedupe = new MessageDeduplicator(500);
    expect(dedupe.tryAcquire("message-2")).toBe(true);
    dedupe.fail("message-2");
    expect(dedupe.tryAcquire("message-2")).toBe(true);
  });

  it("expires each completion at its own deadline including clock rollback", () => {
    let now = 1000;
    const dedupe = new MessageDeduplicator(500, () => now);
    dedupe.complete("later");
    now = 800;
    dedupe.complete("earlier");
    now = 1299;
    expect(dedupe.tryAcquire("earlier")).toBe(false);
    now = 1300;
    expect(dedupe.tryAcquire("earlier")).toBe(true);
    expect(dedupe.tryAcquire("later")).toBe(false);
    now = 1500;
    expect(dedupe.tryAcquire("later")).toBe(true);
  });

  it("keeps a refreshed completion protected through the former deadline", () => {
    let now = 1000;
    const dedupe = new MessageDeduplicator(500, () => now);
    dedupe.complete("refreshed");
    now = 1400;
    dedupe.complete("refreshed");
    now = 1500;
    expect(dedupe.tryAcquire("refreshed")).toBe(false);
    now = 1900;
    expect(dedupe.tryAcquire("refreshed")).toBe(true);
    dedupe.fail("refreshed");
    now = 500;
    dedupe.complete("new-after-empty");
    now = 1000;
    expect(dedupe.tryAcquire("new-after-empty")).toBe(true);
  });
});
