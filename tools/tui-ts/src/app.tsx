import { useTerminalDimensions, useRenderer, useSelectionHandler, useKeyHandler } from "@opentui/solid";
import { onMount, createSignal } from "solid-js";
import { ChatHistory } from "./components/ChatHistory.tsx";
import { InputArea } from "./components/InputArea.tsx";
import { StatusBar } from "./components/StatusBar.tsx";
import {
  addUserMessage,
  beginAssistantMessage,
  applySSEEvent,
  clearMessages,
  setSending,
  setErrorBanner,
  setCopyFlash,
} from "./lib/store.ts";
import { handleClear } from "./lib/handle-clear.ts";
import { writeToClipboard, matchesCopyKey } from "./lib/clipboard.ts";
import type { RelaisClient } from "./lib/client.ts";
import { logger } from "./lib/logger.ts";

interface Props {
  client: RelaisClient;
  sessionId: string;
  onSessionId: (id: string) => void;
}

const INPUT_HEIGHT = 5;
const STATUS_HEIGHT = 1;

export function App({ client, sessionId, onSessionId }: Props) {
  const renderer = useRenderer();
  const dims = useTerminalDimensions();
  const chatHeight = () => (dims().height ?? 24) - INPUT_HEIGHT - STATUS_HEIGHT;

  let copyFlashTimer: ReturnType<typeof setTimeout> | undefined;
  const [hasSelection, setHasSelection] = createSignal(false);

  useSelectionHandler((selection: any) => {
    const active = selection !== null && selection?.isActive === true;
    const wasDragging = hasSelection();
    setHasSelection(active);

    // Auto-copy when drag ends (selection released) — best UX on all platforms
    if (wasDragging && !active) {
      const sel = renderer.getSelection?.();
      const text = sel?.getSelectedText?.();
      if (text) {
        writeToClipboard(text);
        clearTimeout(copyFlashTimer);
        setCopyFlash("Copied!");
        copyFlashTimer = setTimeout(() => setCopyFlash(""), 1500);
      }
    }
  });

  // Explicit copy keybinding as fallback
  useKeyHandler((ev: any) => {
    if (matchesCopyKey(ev, "y") && hasSelection()) {
      const sel = renderer.getSelection?.();
      const text = sel?.getSelectedText?.();
      if (text) {
        writeToClipboard(text);
        clearTimeout(copyFlashTimer);
        setCopyFlash("Copied!");
        copyFlashTimer = setTimeout(() => setCopyFlash(""), 1500);
        ev.stopPropagation?.();
        ev.preventDefault?.();
      }
    }
  });

  // Force a redraw 300ms after mount so the Kitty graphics probe bytes (written
  // by setupTerminal via fd 1, after the first render frame) get covered.
  onMount(() => {
    setTimeout(() => renderer.intermediateRender(), 300);
  });

  async function handleSubmit(text: string) {
    if (text === "/clear") {
      await handleClear({ client, sessionId, clearMessages, onSessionId, setErrorBanner, setCopyFlash });
      clearTimeout(copyFlashTimer);
      copyFlashTimer = setTimeout(() => setCopyFlash(""), 1500);
      return;
    }

    setErrorBanner("");
    setSending(true);
    addUserMessage(text);
    const assistantId = beginAssistantMessage();

    logger.info(`submit: ${text.slice(0, 80)}`);
    try {
      for await (const ev of client.streamMessage(text, { sessionId: sessionId || undefined })) {
        if (ev.type === "error") logger.error(`sse error: ${ev.error}`);
        else if (ev.type === "done") logger.info(`done session=${ev.sessionId}`);
        applySSEEvent(assistantId, ev);
        if (ev.type === "done") {
          onSessionId(ev.sessionId);
        }
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.error(`stream error: ${msg}`);
      applySSEEvent(assistantId, { type: "error", error: msg, correlationId: "" });
    } finally {
      setSending(false);
    }
  }

  return (
    <box width="100%" height="100%" flexDirection="column">
      <ChatHistory height={chatHeight()} />
      <InputArea height={INPUT_HEIGHT} onSubmit={handleSubmit} />
      <StatusBar />
    </box>
  );
}
