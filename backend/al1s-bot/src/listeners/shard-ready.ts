import { Listener } from "@sapphire/framework";
import { Events } from "discord.js";

import { getRuntime } from "../runtime.js";

export class ShardReadyListener extends Listener<typeof Events.ShardReady> {
  public constructor(context: Listener.LoaderContext) {
    super(context, { event: Events.ShardReady });
  }

  public run(shardId: number, unavailableGuilds: Set<string> | undefined): void {
    const runtime = getRuntime();
    runtime.health.setDiscordReady(true);
    runtime.logger.info("discord_shard_ready", {
      shardId,
      unavailableGuildCount: unavailableGuilds?.size ?? 0
    });
  }
}
