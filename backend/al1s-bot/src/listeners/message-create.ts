import { Listener } from "@sapphire/framework";
import { Events, type Message } from "discord.js";

import { getRuntime } from "../runtime.js";

export class MessageCreateListener extends Listener<typeof Events.MessageCreate> {
  public constructor(context: Listener.LoaderContext) {
    super(context, { event: Events.MessageCreate });
  }

  public async run(message: Message): Promise<void> {
    await getRuntime().bridge.handle(message);
  }
}
