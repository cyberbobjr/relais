import { SSEParser, type SSEEvent, type DoneEvent } from "./sse-parser.ts";
import type { Config } from "./config.ts";

const MESSAGES_PATH = "/v1/messages";
const HEALTHZ_PATH = "/healthz";
const HISTORY_PATH = "/v1/history";
const COMMANDS_PATH = "/v1/commands";

export type { SSEEvent, DoneEvent };

export interface CommandEntry {
  name: string;
  description: string;
}

export class RelaisClient {
  readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly timeoutMs: number;

  constructor(config: Config) {
    this.baseUrl = config.apiUrl;
    this.apiKey = config.apiKey;
    this.timeoutMs = config.requestTimeout * 1000;
  }

  async healthz(): Promise<boolean> {
    try {
      const resp = await this.fetch(HEALTHZ_PATH, { method: "GET" });
      return resp.ok;
    } catch {
      return false;
    }
  }

  async sendMessage(
    content: string,
    opts: { sessionId?: string; mediaRefs?: unknown[] } = {},
  ): Promise<DoneEvent> {
    const body: Record<string, unknown> = { content };
    if (opts.sessionId) body["session_id"] = opts.sessionId;
    if (opts.mediaRefs) body["media_refs"] = opts.mediaRefs;

    const resp = await this.fetch(MESSAGES_PATH, {
      method: "POST",
      headers: { ...this.authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`HTTP ${resp.status}: ${text}`);
    }

    const data = (await resp.json()) as Record<string, string>;
    return {
      type: "done",
      content: data["content"] ?? "",
      correlationId: data["correlation_id"] ?? "",
      sessionId: data["session_id"] ?? "",
    };
  }

  async *streamMessage(
    content: string,
    opts: { sessionId?: string; mediaRefs?: unknown[] } = {},
  ): AsyncGenerator<SSEEvent> {
    const body: Record<string, unknown> = { content };
    if (opts.sessionId) body["session_id"] = opts.sessionId;
    if (opts.mediaRefs) body["media_refs"] = opts.mediaRefs;

    // Rolling idle timeout: resets on every SSE chunk (keepalive included).
    // This lets the client wait indefinitely while the server is alive and
    // sending keepalives, and only aborts when the server goes silent.
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> = setTimeout(
      () => controller.abort(new DOMException("SSE idle timeout", "TimeoutError")),
      this.timeoutMs,
    );
    const resetTimer = () => {
      clearTimeout(timer);
      timer = setTimeout(
        () => controller.abort(new DOMException("SSE idle timeout", "TimeoutError")),
        this.timeoutMs,
      );
    };

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let reader: any;
    try {
      const resp = await fetch(this.baseUrl + MESSAGES_PATH, {
        method: "POST",
        headers: {
          ...this.authHeaders(),
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          "Cache-Control": "no-cache",
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`HTTP ${resp.status}: ${text}`);
      }

      const ct = resp.headers.get("content-type") ?? "";

      // JSON fallback — server did not stream
      if (ct.includes("application/json")) {
        const data = (await resp.json()) as Record<string, string>;
        yield {
          type: "done",
          content: data["content"] ?? "",
          correlationId: data["correlation_id"] ?? "",
          sessionId: data["session_id"] ?? "",
        };
        return;
      }

      if (!resp.body) throw new Error("Response has no body");

      const parser = new SSEParser();
      reader = resp.body.getReader();
      const decoder = new TextDecoder();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        resetTimer();
        const chunk = decoder.decode(value, { stream: true });
        for (const ev of parser.feed(chunk)) {
          if (ev.type !== "keepalive") {
            yield ev;
            if (ev.type === "done") return;
          }
        }
      }
    } finally {
      clearTimeout(timer);
      reader?.releaseLock();
    }
  }

  async getCommands(): Promise<CommandEntry[]> {
    const resp = await this.fetch(COMMANDS_PATH, {
      method: "GET",
      headers: this.authHeaders(),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = (await resp.json()) as { commands?: CommandEntry[] };
    return data.commands ?? [];
  }

  async fetchHistory(sessionId: string, limit = 50): Promise<Array<{ user_content: string; assistant_content: string }>> {
    const url = new URL(HISTORY_PATH, this.baseUrl);
    url.searchParams.set("session_id", sessionId);
    url.searchParams.set("limit", String(limit));
    const resp = await fetch(url.toString(), {
      headers: this.authHeaders(),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`HTTP ${resp.status}: ${text}`);
    }
    const data = (await resp.json()) as { turns?: Array<{ user_content: string; assistant_content: string }> };
    return data.turns ?? [];
  }

  private fetch(path: string, init: RequestInit): Promise<Response> {
    const url = this.baseUrl + path;
    return fetch(url, { ...init, signal: AbortSignal.timeout(this.timeoutMs) });
  }

  private authHeaders(): Record<string, string> {
    return this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : {};
  }
}
