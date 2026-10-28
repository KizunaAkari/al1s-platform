import { REST } from "discord.js";
import { MockAgent } from "undici";
import { expect, it, vi } from "vitest";
import { readMemeAvatarURL } from "../src/memes/avatars.js";

const guild = "4234567890123456789", user = "2234567890123456789";
const globalURL = "https://cdn.discordapp.com/avatars/user/global.png";
it("uses a scoped member GET and the SDK guild-avatar PNG endpoint without sending messages", async () => {
  const agent = new MockAgent(); agent.disableNetConnect();
  agent.get("https://discord.com").intercept({ method: "GET", path: `/api/v10/guilds/${guild}/members/${user}` })
    .reply(200, { avatar: "b".repeat(32) }, { headers: { "content-type": "application/json" } });
  const rest = new REST({ retries: 0, timeout: 5000, agent }).setToken("test-token");
  try {
    const result = await readMemeAvatarURL(rest, guild, user, globalURL, new AbortController().signal);
    expect(result).toBe(`https://cdn.discordapp.com/guilds/${guild}/users/${user}/avatars/${"b".repeat(32)}.png?size=256`);
    agent.assertNoPendingInterceptors();
  } finally { rest.clearHashSweeper(); rest.clearHandlerSweeper(); await agent.close(); }
});
it("keeps account avatars in DMs without any member request", async () => {
  const rest = new REST(), get = vi.spyOn(rest, "get");
  try {
    expect(await readMemeAvatarURL(rest, null, user, globalURL, new AbortController().signal)).toBe(globalURL);
    expect(get).not.toHaveBeenCalled();
  } finally { rest.clearHashSweeper(); rest.clearHandlerSweeper(); }
});
it.each([null, undefined])("uses the account avatar when no independent server avatar exists (%s)", async avatar => {
  const rest = new REST(); vi.spyOn(rest, "get").mockResolvedValue({ avatar });
  try {
    expect(await readMemeAvatarURL(rest, guild, user, globalURL, new AbortController().signal)).toBe(globalURL);
  } finally { rest.clearHashSweeper(); rest.clearHandlerSweeper(); }
});
it("rejects unexpected member errors instead of silently substituting an account avatar", async () => {
  const rest = new REST(); vi.spyOn(rest, "get").mockRejectedValue(new Error("member-unavailable"));
  try {
    await expect(readMemeAvatarURL(rest, guild, user, globalURL, new AbortController().signal)).rejects.toThrow("member-unavailable");
  } finally { rest.clearHashSweeper(); rest.clearHandlerSweeper(); }
});
it("propagates cancellation to the request and rejects a late member reply", async () => {
  const rest = new REST(), controller = new AbortController();
  let complete!: (value: unknown) => void;
  const get = vi.spyOn(rest, "get").mockReturnValue(new Promise(resolve => { complete = resolve; }));
  try {
    const pending = readMemeAvatarURL(rest, guild, user, globalURL, controller.signal);
    const rejection = expect(pending).rejects.toThrow();
    controller.abort(); complete({ avatar: "b".repeat(32) });
    await rejection;
    expect(get.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
  } finally { rest.clearHashSweeper(); rest.clearHandlerSweeper(); }
});
