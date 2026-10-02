import { describe, expect, it } from "vitest";
import { loadConfig } from "../src/config.js";

const identity = {
  DISCORD_TOKEN: "test-discord-token-that-is-long-enough-for-validation",
  DISCORD_APPLICATION_ID: "1439231483326894160",
};

describe("Discord worker identity", () => {
  it("starts without old mention or OneBot routing fields", () => {
    const config = loadConfig(identity);
    expect(config.discord.applicationId).toBe(identity.DISCORD_APPLICATION_ID);
    expect(config.forward.prefix).toBe("[Discord]");
    expect(config).not.toHaveProperty("oneBot");
    expect(config.discord).not.toHaveProperty("ownerUserIds");
  });
  it("ignores historical routing settings and validates only the current identity", () => {
    const config = loadConfig({ ...identity, DISCORD_OWNER_USER_IDS: "invalid",
      DISCORD_GROUP_CHANNEL_IDS: "invalid", QQ_TARGET_USER_ID: "invalid" });
    expect(config.discord.applicationId).toBe(identity.DISCORD_APPLICATION_ID);
    expect(() => loadConfig({ ...identity, DISCORD_TOKEN: "" })).toThrow("DISCORD_TOKEN is missing or too short");
  });
});
