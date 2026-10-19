import { DiscordAPIError, Routes, type REST } from "discord.js";
import { z } from "zod";

const memberSchema = z.object({ avatar: z.string().nullable().optional() });
export async function readMemeAvatarURL(
  rest: Pick<REST, "get" | "cdn">, guildId: string | null, userId: string,
  globalURL: string, signal: AbortSignal,
): Promise<string> {
  signal.throwIfAborted();
  if (guildId === null) return globalURL;
  let member;
  try {
    member = memberSchema.parse(await rest.get(Routes.guildMember(guildId, userId), {
      signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]),
    }));
  } catch (error) {
    signal.throwIfAborted();
    if (error instanceof DiscordAPIError && error.code === 10007) return globalURL;
    throw error;
  }
  signal.throwIfAborted();
  return member.avatar ? rest.cdn.guildMemberAvatar(guildId, userId, member.avatar, {
    forceStatic: true, extension: "png", size: 256,
  }) : globalURL;
}
