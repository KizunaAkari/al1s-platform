import { describe, expect, it } from "vitest";

import { formatDiscordMessageForQq } from "../src/bridge/formatter.js";

describe("formatDiscordMessageForQq", () => {
  it("includes source context, attachments, and a stable message link", () => {
    const output = formatDiscordMessageForQq({
      guildName: "测试服务器",
      channelName: "通知",
      displayName: "結月緣",
      username: "kizunaakari",
      content: "你好",
      attachmentUrls: ["https://cdn.discordapp.com/example.png"],
      messageUrl: "https://discord.com/channels/1/2/3"
    });

    expect(output).toContain("[Discord]｜测试服务器／#通知");
    expect(output).toContain("結月緣 (@kizunaakari)：你好");
    expect(output).toContain("https://cdn.discordapp.com/example.png");
    expect(output).toContain("https://discord.com/channels/1/2/3");
  });

  it("removes null bytes and normalizes empty content", () => {
    const output = formatDiscordMessageForQq({
      guildName: "g",
      channelName: "c",
      displayName: "d",
      username: "u",
      content: "\0",
      attachmentUrls: [],
      messageUrl: "https://discord.com/channels/1/2/3"
    });
    expect(output).toContain("（无文本内容）");
    expect(output).not.toContain("\0");
  });
});
