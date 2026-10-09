import { z } from "zod";
import { parseMemeCommand, type ParsedCommand } from "./commands.js";
import { catalogSchema, emptyCatalog, type MemeCatalog, type MemeResult } from "./contracts.js";
import { readMemeMedia } from "./media.js";
import { memeSendFailureCode } from "./send-failure.js";

type Avatar = { displayAvatarURL(options?: { forceStatic: boolean; extension: "png"; size: 256 }): string };
type Attachment = { url: string };
export interface MemeMessage {
  id: string; guildId: string | null; channelId: string; content: string;
  author: Avatar & { id: string; bot: boolean }; webhookId: string | null;
  client: { user: { id: string } | null };
  mentions: { users: ReadonlyMap<string, Avatar> };
  attachments: ReadonlyMap<string, Attachment>;
  reference: unknown | null;
  fetchReference(): Promise<{ attachments: ReadonlyMap<string, Attachment> }>;
}
interface Options {
  request(path: string, init: RequestInit): Promise<Response>;
  send(message: MemeMessage, result: MemeResult, signal: AbortSignal): Promise<void>;
  media?(url: string, signal: AbortSignal): Promise<Buffer>;
  report?(code: string): void;
}
const ROOT = "/api/v1/bots/worker/memes";

export class MemeCommandService {
  private catalog: MemeCatalog = emptyCatalog;
  private readonly controller = new AbortController();
  private timer: ReturnType<typeof setInterval> | null = null;
  private refreshing = false;
  private stopped = false;
  private activeGenerators = 0;
  private readonly inflight = new Set<string>();
  private readonly cooldown = new Map<string, number>();
  public constructor(private readonly options: Options) {}

  public start(): void {
    if (this.stopped || this.timer) return;
    void this.refresh();
    this.timer = setInterval(() => void this.refresh(), 30_000);
    this.timer.unref();
  }
  public stop(): void {
    this.stopped = true;
    this.controller.abort();
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.cooldown.clear();
  }
  public async refresh(): Promise<void> {
    if (this.stopped || this.refreshing) return;
    this.refreshing = true;
    try {
      const response = await this.options.request(`${ROOT}/catalog`, { method: "GET", signal: this.controller.signal });
      if (!response.ok) throw new Error("meme_catalog_unavailable");
      const candidate = catalogSchema.parse(await response.json());
      if (!this.stopped) this.catalog = candidate;
    } catch { if (!this.stopped) this.options.report?.("meme_catalog_unavailable"); }
    finally { this.refreshing = false; }
  }

  private permitted(message: MemeMessage): boolean {
    if (message.guildId === null) return this.catalog.private_enabled;
    return this.catalog.scopes.some(scope => scope.kind === "discord_channel"
      && scope.target_id === message.channelId && (!scope.guild_id || scope.guild_id === message.guildId));
  }

  public async handle(message: MemeMessage): Promise<void> {
    if (this.stopped || !this.catalog.enabled || message.author.bot || message.webhookId || !this.permitted(message)) return;
    const command = parseMemeCommand(message.content, message.client.user?.id ?? "", this.catalog);
    if (!command || this.inflight.has(message.id)) return;
    const key = `${message.channelId}:${message.author.id}`;
    if ((this.cooldown.get(key) ?? 0) > Date.now()) return;
    this.inflight.add(message.id);
    const signal = AbortSignal.any([this.controller.signal, AbortSignal.timeout(15_000)]);
    try {
      const claim = await this.json("/claim", { source: {
        message_id: message.id, scope_kind: message.guildId ? "discord_channel" : "discord_private",
        target_id: message.channelId, sender_id: message.author.id, guild_id: message.guildId,
      } }, signal);
      const receipt = z.object({ receipt_id: z.string().uuid().nullable() }).parse(claim).receipt_id;
      if (!receipt || this.stopped) return;
      this.cooldown.set(key, Date.now() + this.catalog.cooldown_seconds * 1000);
      while (this.cooldown.size > 4096) this.cooldown.delete(this.cooldown.keys().next().value!);
      await this.execute(message, command, receipt, signal);
    } catch { if (!this.stopped) this.options.report?.("meme_command_failed"); }
    finally { this.inflight.delete(message.id); }
  }

