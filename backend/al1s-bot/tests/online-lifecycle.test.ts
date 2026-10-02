import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { PlatformConfigProvider } from "../src/platform-config-provider.js";

const indexModules = [
  "@sapphire/framework",
  "discord.js",
  "../src/bridge/dedupe.js",
  "../src/bridge/service.js",
  "../src/config-provider.js",
  "../src/health/server.js",
  "../src/logger.js",
  "../src/runtime.js",
  "../src/outbound-proxy.js",
];

type WorkerMocks = ReturnType<typeof installIndexMocks>;

function workerConfig() {
  return {
    discord: {
      token: "test-discord-token-that-is-long-enough-for-validation",
      applicationId: "1439231483326894160",
    },
    forward: { prefix: "[Discord]", dedupeTtlMs: 86_400_000 },
    health: { host: "127.0.0.1", port: 31_001 },
    logLevel: "error" as const,
  };
}

function installIndexMocks(options: {
  login: () => Promise<void>;
  loadRules?: (signal?: AbortSignal) => Promise<[]>;
  markApplied?: () => Promise<void>;
  startHealth?: () => Promise<void>;
  destroy?: () => Promise<void>;
  reportHealth?: (snapshot: {
    status: "healthy" | "degraded" | "unhealthy";
    diagnostics: Record<string, unknown>;
  }) => Promise<void>;
}) {
  const order: string[] = [];
  const reportHealth = vi.fn(async (snapshot: {
    status: "healthy" | "degraded" | "unhealthy";
    diagnostics: Record<string, unknown>;
  }) => {
    order.push("reportHealth");
    await options.reportHealth?.(snapshot);
  });
  const stopConfigWatch = vi.fn(() => order.push("stopConfigWatch"));
  const stopForwardRules = vi.fn(() => order.push("stopForwardRules"));
  const stopIngress = vi.fn(() => order.push("stopIngress"));
  const stopHeartbeat = vi.fn(() => order.push("stopHeartbeat"));
  const provider = {
    load: vi.fn(async () => workerConfig()),
    submitMessage: vi.fn(async () => undefined),
    startIngress: vi.fn(() => {
      order.push("startIngress");
      return stopIngress;
    }),
    loadForwardRules: vi.fn(async (signal?: AbortSignal) => {
      order.push("loadForwardRules");
      return options.loadRules ? options.loadRules(signal) : [];
    }),
    watchForwardRules: vi.fn(() => {
      order.push("watchForwardRules");
      return stopForwardRules;
    }),
    watch: vi.fn(() => stopConfigWatch),
    markApplied: vi.fn(async () => {
      order.push("markApplied");
      await options.markApplied?.();
    }),
    markRejected: vi.fn(async () => {
      order.push("markRejected");
    }),
    close: vi.fn(async () => {
      order.push("close");
    }),
    reportHealth,
    startHeartbeat: vi.fn(() => {
      order.push("startHeartbeat");
      return stopHeartbeat;
    }),
  };

  const client = {
    login: vi.fn(async () => {
      order.push("login");
      return options.login();
    }),
    destroy: vi.fn(async () => {
      order.push("destroy");
      await options.destroy?.();
    }),
  };
  class SapphireClientMock {
    public login = client.login;
    public destroy = client.destroy;
  }

  class MessageDeduplicatorMock {
    public constructor(_ttl: number) {}
  }
  class DiscordRuleBridgeMock {
    public readonly setRules = vi.fn();
    public constructor(..._args: unknown[]) {}
  }

  const healthServer = {
    start: vi.fn(async () => options.startHealth?.()),
    stop: vi.fn(async () => order.push("healthServerStop")),
  };
  class HealthServerMock {
    public start = healthServer.start;
    public stop = healthServer.stop;
    public constructor(..._args: unknown[]) {}
  }
  class LoggerMock {
    public readonly info = vi.fn();
    public readonly error = vi.fn();
    public constructor(..._args: unknown[]) {}
  }
  const setRuntime = vi.fn();
  const outboundProxy = vi.fn(() => ({}));

  vi.doMock("@sapphire/framework", () => ({ SapphireClient: SapphireClientMock }));
  vi.doMock("discord.js", () => ({ GatewayIntentBits: {
    Guilds: 1, GuildMessages: 2, MessageContent: 4,
  } }));
  vi.doMock("../src/bridge/dedupe.js", () => ({ MessageDeduplicator: MessageDeduplicatorMock }));
  vi.doMock("../src/bridge/service.js", () => ({ DiscordRuleBridge: DiscordRuleBridgeMock }));
  vi.doMock("../src/config-provider.js", () => ({
    configuredProvider: vi.fn(() => provider),
  }));
  vi.doMock("../src/health/server.js", () => ({ HealthServer: HealthServerMock }));
  vi.doMock("../src/logger.js", () => ({ Logger: LoggerMock }));
  vi.doMock("../src/runtime.js", () => ({ setRuntime }));
  vi.doMock("../src/outbound-proxy.js", () => ({ outboundProxy }));

  return { order, provider, client, stopHeartbeat, healthServer, setRuntime, outboundProxy };
}

