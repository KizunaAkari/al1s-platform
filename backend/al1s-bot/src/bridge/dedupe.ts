export class MessageDeduplicator {
  private readonly inFlight = new Set<string>();
  private readonly completed = new Map<string, number>();
  private nextExpiry = Infinity;

  public constructor(
    private readonly ttlMs: number,
    private readonly now: () => number = Date.now
  ) {}

  public tryAcquire(messageId: string): boolean {
    this.removeExpired();
    if (this.inFlight.has(messageId) || this.completed.has(messageId)) {
      return false;
    }
    this.inFlight.add(messageId);
    return true;
  }

  public complete(messageId: string): void {
    this.inFlight.delete(messageId);
    const expiresAt = this.now() + this.ttlMs;
    this.completed.set(messageId, expiresAt);
    this.nextExpiry = Math.min(this.nextExpiry, expiresAt);
  }

  public fail(messageId: string): void {
    this.inFlight.delete(messageId);
  }

  private removeExpired(): void {
    const current = this.now();
    if (current < this.nextExpiry) return;
    this.nextExpiry = Infinity;
    for (const [messageId, expiresAt] of this.completed) {
      if (expiresAt <= current) {
        this.completed.delete(messageId);
      } else {
        this.nextExpiry = Math.min(this.nextExpiry, expiresAt);
      }
    }
  }
}
