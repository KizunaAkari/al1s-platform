import { randomUUID } from "node:crypto";
import { chmod, mkdir, open, readFile, rename, rm } from "node:fs/promises";
import { dirname, join } from "node:path";

import { z } from "zod";

import { loadConfig, type AppConfig } from "./config.js";
import type { ConfigProvider } from "./config-provider.js";
import type { ForwardRule } from "./bridge/forward-rule-matcher.js";
import { EncryptedIngressQueue, type RuleForwardMessage } from "./platform-ingress.js";

const primitiveSettingSchema = z.union([z.string(), z.number(), z.boolean()]);
const workerConfigSchema = z.object({
  application_id: z.string().uuid(),
  application_row_version: z.number().int().positive(),
  config_version_id: z.string().uuid(),
  version_no: z.number().int().positive(),
  service_id: z.string().uuid(),
  settings: z.record(z.string(), primitiveSettingSchema),
  secret: z.string().nullable(),
  config_hash: z.string().regex(/^[a-f0-9]{64}$/)
});
const forwardRuleSchema = z.object({
  id: z.string().uuid(), row_version: z.number().int().positive(),
  guild_id: z.string().regex(/^[1-9][0-9]{16,19}$/),
  channel_id: z.string().regex(/^[1-9][0-9]{16,19}$/),
  trigger_kind: z.enum(["contains", "acrostic", "frequency"]),
  trigger_text: z.string().nullable(),
  frequency_count: z.number().int().nullable(),
  frequency_window_seconds: z.number().int().nullable(),
  cooldown_seconds: z.number().int().nullable(),
}).superRefine((rule, context) => {
  if (rule.trigger_kind === "frequency") {
    if (rule.frequency_count === null || rule.frequency_count < 2 || rule.frequency_count > 64
      || rule.frequency_window_seconds === null || rule.frequency_window_seconds < 1
      || rule.frequency_window_seconds > 3600 || rule.cooldown_seconds === null
      || rule.cooldown_seconds < 0 || rule.cooldown_seconds > 3600) {
      context.addIssue({ code: "custom", message: "Invalid frequency trigger" });
    }
  } else if (!rule.trigger_text?.trim()
    || Array.from(rule.trigger_text).length > (rule.trigger_kind === "acrostic" ? 64 : 256)) {
    context.addIssue({ code: "custom", message: "Invalid text trigger" });
  }
});
const registrationResponseSchema = z.object({
  identity_id: z.string().uuid(),
  service_id: z.string().uuid(),
  credential: z.string().min(1)
});

type WorkerConfigEnvelope = z.infer<typeof workerConfigSchema>;

export interface PlatformProviderOptions {
  baseUrl: string;
  credentialPath: string;
  stateDirectory: string;
  registrationCodePath?: string;
  pollIntervalMs?: number;
  fetcher?: typeof fetch;
  environment?: NodeJS.ProcessEnv;
}

export class PlatformConfigProvider implements ConfigProvider {
  private readonly baseUrl: string;
  private readonly lastKnownGoodPath: string;
  private readonly pollIntervalMs: number;
  private readonly fetcher: typeof fetch;
  private readonly environment: NodeJS.ProcessEnv;
  private candidate: WorkerConfigEnvelope | null = null;
  private active: WorkerConfigEnvelope | null = null;
  private locallyApplied = false;
  private credential: string | null = null;
  private ingress: Promise<EncryptedIngressQueue> | null = null;
  private ingressCycle: Promise<void> | null = null;
  private ingressFailureCount = 0;
  private ingressLastErrorCode: string | null = null;
  private ingressLastSuccessAt: string | null = null;
  private rulesFailureCount = 0;
  private rulesLastErrorCode: string | null = null;
  private rulesLastSuccessAt: string | null = null;

  private recordIngressFailure(code: string): void {
    this.ingressFailureCount += 1;
    this.ingressLastErrorCode = code;
  }

  private openIngress(): Promise<EncryptedIngressQueue> {
    if (this.ingress === null) {
      const opened = EncryptedIngressQueue.open(this.options.stateDirectory);
      this.ingress = opened;
      void opened.catch(() => {
        if (this.ingress === opened) this.ingress = null;
      });
    }
    return this.ingress;
  }