function startWorker(options: {
  login: () => Promise<void>;
  loadRules?: (signal?: AbortSignal) => Promise<[]>;
  markApplied?: () => Promise<void>;
  startHealth?: () => Promise<void>;
  destroy?: () => Promise<void>;
  reportHealth?: WorkerMocks["provider"]["reportHealth"];
}) {
  vi.resetModules();
  const mocks = installIndexMocks(options);
  const signals = new Map<string, (...args: unknown[]) => void>();
  vi.spyOn(process, "once").mockImplementation(((event: string, listener: (...args: unknown[]) => void) => {
    signals.set(event, listener);
    return process;
  }) as never);
  vi.spyOn(process, "on").mockImplementation(((event: string, listener: (...args: unknown[]) => void) => {
    void event;
    void listener;
    return process;
  }) as never);
  vi.spyOn(process, "exit").mockImplementation(((code?: number) => {
    throw new Error(`unexpected process.exit(${code ?? "undefined"})`);
  }) as never);
  return { ...mocks, signals, importPromise: import("../src/index.js") };
}

afterEach(() => {
  for (const moduleId of indexModules) vi.doUnmock(moduleId);
  vi.restoreAllMocks();
  vi.resetModules();
  process.exitCode = undefined;
});

describe("online worker lifecycle", () => {
  it("waits for a late Gateway connection to finish closing before returning", async () => {
    let finishLogin!: () => void;
    let finishCleanup!: () => void;
    let closes = 0;
    let settled = false;
    const worker = startWorker({
      login: () => new Promise(resolve => { finishLogin = resolve; }),
      destroy: async () => {
        if (++closes === 2) await new Promise<void>(resolve => { finishCleanup = resolve; });
      },
    });
    void worker.importPromise.then(() => { settled = true; });
    await vi.waitFor(() => expect(worker.client.login).toHaveBeenCalledOnce());
    worker.signals.get("SIGTERM")!();
    await vi.waitFor(() => expect(worker.healthServer.stop).toHaveBeenCalledOnce());
    finishLogin();
    await vi.waitFor(() => expect(worker.client.destroy).toHaveBeenCalledTimes(2));
    expect(settled).toBe(false);
    finishCleanup();
    await worker.importPromise;
    expect(settled).toBe(true);
    expect(worker.provider.startIngress).not.toHaveBeenCalled();
  });
  it.each(["SIGINT", "SIGTERM"])("never resumes startup when %s stops an outstanding initial rule request", async (signal) => {
    let finish!: (value: []) => void;
    let requestSignal: AbortSignal | undefined;
    const worker = startWorker({
      login: async () => {},
      loadRules: pendingSignal => {
        requestSignal = pendingSignal;
        return new Promise(resolve => { finish = resolve; });
      },
    });
    await vi.waitFor(() => expect(worker.provider.loadForwardRules).toHaveBeenCalledOnce());
    worker.signals.get(signal)!();
    await vi.waitFor(() => expect(worker.healthServer.stop).toHaveBeenCalledOnce());
    finish([]);
    await worker.importPromise;
    expect(worker.client.login).not.toHaveBeenCalled();
    expect(worker.provider.markApplied).not.toHaveBeenCalled();
    expect(worker.provider.watchForwardRules).not.toHaveBeenCalled();
    expect(worker.provider.startIngress).not.toHaveBeenCalled();
    expect(requestSignal?.aborted).toBe(true);
    expect(process.exitCode).toBe(0);
  });
  it.each(["resolve", "reject"])("cleans a late Gateway %s after shutdown without applying or rejecting config", async (outcome) => {
    let finish!: () => void;
    const worker = startWorker({ login: () => new Promise<void>((resolve, reject) => {
      finish = () => outcome === "resolve" ? resolve() : reject(new Error("destroyed"));
    }) });
    await vi.waitFor(() => expect(worker.client.login).toHaveBeenCalledOnce());
    worker.signals.get("SIGTERM")!();
    await vi.waitFor(() => expect(worker.healthServer.stop).toHaveBeenCalledOnce());
    finish();
    await worker.importPromise;
    expect(worker.client.destroy).toHaveBeenCalledTimes(2);
    expect(worker.provider.markApplied).not.toHaveBeenCalled();
    expect(worker.provider.markRejected).not.toHaveBeenCalled();
    expect(worker.provider.watchForwardRules).not.toHaveBeenCalled();
    expect(worker.provider.startIngress).not.toHaveBeenCalled();
    expect(process.exitCode).toBe(0);
  });
  it("does not treat a cancelled initial rule failure as a startup or configuration error", async () => {
    let fail!: () => void;
    const worker = startWorker({
      login: async () => {},
      loadRules: () => new Promise((_resolve, reject) => { fail = () => reject(new Error("aborted")); }),
    });
    await vi.waitFor(() => expect(worker.provider.loadForwardRules).toHaveBeenCalledOnce());
    worker.signals.get("SIGTERM")!();
    await vi.waitFor(() => expect(worker.healthServer.stop).toHaveBeenCalledOnce());
    fail();
    await worker.importPromise;
    expect(worker.client.login).not.toHaveBeenCalled();
    expect(worker.provider.reportHealth).not.toHaveBeenCalled();
    expect(worker.provider.markRejected).not.toHaveBeenCalled();
    expect(process.exitCode).toBe(0);
  });
  it.each(["resolve", "reject"])("does not start watches or ingress when configuration finishes with %s after shutdown", async (outcome) => {
    let finish!: () => void;
    const worker = startWorker({ login: async () => {}, markApplied: () => new Promise<void>((resolve, reject) => {
      finish = () => outcome === "resolve" ? resolve() : reject(new Error("stopped"));
    }) });
    await vi.waitFor(() => expect(worker.provider.markApplied).toHaveBeenCalledOnce());
    worker.signals.get("SIGINT")!();
    await vi.waitFor(() => expect(worker.healthServer.stop).toHaveBeenCalledOnce());
    finish();
    await worker.importPromise;
    expect(worker.provider.watchForwardRules).not.toHaveBeenCalled();
    expect(worker.provider.startIngress).not.toHaveBeenCalled();
    expect(worker.provider.markRejected).not.toHaveBeenCalled();
    expect(worker.provider.close).toHaveBeenCalledOnce();
    expect(process.exitCode).toBe(0);
  });
  it("closes a health listener that finishes opening after shutdown", async () => {
    let finish!: () => void;
    const worker = startWorker({ login: async () => {}, startHealth: () => new Promise(resolve => { finish = resolve; }) });
    await vi.waitFor(() => expect(worker.healthServer.start).toHaveBeenCalledOnce());
    worker.signals.get("SIGTERM")!();
    await vi.waitFor(() => expect(worker.healthServer.stop).toHaveBeenCalledOnce());
    finish();
    await worker.importPromise;
    expect(worker.healthServer.stop).toHaveBeenCalledTimes(2);
    expect(worker.provider.startHeartbeat).not.toHaveBeenCalled();
    expect(worker.provider.loadForwardRules).not.toHaveBeenCalled();
    expect(worker.client.login).not.toHaveBeenCalled();
    expect(process.exitCode).toBe(0);
  });
  it("waits for the initial rule snapshot before the Gateway can receive messages", async () => {
    let finish!: (value: []) => void;
    const worker = startWorker({ login: async () => {}, loadRules: () => new Promise(resolve => { finish = resolve; }) });
    await vi.waitFor(() => expect(worker.provider.loadForwardRules).toHaveBeenCalledOnce());
    expect(worker.client.login).not.toHaveBeenCalled();
    finish([]); await worker.importPromise;
    expect(worker.client.login).toHaveBeenCalledOnce();
    expect(worker.order.indexOf("loadForwardRules")).toBeLessThan(worker.order.indexOf("login"));
  });
  it("keeps a temporary initial rule failure separate from a rejected bot configuration", async () => {
    const worker = startWorker({ login: async () => {}, loadRules: async () => { throw new Error("temporary rules failure"); } });
    await worker.importPromise;
    expect(worker.client.login).not.toHaveBeenCalled();
    expect(worker.provider.markRejected).not.toHaveBeenCalled();
    expect(worker.provider.reportHealth).toHaveBeenCalledOnce();
    expect(worker.stopHeartbeat).toHaveBeenCalledOnce();
  });
  it("stops cleanly when the failed initial rule load cannot report health", async () => {
    const worker = startWorker({
      login: async () => {},
      loadRules: async () => { throw new Error("rules unavailable"); },
      reportHealth: async () => { throw new Error("platform unavailable"); },
    });
    await worker.importPromise;
    expect(worker.client.login).not.toHaveBeenCalled();
    expect(worker.provider.markRejected).not.toHaveBeenCalled();
    expect(worker.provider.close).toHaveBeenCalledOnce();
    expect(worker.stopHeartbeat).toHaveBeenCalledOnce();
    expect(worker.healthServer.stop).toHaveBeenCalledOnce();
    expect(process.exitCode).toBe(1);
  });
  it("starts exactly one heartbeat while login is still pending and enables ingress only after login", async () => {
    let loginSettled = false;
    let resolveLogin!: () => void;
    const login = () => new Promise<void>(resolve => {
      resolveLogin = () => {
        loginSettled = true;
        resolve();
      };
    });
    const worker = startWorker({ login });

    await vi.waitFor(() => expect(worker.provider.startHeartbeat).toHaveBeenCalledOnce());
    expect(loginSettled).toBe(false);
    expect(worker.client.login).toHaveBeenCalledOnce();
    expect(worker.order.indexOf("startHeartbeat")).toBeLessThan(worker.order.indexOf("login"));
    expect(worker.provider.markApplied).not.toHaveBeenCalled();
    expect(worker.provider.watchForwardRules).not.toHaveBeenCalled();
    expect(worker.provider.startIngress).not.toHaveBeenCalled();

    resolveLogin();
    await worker.importPromise;

    expect(worker.provider.markApplied).toHaveBeenCalledOnce();
    expect(worker.provider.watchForwardRules).toHaveBeenCalledOnce();
    expect(worker.provider.startIngress).toHaveBeenCalledOnce();
    expect(worker.provider.startHeartbeat).toHaveBeenCalledOnce();
  });

  it("reports a safe login failure before stopping heartbeat or destroying the client", async () => {
    let resolveHealthReport!: () => void;
    const reportHealth = vi.fn(() => new Promise<void>(resolve => {
      resolveHealthReport = resolve;
    }));
    const loginError = Object.assign(new Error("Discord token=SECRET must not be reported"), {
      code: "TokenInvalid",
    });
    const worker = startWorker({
      login: async () => { throw loginError; },
      reportHealth,
    });

    await vi.waitFor(() => expect(reportHealth).toHaveBeenCalledOnce());
    const snapshot = reportHealth.mock.calls[0]?.[0];
    expect(snapshot?.status).toBe("unhealthy");
    expect(snapshot?.diagnostics).toMatchObject({
      discord_ready: false,
      runtime_error_code: "discord_authentication_failed",
      runtime_error_at: expect.any(String),
    });
    expect(JSON.stringify(snapshot)).not.toContain("SECRET");
    expect(worker.stopHeartbeat).not.toHaveBeenCalled();
    expect(worker.client.destroy).not.toHaveBeenCalled();

    resolveHealthReport();
    await worker.importPromise;

    expect(worker.provider.markRejected).toHaveBeenCalledOnce();
    expect(worker.stopHeartbeat).toHaveBeenCalledOnce();
    expect(worker.client.destroy).toHaveBeenCalledOnce();
    expect(worker.provider.close).toHaveBeenCalledOnce();
    expect(worker.healthServer.stop).toHaveBeenCalledOnce();
    expect(worker.order.indexOf("reportHealth")).toBeLessThan(worker.order.indexOf("stopHeartbeat"));
    expect(worker.order.indexOf("stopHeartbeat")).toBeLessThan(worker.order.indexOf("destroy"));
    expect(worker.provider.markApplied).not.toHaveBeenCalled();
    expect(worker.provider.watchForwardRules).not.toHaveBeenCalled();
    expect(worker.provider.startIngress).not.toHaveBeenCalled();
    expect(process.exitCode).toBe(1);
  });
});

