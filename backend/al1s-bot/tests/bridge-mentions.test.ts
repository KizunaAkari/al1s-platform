import { describe, expect, it } from "vitest";
import type { Message } from "discord.js";

import type { AppConfig } from "../src/config.js";
import { MessageDeduplicator } from "../src/bridge/dedupe.js";
import { DiscordRuleBridge } from "../src/bridge/service.js";
import type { ForwardRule } from "../src/bridge/forward-rule-matcher.js";
import { HealthState } from "../src/health/state.js";
import { Logger } from "../src/logger.js";
import type { RuleForwardMessage } from "../src/platform-ingress.js";

const config: AppConfig = {
  discord: { token: "unused", applicationId: "12345678901234567" },
  forward: { prefix: "[Discord]", dedupeTtlMs: 60_000 },
  health: { host: "127.0.0.1", port: 3100 }, logLevel: "error",
};

function message(id: string, content: string, cleanContent: string, everyone = false): Message {
  const mentioned = { id: "577928624884154368", username: "yukari_user", globalName: "Yukari" };
  return {
    id, content, cleanContent, guildId: "g", channelId: "c",
    guild: { name: "server" }, channel: { name: "general" },
    author: { id: "author", username: "author", globalName: null, bot: false },
    member: null, webhookId: null, url: `https://discord.com/channels/g/c/${id}`,
    attachments: { map: () => [] },
    mentions: {
      everyone, users: new Map([[mentioned.id, mentioned]]),
      members: new Map([[mentioned.id, { displayName: "结月缘" }]]),
    },
  } as unknown as Message;
}

function rule(id: string, text: string): ForwardRule {
  return { id, version: 1, guildId: "g", channelId: "c", trigger: { kind: "contains", text } };
}

describe("Discord mention matching at the bridge boundary", () => {
  it("does not mark a message processed before the first rule snapshot", async () => {
    const submitted: RuleForwardMessage[] = [];
    const bridge = new DiscordRuleBridge(config, new MessageDeduplicator(60_000), new HealthState(),
      new Logger("error"), async payload => { submitted.push(payload); });
    const first = message("first", "ready", "ready");
    await bridge.handle(first);
    bridge.setRules([rule("initial", "ready")]);
    await bridge.handle(first);
    expect(submitted).toHaveLength(1);
  });
  it("matches a real user mention by guild display name and ID, then forwards resolved text", async () => {
    const submitted: RuleForwardMessage[] = [];
    const bridge = new DiscordRuleBridge(config, new MessageDeduplicator(60_000), new HealthState(),
      new Logger("error"), async payload => { submitted.push(payload); });
    bridge.setRules([rule("name", "@结月缘"), rule("id", "@577928624884154368")]);
    await bridge.handle(message("1", "请看 <@!577928624884154368>", "请看 @结月缘"));
    expect(submitted.map(item => item.rule_id)).toEqual(["name", "id"]);
    expect(submitted[0]?.body).toContain("请看 @结月缘");
  });

  it("uses the Discord everyone flag and excludes @here", async () => {
    const submitted: RuleForwardMessage[] = [];
    const bridge = new DiscordRuleBridge(config, new MessageDeduplicator(60_000), new HealthState(),
      new Logger("error"), async payload => { submitted.push(payload); });
    bridge.setRules([rule("everyone", "@everyone")]);
    await bridge.handle(message("1", "@everyone 注意", "@everyone 注意", false));
    await bridge.handle(message("2", "@here 注意", "@here 注意", true));
    await bridge.handle(message("3", "@everyone 注意", "@everyone 注意", true));
    expect(submitted.map(item => item.message_ids)).toEqual([["3"]]);
  });
});
