export interface NormalizedDiscordMessage {
  guildName: string;
  channelName: string;
  displayName: string;
  username: string;
  content: string;
  attachmentUrls: readonly string[];
  messageUrl: string;
}

function normalizeText(value: string): string {
  return value.replace(/\0/g, "").replace(/\r\n?/g, "\n").trim();
}

export function formatDiscordMessageForQq(
  message: NormalizedDiscordMessage,
  prefix = "[Discord]"
): string {
  const header = `${normalizeText(prefix)}｜${normalizeText(message.guildName)}／#${normalizeText(message.channelName)}`;
  const author = `${normalizeText(message.displayName)} (@${normalizeText(message.username)})`;
  const content = normalizeText(message.content) || "（无文本内容）";
  const lines = [`${header}\n${author}：${content}`];

  if (message.attachmentUrls.length > 0) {
    lines.push(`附件：\n${message.attachmentUrls.map(normalizeText).join("\n")}`);
  }
  lines.push(`原消息：${normalizeText(message.messageUrl)}`);
  return lines.join("\n");
}
