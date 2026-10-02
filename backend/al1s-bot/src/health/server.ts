import { createServer, type Server } from "node:http";

import type { Logger } from "../logger.js";
import type { HealthState } from "./state.js";

export class HealthServer {
  private server: Server | null = null;

  public constructor(
    private readonly host: string,
    private readonly port: number,
    private readonly state: HealthState,
    private readonly logger: Logger
  ) {}

  public async start(): Promise<void> {
    if (this.server !== null) {
      return;
    }
    const server = createServer((request, response) => {
      const readyProbe = request.url === "/ready";
      if (request.url !== "/health" && !readyProbe) {
        response.writeHead(404, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ error: "not_found" }));
        return;
      }

      const statusCode = readyProbe && !this.state.isReady() ? 503 : 200;
      response.writeHead(statusCode, {
        "Content-Type": "application/json",
        "Cache-Control": "no-store"
      });
      response.end(JSON.stringify(this.state.snapshot()));
    });

    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(this.port, this.host, () => {
        server.off("error", reject);
        resolve();
      });
    });
    this.server = server;
    this.logger.info("health_server_started", { host: this.host, port: this.port });
  }

  public async stop(): Promise<void> {
    const server = this.server;
    this.server = null;
    if (server === null) {
      return;
    }
    await new Promise<void>((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
    });
  }
}
