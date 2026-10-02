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
});