  public async submitMessage(message: RuleForwardMessage): Promise<void> {
    try {
      (await this.openIngress()).enqueue(message);
      if (["ingress_storage_unavailable", "ingress_enqueue_failed", "ingress_queue_full",
        "ingress_message_too_large", "invalid_ingress_key"].includes(this.ingressLastErrorCode ?? "")) {
        this.ingressFailureCount = 0;
        this.ingressLastErrorCode = null;
      }
    } catch (error) {
      const code = error instanceof Error && [
        "ingress_queue_full", "ingress_message_too_large", "invalid_ingress_key",
      ].includes(error.message) ? error.message : "ingress_enqueue_failed";
      this.recordIngressFailure(code);
      throw error;
    }
  }

  public async loadForwardRules(signal?: AbortSignal): Promise<ForwardRule[]> {
    const response = await this.request("/api/v1/bots/worker/forward-rules", { method: "GET", ...(signal ? { signal } : {}) });
    if (!response.ok) throw new Error(`Forward rules unavailable: ${response.status}`);
    const payload = z.object({ items: z.array(forwardRuleSchema).max(500) }).parse(await response.json());
    return payload.items.map(item => ({
      id: item.id, version: item.row_version, guildId: item.guild_id,
      channelId: item.channel_id,
      trigger: item.trigger_kind === "frequency"
        ? { kind: "frequency", count: item.frequency_count!,
            windowSeconds: item.frequency_window_seconds!,
            cooldownSeconds: item.cooldown_seconds! }
        : { kind: item.trigger_kind, text: item.trigger_text! },
    }));
  }

  public watchForwardRules(onChange: (rules: ForwardRule[]) => void): () => void {
    let stopped = false;
    let checking = false;
    const check = async (): Promise<void> => {
      if (stopped || checking) return;
      checking = true;
      try {
        onChange(await this.loadForwardRules());
        this.rulesFailureCount = 0;
        this.rulesLastErrorCode = null;
        this.rulesLastSuccessAt = new Date().toISOString();
      }
      catch {
        this.rulesFailureCount += 1;
        this.rulesLastErrorCode = "rules_refresh_failed";
        // Keep the last snapshot while the platform is unreachable. A stale
        // rule is rejected by the platform on ingress; clearing the snapshot
        // here could instead re-enable the legacy forwarding route.
      }
      finally { checking = false; }
    };
    void check();
    const timer = setInterval(() => void check(), this.pollIntervalMs);
    timer.unref();
    return () => { stopped = true; clearInterval(timer); };
  }

  public startIngress(): () => void {
    let stopped = false;
    let running = false;
    const flush = (): void => {
      if (stopped || running) return;
      running = true;
      const cycle = (async () => {
        try {
          let queue: EncryptedIngressQueue;
          try { queue = await this.openIngress(); }
          catch {
            this.recordIngressFailure("ingress_storage_unavailable");
            return;
          }
          let transportFailed = false;
          try {
            const result = await queue.flush(async (message) => {
              try { return await this.postRuleMatch(message); }
              catch { transportFailed = true; throw new Error("ingress_transport_failed"); }
            });
            if (result.failureCode !== null) this.recordIngressFailure(result.failureCode);
            else {
              this.ingressFailureCount = 0;
              this.ingressLastErrorCode = null;
              if (result.delivered > 0) this.ingressLastSuccessAt = new Date().toISOString();
            }
          } catch {
            this.recordIngressFailure(transportFailed ? "ingress_transport_failed" : "ingress_storage_unavailable");
            // Retain encrypted queue; retry after reconnection without body logging.
          }
        }
        finally { running = false; }
      })();
      this.ingressCycle = cycle;
      void cycle.then(() => {
        if (this.ingressCycle === cycle) this.ingressCycle = null;
      });
    };
    flush();
    const timer = setInterval(flush, 3000);
    timer.unref();
    return () => { stopped = true; clearInterval(timer); };
  }

  public async close(): Promise<void> {
    await this.ingressCycle;
    const queue = await this.ingress?.catch(() => null);
    this.ingress = null;
    queue?.close();
  }

  public constructor(private readonly options: PlatformProviderOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.lastKnownGoodPath = join(options.stateDirectory, "last-known-good.json");
    this.pollIntervalMs = options.pollIntervalMs ?? 15_000;
    this.fetcher = options.fetcher ?? fetch;
    this.environment = options.environment ?? process.env;
  }

  public async load(): Promise<AppConfig> {
    try {
      const candidate = await this.fetchCandidate();
      if (candidate !== null) {
        try {
          const config = this.parseRuntimeConfig(candidate);
          this.candidate = candidate;
          this.active = candidate;
          this.locallyApplied = false;
          return config;
        } catch {
          await this.reportRejected(candidate).catch(() => undefined);
        }
      }
    } catch {
      // Platform outages and revoked credentials must not erase a known-good file.
    }
    return this.loadLastKnownGood();
  }

