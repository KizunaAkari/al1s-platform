import { expect, it } from "vitest";
import { memeSendFailureCode } from "../src/memes/send-failure.js";

it("records only fixed send failure categories and numeric Discord codes", () => {
  expect(memeSendFailureCode({ name: "AbortError", message: "private signed url" })).toBe("meme_reply_timeout");
  expect(memeSendFailureCode({ code: 50013, status: 403, requestBody: "private body" })).toBe("meme_reply_discord_50013");
  expect(memeSendFailureCode({ cause: { code: "UND_ERR_CONNECT_TIMEOUT" } })).toBe("meme_reply_timeout");
  expect(memeSendFailureCode({ code: "private-token", message: "private body" })).toBe("meme_reply_result_unknown");
});
