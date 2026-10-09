import type { Dispatcher } from "undici";

const MAX_BYTES = 8 * 1024 * 1024;
const HOSTS = new Set(["cdn.discordapp.com", "media.discordapp.net"]);

export async function readMemeMedia(url: string, signal: AbortSignal, agent?: Dispatcher | null): Promise<Buffer> {
  const address = new URL(url);
  if (address.protocol !== "https:" || !HOSTS.has(address.hostname) || address.username || address.password) {
    throw new Error("meme_media_source_rejected");
  }
  const options: RequestInit & { dispatcher?: Dispatcher } = {
    signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]), redirect: "error",
    ...(agent ? { dispatcher: agent } : {}),
  };
  const response = await fetch(address, options);
  if (!response.ok || !response.body || Number(response.headers.get("content-length")) > MAX_BYTES) {
    throw new Error("meme_media_unavailable");
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      length += chunk.value.byteLength;
      if (length > MAX_BYTES) throw new Error("meme_media_too_large");
      chunks.push(chunk.value);
    }
  } finally { await reader.cancel().catch(() => undefined); }
  if (!length) throw new Error("meme_media_empty");
  return Buffer.concat(chunks, length);
}
