import type { RelaisClient } from "./client.ts";

export interface ClearDeps {
  client: Pick<RelaisClient, "sendMessage">;
  sessionId: string;
  clearMessages: () => void;
  onSessionId: (id: string) => void;
  setErrorBanner: (msg: string) => void;
  setCopyFlash: (msg: string) => void;
}

export async function handleClear(deps: ClearDeps): Promise<void> {
  const { client, sessionId, clearMessages, onSessionId, setErrorBanner, setCopyFlash } = deps;

  clearMessages();

  try {
    await client.sendMessage("/clear", { sessionId: sessionId || undefined });
    onSessionId("");
    setCopyFlash("History cleared");
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    setErrorBanner(`Clear failed: ${msg}`);
  }
}
