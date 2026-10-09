import { z } from "zod";

export const catalogSchema = z.object({
  enabled: z.boolean(), private_enabled: z.boolean(), cooldown_seconds: z.number().int().min(1).max(60),
  scopes: z.array(z.object({
    kind: z.enum(["qq_group", "discord_channel"]), target_id: z.string(),
    guild_id: z.string().nullable().optional(),
  })).max(100),
  items: z.array(z.object({
    id: z.string().uuid(), name: z.string().max(64), keyword: z.string().max(32),
    aliases: z.array(z.string().max(32)).max(8), random_enabled: z.boolean(),
    requirements: z.object({ avatars: z.number().int().min(0).max(4), images: z.number().int().min(0).max(1),
      texts: z.number().int().min(0).max(4) }),
  })).max(500),
});
export type MemeCatalog = z.infer<typeof catalogSchema>;
export type MemeResult = { content?: string; body?: Buffer; mediaType?: "image/png" | "image/gif" };
export const emptyCatalog: MemeCatalog = {
  enabled: false, private_enabled: false, cooldown_seconds: 5, scopes: [], items: [],
};
