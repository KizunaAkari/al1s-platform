export class MessageDeduplicator {
  private readonly inFlight = new Set<string>();
  private readonly completed = new Map<string, number>();

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
    this.completed.set(messageId, this.now() + this.ttlMs);
  }

  public fail(messageId: string): void {
    this.inFlight.delete(messageId);
  }

  private removeExpired(): void {
    const current = this.now();
    for (const [messageId, expiresAt] of this.completed) {
      if (expiresAt <= current) {
        this.completed.delete(messageId);
      }
    }
  }
}
