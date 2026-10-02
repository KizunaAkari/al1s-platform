import { SapphireClient } from "@sapphire/framework";
import { GatewayIntentBits } from "discord.js";

import { MessageDeduplicator } from "./bridge/dedupe.js";
import { DiscordRuleBridge } from "./bridge/service.js";
import { configuredProvider } from "./config-provider.js";
import { HealthServer } from "./health/server.js";
import { HealthState } from "./health/state.js";
import { Logger } from "./logger.js";
import { setRuntime } from "./runtime.js";
import { outboundProxy } from "./outbound-proxy.js";

async function main(): Promise<void> {
  const provider = configuredProvider();
  const config = await provider.load();
  const logger = new Logger(config.logLevel);
  const health = new HealthState();
  if (!provider.submitMessage || !provider.startIngress) throw new Error("platform_ingress_required");
  const deduplicator = new MessageDeduplicator(config.forward.dedupeTtlMs);
  const bridge = new DiscordRuleBridge(
    config, deduplicator, health, logger, message => provider.submitMessage!(message),
  );
  let stopIngress: (() => void) | undefined;
  const healthServer = new HealthServer(config.health.host, config.health.port, health, logger);
  setRuntime({ bridge, health, logger });

  const client = new SapphireClient({
    ...outboundProxy(),
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMessages,
      GatewayIntentBits.MessageContent
    ],
    loadMessageCommandListeners: false
  });

  let stopping = false;
  const startupAbort = new AbortController();
  let stopConfigWatch: (() => void) | undefined;
  let stopForwardRules: (() => void) | undefined;
  let stopHeartbeat: (() => void) | undefined;
  const closeClient = async (): Promise<void> => {
    await client.destroy().catch((error: unknown) => {
      logger.error("discord_client_destroy_failed", { error });
    });
  };
  const shutdown = async (signal: string, exitCode = 0): Promise<void> => {
    if (stopping) {
      return;
    }
    stopping = true;
    startupAbort.abort();
    health.setShuttingDown();
    logger.info("worker_stopping", { signal });
    stopConfigWatch?.();
    stopForwardRules?.();
    stopIngress?.();
    stopHeartbeat?.();
    await closeClient();
    if (provider.close) {
      await provider.close().catch((error: unknown) => {
        logger.error("ingress_close_failed", { error });
      });
    }
    await healthServer.stop().catch((error: unknown) => {
      logger.error("health_server_stop_failed", { error });
    });
    process.exitCode = exitCode;
  };

  stopConfigWatch = provider.watch?.(() => {
    if (stopping) return;
    logger.info("runtime_config_changed", { source: "al1s-control-center" });
    void shutdown("runtime_config_changed").finally(() => process.exit(75));
  });

  process.once("SIGINT", () => void shutdown("SIGINT"));
  process.once("SIGTERM", () => void shutdown("SIGTERM"));
  process.on("unhandledRejection", (error) => {
    health.recordError(error);
    logger.error("unhandled_rejection", { error });
  });
  process.once("uncaughtException", (error) => {
    health.recordError(error);
    logger.error("uncaught_exception", { error });
    void shutdown("uncaughtException", 1);
  });

  await healthServer.start();
  if (stopping) {
    await healthServer.stop();
    return;
  }
  stopHeartbeat = provider.startHeartbeat?.(() => health.platformSnapshot());
  try {
    if (!provider.loadForwardRules) throw new Error("platform_rule_provider_required");
    const rules = await provider.loadForwardRules(startupAbort.signal);
    if (stopping) return;
    bridge.setRules(rules);
  } catch (error) {
    if (stopping) return;
    health.recordError(error);
    await provider.reportHealth?.(health.platformSnapshot()).catch((reportError: unknown) => {
      logger.error("initial_rules_health_report_failed", { error: reportError });
    });
    if (stopping) return;
    logger.error("initial_forward_rules_failed", { error });
    await shutdown("rules_not_ready", 1);
    return;
  }

  try {
    logger.info("discord_login_started", {
      applicationId: config.discord.applicationId,
    });
    await client.login(config.discord.token);
    if (stopping) {
      await closeClient();
      return;
    }
    await provider.markApplied?.();
    if (stopping) return;
    stopForwardRules = provider.watchForwardRules?.(rules => {
      if (!stopping) bridge.setRules(rules);
    });
    stopIngress = provider.startIngress();
  } catch (error) {
    if (stopping) {
      await closeClient();
      return;
    }
    await provider.markRejected?.();
    if (stopping) return;
    health.recordLoginError(error);
    await provider.reportHealth?.(health.platformSnapshot());
    if (stopping) return;
    logger.error("discord_login_failed", { error });
    await shutdown("login_failed", 1);
  }
}

await main().catch((error: unknown) => {
  console.error(JSON.stringify({
    timestamp: new Date().toISOString(),
    level: "error",
    event: "worker_start_failed",
    error: error instanceof Error ? error.message : String(error)
  }));
  process.exitCode = 1;
});
