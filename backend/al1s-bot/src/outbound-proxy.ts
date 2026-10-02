import { bootstrap } from "global-agent";
import { ProxyAgent } from "undici";

const INTERNAL = "localhost,127.0.0.1,backend,al1s-platform-app,al1s-llbot";

/** Discord REST uses Undici; its WebSocket uses the Node HTTP agents. */
export function outboundProxy(env: NodeJS.ProcessEnv = process.env): { rest?: { agent: ProxyAgent } } {
  const value = env.AL1S_BOT_HTTP_PROXY?.trim();
  if (!value) return {};
  let proxy: URL;
  try { proxy = new URL(value); }
  catch { throw new Error("invalid_bot_proxy"); }
  if (!["http:", "https:"].includes(proxy.protocol) || !proxy.hostname || proxy.username
    || proxy.password || proxy.search || proxy.hash || proxy.pathname !== "/") {
    throw new Error("invalid_bot_proxy");
  }
  process.env.AL1S_BOT_HTTP_PROXY = proxy.toString();
  process.env.AL1S_BOT_HTTPS_PROXY = proxy.toString();
  process.env.AL1S_BOT_NO_PROXY = env.AL1S_BOT_NO_PROXY?.trim() || INTERNAL;
  bootstrap({ environmentVariableNamespace: "AL1S_BOT_" });
  return { rest: { agent: new ProxyAgent(proxy.toString()) } };
}
