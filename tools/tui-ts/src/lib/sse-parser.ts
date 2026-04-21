export type TokenEvent = { type: "token"; text: string };
export type DoneEvent = { type: "done"; content: string; correlationId: string; sessionId: string };
export type ProgressEvent = { type: "progress"; event: string; detail: string };
export type ErrorEvent = { type: "error"; error: string; correlationId: string };
export type Keepalive = { type: "keepalive" };

export type SSEEvent = TokenEvent | DoneEvent | ProgressEvent | ErrorEvent | Keepalive;

export class SSEParser {
  private buf = "";
  private eventType = "";
  private data = "";
  private hasComment = false;

  *feed(chunk: string): Generator<SSEEvent> {
    this.buf += chunk;

    let nl: number;
    while ((nl = this.buf.indexOf("\n")) !== -1) {
      let line = this.buf.slice(0, nl);
      this.buf = this.buf.slice(nl + 1);

      if (line.endsWith("\r")) line = line.slice(0, -1);

      if (line === "") {
        const ev = this.emit();
        if (ev) yield ev;
        continue;
      }

      if (line.startsWith(":")) {
        this.hasComment = true;
      } else if (line.includes(":")) {
        const colon = line.indexOf(":");
        const field = line.slice(0, colon);
        const value = line.slice(colon + 1).replace(/^ /, "");
        if (field === "event") this.eventType = value;
        else if (field === "data") this.data = this.data ? this.data + "\n" + value : value;
      }
    }
  }

  reset(): void {
    this.buf = "";
    this.eventType = "";
    this.data = "";
    this.hasComment = false;
  }

  private emit(): SSEEvent | null {
    const eventType = this.eventType;
    const data = this.data;
    const hasComment = this.hasComment;
    this.eventType = "";
    this.data = "";
    this.hasComment = false;

    if (hasComment && !eventType && !data) return { type: "keepalive" };
    if (!eventType || !data) return null;

    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(data) as Record<string, unknown>;
    } catch {
      return null;
    }

    switch (eventType) {
      case "token":
        return { type: "token", text: (payload["t"] as string) ?? "" };
      case "done":
        return {
          type: "done",
          content: (payload["content"] as string) ?? "",
          correlationId: (payload["correlation_id"] as string) ?? "",
          sessionId: (payload["session_id"] as string) ?? "",
        };
      case "progress":
        return {
          type: "progress",
          event: (payload["event"] as string) ?? "",
          detail: (payload["detail"] as string) ?? "",
        };
      case "error":
        return {
          type: "error",
          error: (payload["error"] as string) ?? "",
          correlationId: (payload["correlation_id"] as string) ?? "",
        };
      default:
        return null;
    }
  }
}