describe("PlatformConfigProvider health reporting", () => {
  const temporaryDirectories: string[] = [];

  afterEach(async () => {
    await Promise.all(temporaryDirectories.splice(0).map(path => rm(path, { recursive: true, force: true })));
  });

  it("sends the safe health snapshot and excludes Discord and platform secrets", async () => {
    const directory = await mkdtemp(join(tmpdir(), "al1s-bot-online-health-"));
    temporaryDirectories.push(directory);
    const credentialPath = join(directory, "credential");
    await writeFile(credentialPath, "worker-id.worker-secret\n", "utf8");
    const candidate = platformCandidate();
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    const fetcher = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      requests.push({ url: String(input), ...(init === undefined ? {} : { init }) });
      return String(input).endsWith("/config")
        ? Response.json(candidate)
        : Response.json({});
    }) as unknown as typeof fetch;
    const provider = new PlatformConfigProvider({
      baseUrl: "http://platform:8000",
      credentialPath,
      stateDirectory: directory,
      fetcher,
    });

    await provider.load();
    await provider.reportHealth({
      status: "unhealthy",
      diagnostics: {
        discord_ready: false,
        runtime_error_code: "discord_login_timeout",
        runtime_error_at: "2026-10-01T00:00:00.000Z",
      },
    });

    const heartbeat = requests.find(request => request.url.endsWith("/worker/heartbeat"));
    expect(heartbeat).toBeDefined();
    const body = JSON.parse(String(heartbeat?.init?.body)) as Record<string, unknown>;
    expect(body).toMatchObject({
      config_version_id: candidate.config_version_id,
      status: "unhealthy",
      diagnostics: {
        discord_ready: false,
        runtime_error_code: "discord_login_timeout",
        ingress_pending: 0,
        ingress_blocked: 0,
      },
    });
    expect((heartbeat?.init?.headers as Record<string, string>).Authorization)
      .toBe("Bearer worker-id.worker-secret");
    expect(JSON.stringify(body)).not.toContain(candidate.secret);
    expect(JSON.stringify(body)).not.toContain("worker-secret");
    expect(JSON.stringify(body)).not.toContain("SECRET");
  });
});

function platformCandidate(): {
  application_id: string;
  application_row_version: number;
  config_version_id: string;
  version_no: number;
  service_id: string;
  settings: Record<string, string>;
  secret: string;
  config_hash: string;
} {
  return {
    application_id: "11111111-1111-4111-8111-111111111111",
    application_row_version: 1,
    config_version_id: "22222222-2222-4222-8222-222222222222",
    version_no: 1,
    service_id: "33333333-3333-4333-8333-333333333333",
    settings: {
      DISCORD_APPLICATION_ID: "1439231483326894160",
    },
    secret: "test-discord-token-that-is-long-enough-for-validation",
    config_hash: "a".repeat(64),
  };
}
