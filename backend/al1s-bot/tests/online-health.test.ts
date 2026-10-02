import { describe, expect, it } from "vitest";
import { HealthState } from "../src/health/state.js";

describe("safe online health", () => {
  it("reports a login timeout before ready without returning raw credentials", () => {
    const health = new HealthState();
    const error = Object.assign(new Error("token=SECRET https://x"), {name:"ConnectTimeoutError"});
    health.recordLoginError(error);
    const snapshot = health.platformSnapshot();
    expect(snapshot.status).toBe("unhealthy");
    expect(snapshot.diagnostics.discord_ready).toBe(false);
    expect(snapshot.diagnostics.runtime_error_code).toBe("discord_login_timeout");
    expect(JSON.stringify(snapshot)).not.toContain("SECRET");
    expect(snapshot.diagnostics.runtime_error_at).toEqual(expect.any(String));
  });
  it("clears a connection error when the Gateway becomes ready, then marks a disconnect", () => {
    const health = new HealthState();
    health.recordLoginError(new Error("failed"));
    health.setDiscordReady(true);
    expect(health.platformSnapshot().diagnostics.runtime_error_code).toBeNull();
    expect(health.platformSnapshot().status).toBe("healthy");
    health.setDiscordReady(false);
    expect(health.platformSnapshot().diagnostics.runtime_error_code).toBe("discord_gateway_disconnected");
    expect(health.platformSnapshot().diagnostics.discord_ready).toBe(false);
  });
  it("classifies invalid tokens without displaying the token", () => {
    const health = new HealthState();
    health.recordLoginError(Object.assign(new Error("SECRET"),{code:"TokenInvalid"}));
    expect(health.platformSnapshot().diagnostics.runtime_error_code).toBe("discord_authentication_failed");
    expect(JSON.stringify(health.platformSnapshot())).not.toContain("SECRET");
  });
});
