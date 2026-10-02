import "dotenv/config";

import { z } from "zod";

const schema = z.object({
  DISCORD_TOKEN: z.string().trim().min(30, "DISCORD_TOKEN is missing or too short"),
  DISCORD_APPLICATION_ID: z.string().trim().regex(/^\d{17,20}$/),
  FORWARD_PREFIX: z.string().default("[Discord]"),
  DEDUPE_TTL_SECONDS: z.coerce.number().int().min(60).max(604_800).default(86_400),
  LOG_LEVEL: z.enum(["debug", "info", "warn", "error"]).default("info"),
  HEALTH_HOST: z.string().trim().min(1).default("0.0.0.0"),
  HEALTH_PORT: z.coerce.number().int().min(1).max(65_535).default(3_100),
});

export type LogLevel = "debug" | "info" | "warn" | "error";

export interface AppConfig {
  discord: { token: string; applicationId: string };
  forward: { prefix: string; dedupeTtlMs: number };
  health: { host: string; port: number };
  logLevel: LogLevel;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const result = schema.safeParse(env);
  if (!result.success) {
    const details = result.error.issues
      .map(issue => `${issue.path.join(".") || "environment"}: ${issue.message}`)
      .join("; ");
    throw new Error(`Invalid configuration: ${details}`);
  }
  const raw = result.data;
  return {
    discord: { token: raw.DISCORD_TOKEN, applicationId: raw.DISCORD_APPLICATION_ID },
    forward: { prefix: raw.FORWARD_PREFIX, dedupeTtlMs: raw.DEDUPE_TTL_SECONDS * 1_000 },
    health: { host: raw.HEALTH_HOST, port: raw.HEALTH_PORT },
    logLevel: raw.LOG_LEVEL,
  };
}