  public async markApplied(): Promise<void> {
    if (this.candidate === null) {
      return;
    }
    await atomicWriteJson(this.lastKnownGoodPath, this.candidate);
    this.locallyApplied = true;
    await this.reportApplied().catch(() => undefined);
  }

  public async markRejected(): Promise<void> {
    if (this.candidate === null) {
      return;
    }
    await this.reportRejected(this.candidate).catch(() => undefined);
  }

  public watch(onChange: () => void): () => void {
    let stopped = false;
    let checking = false;
    const check = async (): Promise<void> => {
      if (stopped || checking) {
        return;
      }
      checking = true;
      try {
        if (this.locallyApplied && this.candidate !== null) {
          await this.reportApplied().catch(() => undefined);
        }
        const remote = await this.fetchCandidate();
        if (remote !== null && remote.application_id !== this.candidate?.application_id) {
          onChange();
        }
      } catch {
        // The running worker deliberately keeps its last-known-good configuration.
      } finally {
        checking = false;
      }
    };
    const timer = setInterval(() => void check(), this.pollIntervalMs);
    timer.unref();
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }

  public async reportHealth(snapshot: { status: "healthy" | "degraded" | "unhealthy"; diagnostics: Record<string, unknown> }): Promise<void> {
    let queueDiagnostics: Record<string, unknown>;
    try {
      queueDiagnostics = this.ingress ? (await this.ingress).stats()
        : { ingress_pending: 0, ingress_blocked: 0 };
    } catch {
      queueDiagnostics = { ingress_storage_unavailable: true };
    }
    const queueUnhealthy = Boolean(queueDiagnostics.ingress_storage_unavailable)
      || Number(queueDiagnostics.ingress_blocked ?? 0) > 0
      || this.ingressLastErrorCode !== null || this.rulesLastErrorCode !== null;
    await this.request("/api/v1/bots/worker/heartbeat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        receipt_event_id: randomUUID(),
        config_version_id: this.active?.config_version_id ?? null,
        status: queueUnhealthy && snapshot.status === "healthy" ? "degraded" : snapshot.status,
        diagnostics: {
          ...snapshot.diagnostics, ...queueDiagnostics,
          ingress_failure_count: this.ingressFailureCount,
          ingress_last_error_code: this.ingressLastErrorCode,
          ingress_last_success_at: this.ingressLastSuccessAt,
          rules_failure_count: this.rulesFailureCount,
          rules_last_error_code: this.rulesLastErrorCode,
          rules_last_success_at: this.rulesLastSuccessAt,
        }
      })
    }).catch(() => undefined);
  }

  public startHeartbeat(snapshot: () => { status: "healthy" | "degraded" | "unhealthy"; diagnostics: Record<string, unknown> }): () => void {
    let stopped = false;
    const send = async (): Promise<void> => {
      if (!stopped) await this.reportHealth(snapshot());
    };
    void send();
    const timer = setInterval(() => void send(), 30_000);
    timer.unref();
    return () => { stopped = true; clearInterval(timer); };
  }

  private async loadLastKnownGood(): Promise<AppConfig> {
    const content = await readFile(this.lastKnownGoodPath, "utf8");
    const envelope = workerConfigSchema.parse(JSON.parse(content));
    this.active = envelope;
    this.candidate = null;
    this.locallyApplied = false;
    return this.parseRuntimeConfig(envelope);
  }

  private parseRuntimeConfig(envelope: WorkerConfigEnvelope): AppConfig {
    const settings = Object.fromEntries(
      Object.entries(envelope.settings).map(([key, value]) => [key, String(value)])
    );
    return loadConfig({
      ...this.environment,
      ...settings,
      DISCORD_TOKEN: envelope.secret ?? "",
    });
  }

  private async fetchCandidate(): Promise<WorkerConfigEnvelope | null> {
    const response = await this.request("/api/v1/bots/worker/config", { method: "GET" });
    if (response.status === 204) {
      return null;
    }
    if (!response.ok) {
      throw new Error(`Bot configuration request failed with HTTP ${response.status}`);
    }
    return workerConfigSchema.parse(await response.json());
  }

  private async reportApplied(): Promise<void> {
    const candidate = this.candidate;
    if (candidate === null) {
      return;
    }
    const response = await this.report(candidate, "applied");
    if (!response.ok) {
      throw new Error(`Bot configuration result failed with HTTP ${response.status}`);
    }
    this.candidate = null;
    this.locallyApplied = false;
  }

  private async reportRejected(candidate: WorkerConfigEnvelope): Promise<void> {
    const response = await this.report(candidate, "rejected");
    if (response.ok) {
      this.candidate = null;
      this.locallyApplied = false;
    }
  }

  private report(candidate: WorkerConfigEnvelope, status: "applied" | "rejected"): Promise<Response> {
    return this.request(
      `/api/v1/bots/worker/config-applications/${candidate.application_id}/result`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          receipt_event_id: candidate.application_id,
          status,
          worker_instance_id: this.environment.HOSTNAME?.slice(0, 255) || "discord-worker",
          ...(status === "rejected" ? {
            error_code: "invalid_runtime_configuration",
            error_summary: "Candidate failed local runtime validation"
          } : {})
        })
      }
    );
  }

  private async request(path: string, init: RequestInit): Promise<Response> {
    init.signal?.throwIfAborted();
    const credential = await this.workerCredential();
    init.signal?.throwIfAborted();
    return this.fetcher(`${this.baseUrl}${path}`, {
      ...init,
      redirect: "error",
      headers: {
        ...init.headers,
        Authorization: `Bearer ${credential}`,
        Accept: "application/json"
      },
      signal: init.signal
        ? AbortSignal.any([init.signal, AbortSignal.timeout(10_000)])
        : AbortSignal.timeout(10_000)
    });
  }

  private async postRuleMatch(message: RuleForwardMessage): Promise<number | { status: number; code?: string }> {
    const result = await this.request("/api/v1/bots/worker/rule-matches", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(message),
    });
    if (result.status === 202) {
      const receipt = await result.json() as { id?: unknown; state?: unknown };
      if (typeof receipt.id !== "string" || typeof receipt.state !== "string") return 502;
    }
    if (result.status === 403 || result.status === 409) {
      const error = await result.json().catch(() => null) as { code?: unknown } | null;
      return typeof error?.code === "string"
        ? { status: result.status, code: error.code } : { status: result.status };
    }
    return result.status;
  }

  private async workerCredential(): Promise<string> {
    if (this.credential !== null) {
      return this.credential;
    }
    let value: string;
    try {
      value = (await readFile(this.options.credentialPath, "utf8")).trim();
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT" || !this.options.registrationCodePath) {
        throw error;
      }
      value = await this.registerWorker(this.options.registrationCodePath);
    }
    if (!value) {
      throw new Error("AL-1S Bot worker credential file is empty");
    }
    this.credential = value;
    return value;
  }

  private async registerWorker(registrationCodePath: string): Promise<string> {
    const registrationCode = (await readFile(registrationCodePath, "utf8")).trim();
    if (!registrationCode) {
      throw new Error("AL-1S Bot registration code file is empty");
    }
    const response = await this.fetcher(`${this.baseUrl}/api/v1/bots/workers/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ registration_code: registrationCode }),
      redirect: "error",
      signal: AbortSignal.timeout(10_000)
    });
    if (!response.ok) {
      throw new Error(`Bot worker registration failed with HTTP ${response.status}`);
    }
    const registered = registrationResponseSchema.parse(await response.json());
    await atomicWriteText(this.options.credentialPath, `${registered.credential}\n`);
    await rm(registrationCodePath, { force: true }).catch(() => undefined);
    return registered.credential;
  }
}

async function atomicWriteJson(path: string, value: WorkerConfigEnvelope): Promise<void> {
  await atomicWriteText(path, `${JSON.stringify(value)}\n`);
}

async function atomicWriteText(path: string, content: string): Promise<void> {
  const directory = dirname(path);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const temporaryPath = `${path}.${randomUUID()}.tmp`;
  const handle = await open(temporaryPath, "wx", 0o600);
  try {
    await handle.writeFile(content, "utf8");
    await handle.sync();
  } finally {
    await handle.close();
  }
  try {
    await rename(temporaryPath, path);
    await chmod(path, 0o600);
    try {
      const directoryHandle = await open(directory, "r");
      try {
        await directoryHandle.sync();
      } finally {
        await directoryHandle.close();
      }
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code !== "EPERM" && code !== "EINVAL") {
        throw error;
      }
    }
  } catch (error) {
    await rm(temporaryPath, { force: true }).catch(() => undefined);
    throw error;
  }
}
