import { readFile } from "node:fs";
import { join } from "node:path";

import { PlatformConfigProvider } from "./platform-config-provider.js";

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

export function configuredProvider(env: NodeJS.ProcessEnv = process.env): PlatformConfigProvider {
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
  throw new Error("platform_control_base_url_required");
}
