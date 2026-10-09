import { REST, Routes } from "discord.js";
import type { Dispatcher } from "undici";
import type { MemeMessage } from "./service.js";
import type { MemeResult } from "./contracts.js";

export function createMemeSender(token: string, agent?: Dispatcher | null) {
  const rest = new REST({ retries: 0, timeout: 5000, ...(agent ? { agent } : {}) }).setToken(token);
  return {
    async send(message: MemeMessage, result: MemeResult, signal: AbortSignal): Promise<void> {
      await rest.post(Routes.channelMessages(message.channelId), {
        signal,
        body: { ...(result.content ? { content: result.content } : {}),
          allowed_mentions: { parse: [], replied_user: false },
          nonce: message.id, enforce_nonce: true,
          message_reference: { message_id: message.id, fail_if_not_exists: false } },
        ...(result.body ? { files: [{ data: result.body, name: result.mediaType === "image/gif" ? "meme.gif" : "meme.png" }] } : {}),
      });
    },
    close(): void { rest.clearHashSweeper(); rest.clearHandlerSweeper(); },
  };
}
