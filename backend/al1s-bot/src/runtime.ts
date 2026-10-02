import type { DiscordRuleBridge } from "./bridge/service.js";
import type { HealthState } from "./health/state.js";
import type { Logger } from "./logger.js";

export interface RuntimeServices {
  bridge: DiscordRuleBridge;
  health: HealthState;
  logger: Logger;
}

let currentRuntime: RuntimeServices | null = null;

export function setRuntime(runtime: RuntimeServices): void {
  currentRuntime = runtime;
}

export function getRuntime(): RuntimeServices {
  if (currentRuntime === null) {
    throw new Error("Runtime services are not initialized");
  }
  return currentRuntime;
}
