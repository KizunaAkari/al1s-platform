import { Listener } from "@sapphire/framework";
import { Events } from "discord.js";

import { getRuntime } from "../runtime.js";

export class ClientErrorListener extends Listener<typeof Events.Error> {
  public constructor(context: Listener.LoaderContext) {
    super(context, { event: Events.Error });
  }

  public run(error: Error): void {
    const runtime = getRuntime();
    runtime.health.recordError(error);
    runtime.logger.error("discord_client_error", { error });
  }
}
