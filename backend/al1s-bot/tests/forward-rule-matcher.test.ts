import { describe, expect, it } from "vitest";

import { ForwardRuleMatcher, type ForwardRule, type SourceMessage } from "../src/bridge/forward-rule-matcher.js";

const message = (id: number, content: string, at: number, authorId = "a"): SourceMessage => ({
  id: String(id), guildId: "g", channelId: "c", authorId, content,
  formattedBody: `source ${id}: ${content}`, receivedAt: at,
});
const rule = (trigger: ForwardRule["trigger"]): ForwardRule => ({
  id: "rule", version: 1, guildId: "g", channelId: "c", trigger,
});

describe("Discord forwarding trigger windows", () => {
  it("matches configured text in the original message", () => {
    const matcher = new ForwardRuleMatcher();
    const target = rule({ kind: "contains", text: "报警" });
    expect(matcher.match(target, message(1, "需要报警", 0))?.messages.map(item => item.id)).toEqual(["1"]);
    expect(matcher.match(target, message(2, "无异常", 0))).toBeNull();
  });

  it("matches a user mention by ID or resolved display name", () => {
    const matcher = new ForwardRuleMatcher();
    const source = {
      ...message(1, "请看 <@577928624884154368>", 0),
      displayContent: "请看 @结月缘",
      mentionAliases: ["@577928624884154368", "@结月缘", "@Yukari", "@yukari_user"],
    };
    for (const text of ["@577928624884154368", "@结月缘", "@Yukari", "@yukari_user", "请看 @结月缘"]) {
      expect(matcher.match(rule({ kind: "contains", text }), source)?.messages).toHaveLength(1);
    }
    expect(matcher.match(rule({ kind: "contains", text: "@其他人" }), source)).toBeNull();
    const legacyMention = { ...source, content: "请看 <@!577928624884154368>", displayContent: "请看 @结月缘" };
    expect(matcher.match(rule({ kind: "contains", text: "@577928624884154368" }), legacyMention)?.messages).toHaveLength(1);
  });

  it("only matches a real @everyone mention, not @here or plain text", () => {
    const matcher = new ForwardRuleMatcher();
    const target = rule({ kind: "contains", text: "@everyone" });
    expect(matcher.match(target, { ...message(1, "@everyone 注意", 0), mentionsEveryone: true })?.messages).toHaveLength(1);
    expect(matcher.match(target, { ...message(2, "@everyone 注意", 0), mentionsEveryone: false })).toBeNull();
    expect(matcher.match(target, { ...message(3, "@here 注意", 0), mentionsEveryone: true })).toBeNull();
  });

  it("uses the same author's recent leading characters and expires after ten idle minutes", () => {
    let now = 0;
    const matcher = new ForwardRuleMatcher(() => now);
    const target = rule({ kind: "acrostic", text: "救命" });
    matcher.match(target, message(1, " 救我", now));
    matcher.match(target, message(2, "其余", now, "b"));
    expect(matcher.match(target, message(3, "命令", now))?.messages.map(item => item.id)).toEqual(["1", "3"]);
    now = 600_000;
    expect(matcher.match(target, message(4, "命令", now))).toBeNull();
    expect(matcher.match({ ...target, version: 2 }, message(5, "救援", now))).toBeNull();
  });

  it("triggers each configured batch inside the window and ignores cooldown messages", () => {
    let now = 0;
    const matcher = new ForwardRuleMatcher(() => now);
    const target = rule({ kind: "frequency", count: 3, windowSeconds: 10, cooldownSeconds: 2 });
    matcher.match(target, message(1, "一", 0));
    matcher.match(target, message(2, "二", 0));
    expect(matcher.match(target, message(3, "三", 0))?.messages.map(item => item.id)).toEqual(["1", "2", "3"]);
    now = 1_000;
    expect(matcher.match(target, message(4, "冷却", now))).toBeNull();
    now = 2_000;
    matcher.match(target, message(5, "四", now));
    matcher.match(target, message(6, "五", now));
    expect(matcher.match(target, message(7, "六", now))?.messages.map(item => item.id)).toEqual(["5", "6", "7"]);
  });

  it("drops messages outside the frequency window", () => {
    let now = 0;
    const matcher = new ForwardRuleMatcher(() => now);
    const target = rule({ kind: "frequency", count: 2, windowSeconds: 10, cooldownSeconds: 0 });
    matcher.match(target, message(1, "旧", now));
    now = 10_001;
    expect(matcher.match(target, message(2, "新", now))).toBeNull();
  });

  it("keeps a long frequency window and cooldown through ten idle minutes", () => {
    let now = 0;
    const matcher = new ForwardRuleMatcher(() => now);
    const accumulation = rule({ kind: "frequency", count: 2, windowSeconds: 3600, cooldownSeconds: 0 });
    expect(matcher.match(accumulation, message(1, "一", now))).toBeNull();
    now = 660_000;
    expect(matcher.match(accumulation, message(2, "二", now))?.messages.map(item => item.id))
      .toEqual(["1", "2"]);

    const cooldown = { ...rule({ kind: "frequency", count: 2,
      windowSeconds: 3600, cooldownSeconds: 3600 }), id: "cooldown" };
    now = 0;
    matcher.match(cooldown, message(3, "一", now));
    expect(matcher.match(cooldown, message(4, "二", now))).not.toBeNull();
    now = 660_000;
    expect(matcher.match(cooldown, message(5, "冷却", now))).toBeNull();
    now = 3_599_999;
    expect(matcher.match(cooldown, message(6, "仍冷却", now))).toBeNull();
    now = 3_600_000;
    expect(matcher.match(cooldown, message(7, "新一", now))).toBeNull();
    expect(matcher.match(cooldown, message(8, "新二", now))?.messages.map(item => item.id))
      .toEqual(["7", "8"]);
  });

  it("isolates long windows by author and resets them when the rule version changes", () => {
    let now = 0;
    const matcher = new ForwardRuleMatcher(() => now);
    const target = rule({ kind: "frequency", count: 2, windowSeconds: 3600, cooldownSeconds: 3600 });
    matcher.match(target, message(1, "甲", now));
    now = 660_000;
    expect(matcher.match(target, message(2, "乙", now, "b"))).toBeNull();
    expect(matcher.match({ ...target, version: 2 }, message(3, "甲新版本", now))).toBeNull();
    expect(matcher.match(target, message(4, "甲旧版本", now, "a"))).toBeNull();
  });

  it("matches a 64-character acrostic using only the latest 64 messages", () => {
    const matcher = new ForwardRuleMatcher(() => 100);
    const target = rule({ kind: "acrostic", text: "甲".repeat(64) });
    for (let index = 1; index < 64; index += 1) {
      expect(matcher.match(target, message(index, "甲消息", 100))).toBeNull();
    }
    expect(matcher.match(target, message(64, "甲消息", 100))?.messages).toHaveLength(64);
    expect(matcher.match(target, message(65, "乙消息", 100))).toBeNull();
    expect(matcher.match(target, message(66, "甲消息", 100))).toBeNull();
  });
});
