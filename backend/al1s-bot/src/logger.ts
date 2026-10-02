import type { LogLevel } from "./config.js";

const priorities: Record<LogLevel, number> = {
  debug: 10,
  info: 20,
  warn: 30,
  error: 40
};

function safeValue(value: unknown): unknown {
  if (value instanceof Error) {
    return {
      name: value.name,
      message: value.message,
      stack: value.stack
    };
  }
  return value;
}

export class Logger {
  public constructor(private readonly minimumLevel: LogLevel) {}

  public debug(event: string, details: Record<string, unknown> = {}): void {
    this.write("debug", event, details);
  }

  public info(event: string, details: Record<string, unknown> = {}): void {
    this.write("info", event, details);
  }

  public warn(event: string, details: Record<string, unknown> = {}): void {
    this.write("warn", event, details);
  }

  public error(event: string, details: Record<string, unknown> = {}): void {
    this.write("error", event, details);
  }

  private write(level: LogLevel, event: string, details: Record<string, unknown>): void {
    if (priorities[level] < priorities[this.minimumLevel]) {
      return;
    }

    const safeDetails = Object.fromEntries(
      Object.entries(details)
        .filter(([key]) => !key.toLowerCase().includes("token"))
        .map(([key, value]) => [key, safeValue(value)])
    );
    const output = JSON.stringify({
      timestamp: new Date().toISOString(),
      level,
      event,
      ...safeDetails
    });

    if (level === "error") {
      console.error(output);
    } else if (level === "warn") {
      console.warn(output);
    } else {
      console.log(output);
    }
  }
}
