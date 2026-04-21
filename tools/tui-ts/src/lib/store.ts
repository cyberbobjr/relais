import { createStore, produce } from "solid-js/store";
import type { SSEEvent } from "./sse-parser.ts";

export type MessageRole = "user" | "assistant";

export interface Message {
  id: string;
  role: MessageRole;
  text: string;
  /** If true, text is being appended via streaming */
  streaming: boolean;
}

export interface ProgressState {
  active: boolean;
  label: string;
}

export interface ChatState {
  messages: Message[];
  progress: ProgressState;
  /** Session ID from the last DoneEvent */
  sessionId: string;
  /** True while a request is in flight */
  sending: boolean;
  /** True while the startup history fetch is in flight */
  historyLoading: boolean;
  /** Non-empty while a recoverable error should be shown */
  errorBanner: string;
  /** Transient flash message (e.g. "Copied!"), auto-cleared after display */
  copyFlash: string;
}

const [state, setState] = createStore<ChatState>({
  messages: [],
  progress: { active: false, label: "" },
  sessionId: "",
  sending: false,
  historyLoading: false,
  errorBanner: "",
  copyFlash: "",
});

export { state };

// ── Mutations ────────────────────────────────────────────────────────────────

export function addUserMessage(text: string): string {
  const id = crypto.randomUUID();
  setState("messages", (msgs) => [...msgs, { id, role: "user", text, streaming: false }]);
  return id;
}

export function beginAssistantMessage(): string {
  const id = crypto.randomUUID();
  setState("messages", (msgs) => [
    ...msgs,
    { id, role: "assistant", text: "", streaming: true },
  ]);
  return id;
}

export function appendToken(id: string, token: string): void {
  setState(
    "messages",
    (m) => m.id === id,
    produce((m) => {
      m.text += token;
    }),
  );
}

export function finalizeAssistantMessage(id: string, fullText?: string): void {
  setState(
    "messages",
    (m) => m.id === id,
    produce((m) => {
      if (fullText !== undefined) m.text = fullText;
      m.streaming = false;
    }),
  );
}

export function setProgress(active: boolean, label = ""): void {
  setState("progress", { active, label });
}

export function setSending(v: boolean): void {
  setState("sending", v);
}

export function setSessionId(id: string): void {
  if (id) setState("sessionId", id);
}

export function setErrorBanner(msg: string): void {
  setState("errorBanner", msg);
}

export function setCopyFlash(msg: string): void {
  setState("copyFlash", msg);
}

export function setHistoryLoading(v: boolean): void {
  setState("historyLoading", v);
}

export function clearMessages(): void {
  setState(
    produce((s) => {
      s.messages = [];
      s.progress = { active: false, label: "" };
      s.errorBanner = "";
    }),
  );
}

/** Apply a raw SSE event from the client to the store (call during streaming). */
export function applySSEEvent(assistantMsgId: string, ev: SSEEvent): void {
  switch (ev.type) {
    case "token":
      appendToken(assistantMsgId, ev.text);
      break;
    case "progress":
      setProgress(true, ev.detail || ev.event);
      break;
    case "done":
      finalizeAssistantMessage(assistantMsgId, ev.content || undefined);
      setProgress(false);
      setSessionId(ev.sessionId);
      break;
    case "error":
      finalizeAssistantMessage(assistantMsgId);
      setProgress(false);
      setErrorBanner(ev.error || "Unknown error");
      break;
    case "keepalive":
      break;
  }
}

/** Load history turns into the store (called at startup). Merge-safe: prepends only. */
export function loadHistory(turns: Array<{ user_content: string; assistant_content: string }>): void {
  const incoming: Message[] = [];
  for (const t of turns) {
    if (t.user_content) {
      incoming.push({ id: crypto.randomUUID(), role: "user", text: t.user_content, streaming: false });
    }
    if (t.assistant_content) {
      incoming.push({ id: crypto.randomUUID(), role: "assistant", text: t.assistant_content, streaming: false });
    }
  }

  if (state.messages.length === 0) {
    setState("messages", incoming);
    return;
  }

  // Deduplicate: skip history entries whose (role, text) already appear in the live store
  const existing = new Set(state.messages.map((m) => `${m.role}\0${m.text}`));
  const toAdd = incoming.filter((m) => !existing.has(`${m.role}\0${m.text}`));
  if (toAdd.length > 0) {
    setState("messages", (prev) => [...toAdd, ...prev]);
  }
}
