export type Trigger =
  | { kind: "contains"; text: string }
  | { kind: "acrostic"; text: string }
  | { kind: "frequency"; count: number; windowSeconds: number; cooldownSeconds: number };

export interface ForwardRule {
  id: string;
  version: number;
  guildId: string;
  channelId: string;
  trigger: Trigger;
}

export interface SourceMessage {
  id: string;
  guildId: string;
  channelId: string;
  authorId: string;
  content: string;
  displayContent?: string;
  mentionAliases?: readonly string[];
  mentionsEveryone?: boolean;
  formattedBody: string;
  receivedAt: number;
}

export interface RuleMatch {
  rule: ForwardRule;
  messages: readonly SourceMessage[];
}

interface WindowState {
  version: number;
  updatedAt: number;
  retainUntil: number;
  messages: SourceMessage[];
  cooldownUntil: number;
}

const IDLE_MS = 10 * 60 * 1000;
const MAX_MESSAGES = 64;
const MAX_ACTIVE_WINDOWS = 512;

function firstCharacter(content: string): string {
  return Array.from(content.trimStart())[0] ?? "";
}

/** Ephemeral state only. A rule revision or worker restart starts a new window. */
export class ForwardRuleMatcher {
  private readonly windows = new Map<string, WindowState>();

  public constructor(private readonly now: () => number = Date.now) {}

  public match(rule: ForwardRule, message: SourceMessage): RuleMatch | null {
    if (rule.guildId !== message.guildId || rule.channelId !== message.channelId) return null;
    if (rule.trigger.kind === "contains") {
      const text = rule.trigger.text;
      if (text.includes("@everyone") && (!message.mentionsEveryone || !message.content.includes("@everyone"))) {
        return null;
      }
      const found = message.content.includes(text)
        || (message.displayContent?.includes(text) ?? false)
        || (message.mentionAliases?.includes(text) ?? false);
      return found ? { rule, messages: [message] } : null;
    }

    const now = this.now();
    this.prune(now);
    const key = `${rule.id}:${message.guildId}:${message.channelId}:${message.authorId}`;
    let state = this.windows.get(key);
    if (!state || state.version !== rule.version || now >= state.retainUntil) {
      state = { version: rule.version, updatedAt: now, retainUntil: now + IDLE_MS,
        messages: [], cooldownUntil: 0 };
      this.windows.set(key, state);
    }
    state.updatedAt = now;
    state.retainUntil = Math.max(state.retainUntil, now + IDLE_MS);
    if (rule.trigger.kind === "acrostic") {
      if (!firstCharacter(message.content)) return null;
      state.messages.push(message);
      if (state.messages.length > MAX_MESSAGES) state.messages.shift();
      const width = Array.from(rule.trigger.text).length;
      if (width < 1 || width > MAX_MESSAGES || state.messages.length < width) return null;
      const selected = state.messages.slice(-width);
      if (selected.map(item => firstCharacter(item.content)).join("") !== rule.trigger.text) return null;
      return { rule, messages: selected };
    }

    if (now < state.cooldownUntil) {
      state.retainUntil = Math.max(state.retainUntil, state.cooldownUntil);
      return null;
    }
    const start = now - rule.trigger.windowSeconds * 1000;
    state.messages = state.messages.filter(item => item.receivedAt >= start);
    state.messages.push(message);
    state.retainUntil = Math.max(state.retainUntil, now + rule.trigger.windowSeconds * 1000);
    if (state.messages.length < rule.trigger.count) return null;
    const selected = state.messages.splice(0, rule.trigger.count);
    state.cooldownUntil = now + rule.trigger.cooldownSeconds * 1000;
    state.messages.length = 0;
    state.retainUntil = Math.max(now + IDLE_MS, state.cooldownUntil);
    return { rule, messages: selected };
  }

  public retainRules(rules: readonly ForwardRule[]): void {
    const versions = new Map(rules.map(rule => [rule.id, rule.version]));
    for (const [key, state] of this.windows) {
      const version = versions.get(key.slice(0, key.indexOf(":")));
      if (version !== state.version) this.windows.delete(key);
    }
  }

  private prune(now: number): void {
    for (const [key, state] of this.windows) {
      if (now >= state.retainUntil) this.windows.delete(key);
    }
    while (this.windows.size > MAX_ACTIVE_WINDOWS) {
      const oldest = [...this.windows].reduce((left, right) =>
        left[1].updatedAt <= right[1].updatedAt ? left : right);
      this.windows.delete(oldest[0]);
    }
  }
}