  private async execute(message: MemeMessage, command: ParsedCommand, receipt: string, signal: AbortSignal): Promise<void> {
    let result: MemeResult;
    let errorCode: string | null = null;
    try {
      result = command.keyword === "表情列表" ? { content: this.listText() }
        : await this.boundedGenerate(message, command, receipt, signal);
    } catch {
      if (this.stopped || signal.aborted) return;
      errorCode = "meme_input_or_render_failed";
      result = { content: "无法生成表情，请检查头像、单张图片和文字是否满足模板要求。发送“表情列表”查看用法。" };
    }
    if (this.stopped || signal.aborted) return;
    const permission = z.object({ allowed: z.boolean() }).parse(await this.json(
      `/${receipt}/send`, { error_code: errorCode }, signal,
    ));
    if (!permission.allowed || this.stopped || signal.aborted) return;
    let status: "sent" | "unknown" = "unknown";
    try { await this.options.send(message, result, signal); status = "sent"; }
    catch (error) {
      const reason = memeSendFailureCode(error);
      errorCode ??= reason;
      this.options.report?.(reason);
    }
    if (!this.stopped) await this.json(`/${receipt}/result`, { status, error_code: errorCode }, signal).catch(
      () => this.options.report?.("meme_receipt_unavailable"),
    );
  }

  private listText(): string {
    return "可用表情：\n" + this.catalog.items.slice(0, 15).map(item =>
      `${item.keyword} · ${item.name}（头像${item.requirements.avatars} / 图片${item.requirements.images} / 文字${item.requirements.texts}）`,
    ).join("\n") + "\n可用：头像合成 模板关键词 @某人、随机表情、配字 文字 + 单张图片。";
  }

  private async boundedGenerate(message: MemeMessage, command: ParsedCommand, receipt: string, signal: AbortSignal): Promise<MemeResult> {
    if (this.activeGenerators >= 2) throw new Error("meme_render_busy");
    ++this.activeGenerators;
    try { return await this.generate(message, command, receipt, signal); }
    finally { --this.activeGenerators; }
  }

  private async generate(message: MemeMessage, command: ParsedCommand, receipt: string, signal: AbortSignal): Promise<MemeResult> {
    const media = this.options.media ?? readMemeMedia;
    const avatars = command.mentionIds.map(id => message.mentions.users.get(id)).filter(user => user !== undefined);
    if (avatars.length > 4 || command.texts.length > 4 || command.texts.join("").length > 200) throw new Error("meme_inputs_exceeded");
    if (!avatars.length) avatars.push(message.author);
    const pictures = [...message.attachments.values()];
    if (!pictures.length && message.reference) pictures.push(...(await message.fetchReference()).attachments.values());
    if (pictures.length > 1) throw new Error("meme_multiple_images");
    const [avatarBytes, imageBytes] = await Promise.all([
      Promise.all(avatars.map(user => media(user.displayAvatarURL({ forceStatic: true, extension: "png", size: 256 }), signal))),
      Promise.all(pictures.map(picture => media(picture.url, signal))),
    ]);
    if (this.stopped || signal.aborted) throw new Error("meme_stopped");
    const response = await this.options.request(`${ROOT}/render`, {
      method: "POST", headers: { "Content-Type": "application/json" }, signal,
      body: JSON.stringify({ receipt_id: receipt, keyword: command.keyword,
        avatars: avatarBytes.map(value => value.toString("base64")),
        images: imageBytes.map(value => value.toString("base64")), texts: command.texts }),
    });
    const type = response.headers.get("content-type");
    if (!response.ok || !["image/png", "image/gif"].includes(type ?? "")) throw new Error("meme_render_failed");
    const body = Buffer.from(await response.arrayBuffer());
    if (!body.length || body.length > 8 * 1024 * 1024) throw new Error("meme_result_size");
    return { body, mediaType: type as "image/png" | "image/gif" };
  }

  private async json(path: string, body: object, signal: AbortSignal): Promise<unknown> {
    const response = await this.options.request(`${ROOT}${path}`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
    if (!response.ok) throw new Error("meme_platform_unavailable");
    return response.status === 204 ? null : response.json();
  }
}
