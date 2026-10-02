export class HealthState {
  private readonly startedAt = new Date().toISOString();
  private discordReady = false;
  private shuttingDown = false;
  private gatewayPingMs: number | null = null;
  private lastForwardAt: string | null = null;
  private lastForwardDiscordMessageId: string | null = null;
  private lastError: { at: string; message: string } | null = null;
  private runtimeErrorCode: string | null = null;
  private runtimeErrorAt: string | null = null;

  public setDiscordReady(ready: boolean, gatewayPingMs: number | null = null): void {
    if (ready) {
      this.runtimeErrorCode = null;
      this.runtimeErrorAt = null;
      this.lastError = null;
    } else if (this.discordReady) {
      this.runtimeErrorCode = "discord_gateway_disconnected";
      this.runtimeErrorAt = new Date().toISOString();
    }
    this.discordReady = ready;
    this.gatewayPingMs = gatewayPingMs;
  }

  public setShuttingDown(): void {
    this.shuttingDown = true;
    this.discordReady = false;
  }

  public recordForward(discordMessageId: string): void {
    this.lastForwardAt = new Date().toISOString();
    this.lastForwardDiscordMessageId = discordMessageId;
    this.lastError = null;
  }

  public recordError(error: unknown): void {
    this.lastError = {
      at: new Date().toISOString(),
      message: error instanceof Error ? error.message : String(error)
    };
    this.runtimeErrorCode = "discord_client_error";
    this.runtimeErrorAt = this.lastError.at;
  }

  public recordLoginError(error: unknown): void {
    this.recordError(error);
    const value = error as { name?: unknown; code?: unknown; cause?: { code?: unknown } } | null;
    const code = String(value?.cause?.code ?? value?.code ?? value?.name ?? "");
    this.runtimeErrorCode = /timeout|ETIMEDOUT/i.test(code) ? "discord_login_timeout"
      : code === "TokenInvalid" ? "discord_authentication_failed" : "discord_connection_error";
  }

  public isReady(): boolean {
    return this.discordReady && !this.shuttingDown;
  }

  public snapshot(): Record<string, unknown> {
    return {
      status: this.shuttingDown ? "stopping" : this.discordReady ? "ready" : "starting",
      startedAt: this.startedAt,
      discordReady: this.discordReady,
      gatewayPingMs: this.gatewayPingMs,
      lastForwardAt: this.lastForwardAt,
      lastForwardDiscordMessageId: this.lastForwardDiscordMessageId,
      lastError: this.lastError
    };
  }

  public platformSnapshot(): {
    status: "healthy" | "degraded" | "unhealthy";
    diagnostics: Record<string, unknown>;
  } {
    return {
      status: this.shuttingDown || (!this.discordReady && this.runtimeErrorCode !== null)
        ? "unhealthy" : this.discordReady ? "healthy" : "degraded",
      diagnostics: {
        discord_ready: this.discordReady,
        shutting_down: this.shuttingDown,
        gateway_ping_ms: this.gatewayPingMs,
        runtime_error_code: this.runtimeErrorCode,
        runtime_error_at: this.runtimeErrorAt,
      }
    };
  }
}
