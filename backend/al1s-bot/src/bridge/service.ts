import type { Message } from "discord.js";
import { createHash } from "node:crypto";

import type { AppConfig } from "../config.js";
import type { HealthState } from "../health/state.js";
import type { Logger } from "../logger.js";
import type { RuleForwardMessage } from "../platform-ingress.js";
import type { MessageDeduplicator } from "./dedupe.js";
import { formatDiscordMessageForQq } from "./formatter.js";
import { ForwardRuleMatcher, type ForwardRule, type SourceMessage } from "./forward-rule-matcher.js";

export class DiscordRuleBridge {
  private rules: readonly ForwardRule[] = [];
  private rulesLoaded = false;
  private readonly matcher = new ForwardRuleMatcher();

  public setRules(rules: readonly ForwardRule[]): void {
    this.matcher.retainRules(rules);
    this.rules = rules;
    this.rulesLoaded = true;
  }

  public constructor(
    private readonly config: AppConfig,
    private readonly deduplicator: MessageDeduplicator,
    private readonly health: HealthState,
    private readonly logger: Logger,
    private readonly submit: (message: RuleForwardMessage) => Promise<void>,
  ) {}

  public async handle(message: Message): Promise<void> {
    if (message.guildId === null || message.author.bot || message.webhookId !== null) return;
    if (!this.rulesLoaded) return;
    if (!this.deduplicator.tryAcquire(message.id)) return;
    try {
      const channelName = "name" in message.channel && typeof message.channel.name === "string"
        ? message.channel.name
        : message.channelId;
      const text = formatDiscordMessageForQq(
        {
          guildName: message.guild?.name ?? message.guildId ?? "unknown-guild",
          channelName,
          displayName: message.member?.displayName ?? message.author.globalName ?? message.author.username,
          username: message.author.username,
          content: message.cleanContent || message.content,
          attachmentUrls: message.attachments.map((attachment) => attachment.url),
          messageUrl: message.url
        },
        this.config.forward.prefix
      );
      const mentionAliases = new Set<string>();
      for (const [, id] of message.content.matchAll(/<@!?(\d+)>/g)) {
        if (!id) continue;
        const user = message.mentions.users.get(id);
        if (!user) continue;
        const member = message.mentions.members?.get(id);
        for (const name of [id, member?.displayName, user.globalName, user.username]) {
          if (name) mentionAliases.add(`@${name}`);
        }
      }
      const source: SourceMessage = {
        id: message.id, guildId: message.guildId, channelId: message.channelId,
        authorId: message.author.id, content: message.content,
        displayContent: message.cleanContent, mentionAliases: [...mentionAliases],
        mentionsEveryone: message.mentions.everyone,
        formattedBody: text, receivedAt: Date.now(),
      };
      const matches = this.rules.map(rule => this.matcher.match(rule, source)).filter(item => item !== null);
      for (const hit of matches) {
        const ids = hit.messages.map(item => item.id);
        const eventId = createHash("sha256").update(`${hit.rule.id}:${ids.join(",")}`).digest("hex");
        const body = hit.messages.map(item => item.formattedBody).join("\n\n——\n\n");
        await this.submit({
          kind: "rule_match", event_id: eventId,
          rule_id: hit.rule.id, rule_version: hit.rule.version,
          guild_id: hit.rule.guildId, channel_id: hit.rule.channelId,
          message_ids: ids, body,
        });
        this.health.recordForward(message.id);
      }
      this.deduplicator.complete(message.id);
      if (matches.length) {
        this.logger.info("discord_message_queued_for_review", {
          discordMessageId: message.id, matchedRuleCount: matches.length,
          guildId: message.guildId, channelId: message.channelId,
        });
      }
    } catch (error) {
      this.deduplicator.fail(message.id);
      this.health.recordError(error);
      this.logger.error("discord_message_forward_failed", {
        discordMessageId: message.id,
        error
      });
    }
  }
}
