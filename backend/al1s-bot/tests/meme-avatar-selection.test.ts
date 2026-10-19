import { describe, expect, it, vi } from "vitest";
import { MemeCommandService, type MemeMessage } from "../src/memes/service.js";

const guildId = "4234567890123456789", channelId = "3234567890123456789";
const targetId = "2234567890123456789", authorId = "6234567890123456789";
const globalURL = "https://cdn.discordapp.com/avatars/user/global.png";
const authorURL = "https://cdn.discordapp.com/avatars/author/global.png";
const guildURL = "https://cdn.discordapp.com/guilds/guild/users/user/avatars/local.png";
function message(mentions = true): MemeMessage {
  return { id: "5234567890123456789", guildId, channelId, content: mentions ? `摸摸 <@${targetId}>` : "摸摸",
    author: { id: authorId, bot: false, displayAvatarURL: () => authorURL }, webhookId: null,
    client: { user: { id: "1234567890123456789" } },
    mentions: { users: new Map([[targetId, { displayAvatarURL: () => globalURL }]]) },
    attachments: new Map(), reference: null, fetchReference: vi.fn() };
}
function request(path: string): Promise<Response> {
  if (path.endsWith("catalog")) return Promise.resolve(Response.json({ enabled: true, private_enabled: true,
    cooldown_seconds: 5, scopes: [{ kind: "discord_channel", target_id: channelId, guild_id: guildId }],
    items: [{ id: "11111111-1111-4111-8111-111111111111", name: "摸摸", keyword: "摸摸", aliases: [],
      random_enabled: true, requirements: { avatars: 1, images: 0, texts: 0 } }] }));
  if (path.endsWith("claim")) return Promise.resolve(Response.json({ receipt_id: "11111111-1111-4111-8111-111111111112" }));
  if (path.endsWith("render")) return Promise.resolve(new Response(new Uint8Array([71,73,70]), { headers: { "content-type": "image/gif" } }));
  return Promise.resolve(Response.json({ allowed: true }));
}
describe("guild avatar selection", () => {
  it.each([true, false])("resolves the current guild avatar for mentioned users and the sender (%s)", async mentions => {
    const avatarURL = vi.fn(async () => guildURL), media = vi.fn(async () => Buffer.from("avatar"));
    const service = new MemeCommandService({ request, avatarURL, media, send: vi.fn() });
    await service.refresh(); await service.handle(message(mentions));
    expect(avatarURL).toHaveBeenCalledWith(guildId, mentions ? targetId : authorId, mentions ? globalURL : authorURL, expect.any(AbortSignal));
    expect(media).toHaveBeenCalledWith(guildURL, expect.any(AbortSignal));
    service.stop();
  });
  it("does not fetch images or render after stopping during a member lookup", async () => {
    let complete!: (url: string) => void;
    const avatarURL = vi.fn(() => new Promise<string>(resolve => { complete = resolve; }));
    const media = vi.fn(), api = vi.fn(request), send = vi.fn();
    const service = new MemeCommandService({ request: api, avatarURL, media, send });
    await service.refresh(); const pending = service.handle(message());
    await vi.waitFor(() => expect(avatarURL).toHaveBeenCalledOnce());
    service.stop(); complete(guildURL); await pending;
    expect(media).not.toHaveBeenCalled(); expect(send).not.toHaveBeenCalled();
    expect(api.mock.calls.some(([path]) => path.endsWith("render"))).toBe(false);
  });
  it("keeps mention order while resolving a repeated user only once per command", async () => {
    const other = "7234567890123456789";
    const input = message(); input.content = `摸摸 <@${other}> <@${targetId}> <@${other}>`;
    input.mentions.users = new Map([[targetId, { displayAvatarURL: () => globalURL }], [other, { displayAvatarURL: () => globalURL }]]);
    const avatarURL = vi.fn(async (_guild: string | null, id: string) => `${guildURL}?user=${id}`);
    const media = vi.fn(async () => Buffer.from("avatar"));
    const service = new MemeCommandService({ request, avatarURL, media, send: vi.fn() });
    await service.refresh(); await service.handle(input);
    expect(avatarURL).toHaveBeenCalledTimes(2);
    expect(media.mock.calls.map(([url]) => url)).toEqual([`${guildURL}?user=${other}`, `${guildURL}?user=${targetId}`, `${guildURL}?user=${other}`]);
    service.stop();
  });
});
