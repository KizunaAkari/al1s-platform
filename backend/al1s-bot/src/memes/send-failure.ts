export function memeSendFailureCode(error: unknown): string {
  if (!error || typeof error !== "object") return "meme_reply_result_unknown";
  const value = error as { name?: unknown; code?: unknown; status?: unknown; cause?: unknown };
  const cause = value.cause && typeof value.cause === "object"
    ? value.cause as { name?: unknown; code?: unknown } : {};
  const names = [value.name, cause.name];
  const codes = [value.code, cause.code];
  if (names.some(name => name === "AbortError" || name === "TimeoutError")
    || codes.some(code => ["UND_ERR_CONNECT_TIMEOUT", "UND_ERR_HEADERS_TIMEOUT",
      "UND_ERR_BODY_TIMEOUT", "UND_ERR_ABORTED", "ETIMEDOUT"].includes(String(code)))) {
    return "meme_reply_timeout";
  }
  if (typeof value.code === "number" && Number.isSafeInteger(value.code)
    && value.code > 0 && value.code < 1_000_000) return "meme_reply_discord_" + value.code;
  if (typeof value.status === "number" && Number.isInteger(value.status)
    && value.status >= 400 && value.status <= 599) return "meme_reply_http_" + value.status;
  return "meme_reply_result_unknown";
}
