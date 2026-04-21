import { render } from "@opentui/solid";
import { createSignal } from "solid-js";
import { App } from "./app.tsx";
import { loadConfig, saveConfig, withSessionId } from "./lib/config.ts";
import { RelaisClient } from "./lib/client.ts";
import { loadHistory, setHistoryLoading, setErrorBanner } from "./lib/store.ts";
import { logger } from "./lib/logger.ts";

const config = loadConfig();
logger.info(`tui-ts starting — logs: ${logger.path}`);

const client = new RelaisClient(config);

const [sessionId, setSessionId] = createSignal(config.lastSessionId);

function handleSessionId(id: string) {
  setSessionId(id);
  saveConfig(withSessionId(config, id));
}

// Render immediately so OpenTUI enters raw mode before any async I/O
render(
  () => (
    <App
      client={client}
      sessionId={sessionId()}
      onSessionId={handleSessionId}
    />
  )
);

// Load history after raw mode is established
async function hydrateHistory(sessionId: string): Promise<void> {
  setHistoryLoading(true);
  try {
    const turns = await client.fetchHistory(sessionId);
    if (turns.length > 0) {
      loadHistory(turns);
    }
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.warn(`history fetch failed: ${msg}`);
    setErrorBanner(`History unavailable: ${msg}`);
  } finally {
    setHistoryLoading(false);
  }
}

if (config.lastSessionId) void hydrateHistory(config.lastSessionId);
