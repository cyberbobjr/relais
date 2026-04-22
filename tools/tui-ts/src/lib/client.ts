import { SSEParser, type SSEEvent, type DoneEvent } from "./sse-parser.ts";
import type { Config } from "./config.ts";

const MESSAGES_PATH = "/v1/messages";
const HEALTHZ_PATH = "/healthz";
const HISTORY_PATH = "/v1/history";

export type { SSEEvent, DoneEvent };

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

    const resp = await this.fetch(MESSAGES_PATH, {
      method: "POST",
      headers: {
        ...this.authHeaders(),
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        "Cache-Control": "no-cache",
      },
      body: JSON.stringify(body),
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
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, { stream: true });
        for (const ev of parser.feed(chunk)) {
          if (ev.type !== "keepalive") yield ev;
        }
      }
    } finally {
      reader.releaseLock();
    }
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
