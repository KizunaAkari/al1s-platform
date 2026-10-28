import { describe, expect, it, vi } from "vitest";
import { parseMemeCommand } from "../src/memes/commands.js";
import { MemeCommandService, type MemeMessage } from "../src/memes/service.js";

const botId = "1234567890123456789";
const targetId = "2234567890123456789";
const sourceId = "3234567890123456789";
const catalog = {
  enabled: true, private_enabled: true, cooldown_seconds: 5,
  scopes: [{ kind: "discord_channel", target_id: sourceId, guild_id: "4234567890123456789" }],
  items: [{ id: "11111111-1111-4111-8111-111111111111", name: "摸摸", keyword: "摸摸",
    aliases: ["petpet"], random_enabled: true, requirements: { avatars: 1, images: 0, texts: 0 } }],
};

function message(): MemeMessage {
  return {
    id: "5234567890123456789", guildId: "4234567890123456789", channelId: sourceId,
    content: `摸摸 <@${targetId}>`, author: { id: "6234567890123456789", bot: false,
      displayAvatarURL: () => "https://cdn.discordapp.com/avatars/a/avatar.png" },
    webhookId: null, client: { user: { id: botId } },
    mentions: { users: new Map([[targetId, { displayAvatarURL: () => "https://cdn.discordapp.com/avatars/b/avatar.png" }]]) },
    attachments: new Map(), reference: null,
    fetchReference: vi.fn(),
  };
}

describe("meme commands", () => {
  it("matches only the beginning and keeps true user mentions in source order", () => {
    expect(parseMemeCommand(`今天想摸摸 <@${targetId}>`, botId, catalog)).toBeNull();
    expect(parseMemeCommand(`<@${botId}> 摸摸 <@${targetId}>`, botId, catalog)).toMatchObject({
      keyword: "摸摸", mentionIds: [targetId], texts: [],
    });
    expect(parseMemeCommand("随机表情 hello | world", botId, catalog)).toMatchObject({
      keyword: "随机表情", texts: ["hello", "world"],
    });
  });

  it("claims the original message and asks for send permission before replying", async () => {
    const operations: string[] = [];
    const request = vi.fn(async (path: string) => {
      operations.push(path.split("/").at(-1)!);
      if (path.endsWith("catalog")) return Response.json(catalog);
      if (path.endsWith("claim")) return Response.json({ receipt_id: "11111111-1111-4111-8111-111111111112" });
      if (path.endsWith("render")) return new Response(new Uint8Array([71, 73, 70]), { headers: { "content-type": "image/gif" } });
      if (path.endsWith("send")) return Response.json({ allowed: true });
      return new Response(null, { status: 204 });
    });
    const send = vi.fn(async () => { operations.push("reply"); });
    const media = vi.fn(async () => Buffer.from("avatar"));
    const service = new MemeCommandService({ request, send, media });
    await service.refresh();
    await service.handle(message());
    expect(operations).toEqual(["catalog", "claim", "render", "send", "reply", "result"]);
    expect(send).toHaveBeenCalledOnce();
    service.stop();
  });

  it("does not fetch avatars or send when the event is duplicate, out of scope or from a bot", async () => {
    const request = vi.fn(async (path: string) => path.endsWith("catalog")
      ? Response.json(catalog) : Response.json({ receipt_id: null }));
    const send = vi.fn();
    const media = vi.fn();
    const service = new MemeCommandService({ request, send, media });
    await service.refresh();
    await service.handle({ ...message(), author: { ...message().author, bot: true } });
    await service.handle({ ...message(), channelId: "9999999999999999999" });
    await service.handle(message());
    expect(media).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
    service.stop();
  });

  it("stopping during an image fetch prevents any later render or reply", async () => {
    let complete!: (body: Buffer) => void;
    const request = vi.fn(async (path: string) => path.endsWith("catalog") ? Response.json(catalog)
      : Response.json({ receipt_id: "11111111-1111-4111-8111-111111111112" }));
    const media = vi.fn(() => new Promise<Buffer>(resolve => { complete = resolve; }));
    const send = vi.fn();
    const service = new MemeCommandService({ request, send, media });
    await service.refresh();
    const handling = service.handle(message());
    await vi.waitFor(() => expect(media).toHaveBeenCalled());
    service.stop();
    complete(Buffer.from("avatar"));
    await handling;
    expect(request.mock.calls.some(([path]) => path.endsWith("render"))).toBe(false);
    expect(send).not.toHaveBeenCalled();
  });

  it("bounds concurrent media generation and sends busy errors through the same permission gate", async () => {
    const request = vi.fn(async (path: string) => {
      if (path.endsWith("catalog")) return Response.json(catalog);
      if (path.endsWith("claim")) return Response.json({ receipt_id: "11111111-1111-4111-8111-111111111112" });
      if (path.endsWith("send")) return Response.json({ allowed: false });
      return new Response(null, { status: 204 });
    });
    const complete: Array<(value: Buffer) => void> = [];
    const media = vi.fn(() => new Promise<Buffer>(resolve => complete.push(resolve)));
    const send = vi.fn();
    const service = new MemeCommandService({ request, media, send });
    await service.refresh();
    const first = service.handle(message());
    const second = service.handle({ ...message(), id: "5234567890123456790", author: { ...message().author, id: "6234567890123456790" } });
    await vi.waitFor(() => expect(media).toHaveBeenCalledTimes(2));
    const third = service.handle({ ...message(), id: "5234567890123456791", author: { ...message().author, id: "6234567890123456791" } });
    await vi.waitFor(() => expect(request.mock.calls.some(([path]) => path.endsWith("send"))).toBe(true));
    expect(media).toHaveBeenCalledTimes(2);
    expect(send).not.toHaveBeenCalled();
    service.stop();
    complete.forEach(resolve => resolve(Buffer.from("avatar")));
    await Promise.all([first, second, third]);
  });
});
