import { render } from "@opentui/solid";
import { TextBufferRenderable } from "@opentui/core";
import { createSignal } from "solid-js";
import { App } from "./app.tsx";
import { loadConfig, saveConfig, withSessionId } from "./lib/config.ts";
import { RelaisClient } from "./lib/client.ts";
import { loadHistory, setHistoryLoading, setErrorBanner, setSessionId as setStoreSessionId, setAvailableCommands } from "./lib/store.ts";
import { initTheme } from "./lib/theme.ts";
import { logger } from "./lib/logger.ts";

// onResize sets the viewport but never calls setWrapWidth, so word-wrap never
// activates after Yoga assigns the final layout width to a text buffer node.
{
  const proto = TextBufferRenderable.prototype as any;
  const orig: (w: number, h: number) => void = proto.onResize;
  proto.onResize = function (width: number, height: number) {
    orig.call(this, width, height);
    if (this._wrapMode !== "none" && width > 0) {
      this.textBufferView.setWrapWidth(width);
    }
  };
}

const config = loadConfig();
initTheme(config.theme);
logger.info(`tui-ts starting — logs: ${logger.path}`);

const client = new RelaisClient(config);

const [sessionId, setSessionId] = createSignal(config.lastSessionId);

// Seed the store with the persisted session so the first request reuses it
if (config.lastSessionId) setStoreSessionId(config.lastSessionId);

function handleSessionId(id: string) {
  setSessionId(id);
  setStoreSessionId(id);
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

async function hydrateCommands(): Promise<void> {
  try {
    const commands = await client.getCommands();
    setAvailableCommands(commands);
    logger.info(`commands loaded: ${commands.length} entries`);
  } catch (err: unknown) {
    logger.warn(`commands fetch failed: ${err instanceof Error ? err.message : String(err)}`);
  }
}
void hydrateCommands();
