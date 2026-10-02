import { Listener } from "@sapphire/framework";
import { Events } from "discord.js";

import { getRuntime } from "../runtime.js";

export class ShardDisconnectListener extends Listener<typeof Events.ShardDisconnect> {
  public constructor(context: Listener.LoaderContext) {
    super(context, { event: Events.ShardDisconnect });
  }

  public run(event: { code?: number }, shardId: number): void {
    const runtime = getRuntime();
    runtime.health.setDiscordReady(false);
    runtime.logger.warn("discord_shard_disconnected", {
      shardId,
      closeCode: event.code ?? null
    });
  }
}
