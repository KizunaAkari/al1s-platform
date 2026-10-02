import { Listener } from "@sapphire/framework";
import { Events, type Client } from "discord.js";

import { getRuntime } from "../runtime.js";

export class ReadyListener extends Listener<typeof Events.ClientReady> {
  public constructor(context: Listener.LoaderContext) {
    super(context, { event: Events.ClientReady, once: true });
  }

  public run(client: Client<true>): void {
    const runtime = getRuntime();
    runtime.health.setDiscordReady(true, client.ws.ping);
    runtime.logger.info("discord_ready", {
      botUserId: client.user.id,
      username: client.user.username,
      guildCount: client.guilds.cache.size,
      gatewayPingMs: client.ws.ping
    });
  }
}
