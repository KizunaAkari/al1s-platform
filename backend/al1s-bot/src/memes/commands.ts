import type { MemeCatalog } from "./contracts.js";

export interface ParsedCommand { keyword: string; mentionIds: string[]; texts: string[] }

export function parseMemeCommand(content: string, selfId: string, catalog: MemeCatalog): ParsedCommand | null {
  let value = content.trim();
  if (/^\d+$/.test(selfId)) value = value.replace(new RegExp(`^<@!?${selfId}>\\s*`), "");
  const words = value.split(/\s+/);
  const first = words.shift()?.normalize("NFKC").toLowerCase() ?? "";
  let keyword = first;
  if (first === "头像合成") keyword = words.shift()?.normalize("NFKC").toLowerCase() ?? "";
  else if (!["随机表情", "表情列表"].includes(first)
    && !catalog.items.some(item => item.keyword === first || item.aliases.includes(first))) return null;
  if (!keyword || keyword.length > 32) return null;
  const tail = words.join(" ");
  const mentionIds = [...tail.matchAll(/<@!?(\d+)>/g)].map(match => match[1]!);
  const remaining = tail.replace(/<@!?\d+>/g, "").trim();
  return { keyword, mentionIds, texts: remaining
    ? remaining.split("|").map(text => text.trim()).filter(Boolean) : [] };
}
