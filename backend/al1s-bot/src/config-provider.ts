import { readFile, watchFile, unwatchFile } from "node:fs";
import { join } from "node:path";

import { loadConfig, type AppConfig } from "./config.js";
import { PlatformConfigProvider } from "./platform-config-provider.js";
import type { ForwardRule } from "./bridge/forward-rule-matcher.js";
import type { RuleForwardMessage } from "./platform-ingress.js";

export interface ConfigProvider {
  loadForwardRules?(signal?: AbortSignal): Promise<ForwardRule[]>;
  submitMessage?(message: RuleForwardMessage): Promise<void>;
  startIngress?(): () => void;
  watchForwardRules?(onChange: (rules: ForwardRule[]) => void): () => void;
  load(): Promise<AppConfig>;
  watch?(onChange: () => void): () => void;
  markApplied?(): Promise<void>;
  markRejected?(): Promise<void>;
  close?(): Promise<void>;
  reportHealth?(snapshot: { status: "healthy" | "degraded" | "unhealthy"; diagnostics: Record<string, unknown> }): Promise<void>;
  startHeartbeat?(
    snapshot: () => { status: "healthy" | "degraded" | "unhealthy"; diagnostics: Record<string, unknown> }
  ): () => void;
}

export class EnvironmentConfigProvider implements ConfigProvider {
  public async load(): Promise<AppConfig> {
    return loadConfig();
  }
}

export class JsonFileConfigProvider implements ConfigProvider {
  public constructor(private readonly path: string) {}

  public async load(): Promise<AppConfig> {
    return loadConfig(await runtimeEnvironment({ ...process.env, BOT_RUNTIME_CONFIG_PATH: this.path }));
  }

  public watch(onChange: () => void): () => void {
    const listener = (): void => onChange();
    watchFile(this.path, { interval: 2_000, persistent: false }, listener);
    return () => unwatchFile(this.path, listener);
  }
}

export async function runtimeEnvironment(env: NodeJS.ProcessEnv = process.env): Promise<NodeJS.ProcessEnv> {
  const path = env.BOT_RUNTIME_CONFIG_PATH?.trim();
  if (!path) {
    return { ...env };
  }
  const content = await new Promise<string>((resolve, reject) => {
    readFile(path, "utf8", (error, value) => error ? reject(error) : resolve(value));
  });
  const parsed: unknown = JSON.parse(content);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("AL-1S bot runtime configuration must be a JSON object");
  }
  const environment: NodeJS.ProcessEnv = { ...env };
  for (const [key, value] of Object.entries(parsed)) {
    if (typeof value !== "string") {
      throw new Error(`AL-1S bot runtime configuration value ${key} must be a string`);
    }
    environment[key] = value;
  }
  return environment;
}

export function configuredProvider(env: NodeJS.ProcessEnv = process.env): ConfigProvider {
  const baseUrl = env.AL1S_CONTROL_BASE_URL?.trim();
  if (baseUrl) {
    const stateDirectory = env.AL1S_BOT_STATE_DIR?.trim() || "/var/lib/al1s-plachta";
    return new PlatformConfigProvider({
      baseUrl,
      credentialPath: env.AL1S_BOT_CREDENTIAL_PATH?.trim() || join(stateDirectory, "credential"),
      stateDirectory,
      ...(env.AL1S_BOT_REGISTRATION_CODE_PATH?.trim()
        ? { registrationCodePath: env.AL1S_BOT_REGISTRATION_CODE_PATH.trim() }
        : {}),
      environment: env
    });
  }
  const path = env.BOT_RUNTIME_CONFIG_PATH?.trim();
  return path ? new JsonFileConfigProvider(path) : new EnvironmentConfigProvider();
}
