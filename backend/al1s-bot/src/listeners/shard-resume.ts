import { Listener } from "@sapphire/framework";
import { Events } from "discord.js";

import { getRuntime } from "../runtime.js";

export class ShardResumeListener extends Listener<typeof Events.ShardResume> {
  public constructor(context: Listener.LoaderContext) {
    super(context, { event: Events.ShardResume });
  }

  public run(shardId: number, replayedEvents: number): void {
    const runtime = getRuntime();
    runtime.health.setDiscordReady(true);
    runtime.logger.info("discord_shard_resumed", { shardId, replayedEvents });
  }
}
