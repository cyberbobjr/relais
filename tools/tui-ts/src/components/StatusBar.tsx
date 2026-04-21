import { createSignal, onCleanup, Show } from "solid-js";
import { state } from "../lib/store.ts";

const SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

export function StatusBar() {
  const [frame, setFrame] = createSignal(0);

  const iv = setInterval(() => {
    setFrame((f) => (f + 1) % SPINNER_FRAMES.length);
  }, 80);
  onCleanup(() => clearInterval(iv));

  const spinner = () => SPINNER_FRAMES[frame()]!;

  return (
    <box width="100%" height={1} backgroundColor="#16213e" flexDirection="row">
      <Show
        when={state.copyFlash}
        fallback={
          <Show
            when={state.errorBanner}
            fallback={
              <Show
                when={state.historyLoading}
                fallback={
                  <Show
                    when={state.sending}
                    fallback={
                      <text fg="#6272a4" flexGrow={1}>
                        {" "}
                        {state.sessionId ? `session: ${state.sessionId.slice(0, 8)}…` : "ready"}
                      </text>
                    }
                  >
                    <text fg="#50fa7b">
                      {" "}{spinner()} {state.progress.label || "Thinking…"}
                    </text>
                    <text fg="#6272a4" flexGrow={1} />
                  </Show>
                }
              >
                <text fg="#f1fa8c">
                  {" "}{spinner()} Loading history…
                </text>
                <text fg="#6272a4" flexGrow={1} />
              </Show>
            }
          >
            <text fg="#ff5555" flexGrow={1}>
              {" "}⚠ {state.errorBanner}
            </text>
          </Show>
        }
      >
        <text fg="#50fa7b" flexGrow={1}>
          {" "}✓ {state.copyFlash}
        </text>
      </Show>
      <text fg="#6272a4"> /clear  /exit </text>
    </box>
  );
}
