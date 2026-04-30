import type { TextareaRenderable, KeyEvent, ContentChangeEvent } from "@opentui/core";
import { useRenderer } from "@opentui/solid";
import { createSignal, createMemo, createEffect, Show, For } from "solid-js";
import { state } from "../lib/store.ts";
import { MOD_LABEL } from "../lib/clipboard.ts";
import { theme } from "../lib/theme.ts";

const KEY_BINDINGS = [
  { name: "return", action: "submit" as const },
  { name: "return", shift: true, action: "newline" as const },
];

interface Props {
  height: number;
  onSubmit: (text: string) => void;
}

export function InputArea({ height, onSubmit }: Props) {
  let taRef: TextareaRenderable | undefined;
  const renderer = useRenderer();
  let _suppressContentChange = false;

  const [completionPrefix, setCompletionPrefix] = createSignal<string | null>(null);
  const [completionIndex, setCompletionIndex] = createSignal(-1);

  const filteredOptions = createMemo(() => {
    const prefix = completionPrefix();
    if (prefix === null) return [];
    return state.availableCommands.filter((cmd) => cmd.name.startsWith(prefix));
  });

  createEffect(() => {
    filteredOptions();
    setCompletionIndex(-1);
  });

  const maxCompletions = Math.max(0, height - 2);
  const visibleCount = () => Math.min(filteredOptions().length, maxCompletions);
  const textareaHeight = () => Math.max(1, height - 1 - visibleCount());

  function applyCompletion(index: number) {
    const opts = filteredOptions();
    if (index < 0 || index >= opts.length) return;
    const chosen = opts[index];
    if (!chosen) return;
    _suppressContentChange = true;
    taRef?.editBuffer.setText(`/${chosen.name}`);
    _suppressContentChange = false;
  }

  function handleSubmit() {
    const raw = taRef?.editBuffer.getText() ?? "";
    const text = raw.trim();
    if (!text) return;

    if (text === "/exit") {
      renderer.destroy();
      process.exit(0);
    }

    setCompletionPrefix(null);
    onSubmit(text);
    taRef?.editBuffer.setText("");
  }

  function handleContentChange(arg: ContentChangeEvent | string): void {
    const text = typeof arg === "string" ? arg : (taRef?.editBuffer.getText() ?? "");
    if (_suppressContentChange) return;
    const trimmed = text.trimStart();
    if (trimmed.startsWith("/") && !trimmed.includes(" ")) {
      setCompletionPrefix(trimmed.slice(1).toLowerCase());
    } else {
      setCompletionPrefix(null);
    }
  }

  function handleKeyDown(event: KeyEvent) {
    const opts = filteredOptions();

    if (event.name === "tab") {
      if (opts.length > 0) {
        event.preventDefault();
        const idx = completionIndex();
        const next = idx < 0 ? 0 : (idx + 1) % opts.length;
        setCompletionIndex(next);
        applyCompletion(next);
      }
      return;
    }

    if (event.name === "escape") {
      if (completionPrefix() !== null) {
        event.preventDefault();
        setCompletionPrefix(null);
      }
      return;
    }

    if (event.name === "return" && !event.shift && completionIndex() >= 0) {
      event.preventDefault();
      setCompletionPrefix(null);
      setCompletionIndex(-1);
    }
  }

  return (
    <box width="100%" height={height} flexDirection="column">
      <box width="100%" height={1} backgroundColor={theme.statusBar} flexDirection="row">
        <text fg={theme.metadata}> › Input</text>
        <text fg={theme.metadata} flexGrow={1} />
        <text fg={theme.metadata}>Enter=send  Shift+Enter=newline  select=auto-copy  {MOD_LABEL}+Y=copy  Ctrl+C=quit </text>
      </box>
      <Show when={visibleCount() > 0}>
        <box width="100%" height={visibleCount()} flexDirection="column">
          <For each={filteredOptions().slice(0, maxCompletions)}>
            {(opt, i) => (
              <box
                width="100%"
                height={1}
                backgroundColor={i() === completionIndex() ? theme.accent : theme.statusBar}
                flexDirection="row"
              >
                <text fg={i() === completionIndex() ? theme.background : theme.assistantText}>
                  {" "}/{opt.name}
                </text>
                <text fg={i() === completionIndex() ? theme.background : theme.metadata} flexGrow={1}>
                  {"  "}{opt.description}
                </text>
              </box>
            )}
          </For>
        </box>
      </Show>
      <textarea
        ref={taRef}
        focused={!state.sending}
        width="100%"
        height={textareaHeight()}
        backgroundColor={theme.background}
        textColor={theme.assistantText}
        focusedBackgroundColor={theme.background}
        focusedTextColor={theme.assistantText}
        placeholder="Type your message…"
        wrapMode="word"
        keyBindings={KEY_BINDINGS}
        onSubmit={handleSubmit}
        onContentChange={handleContentChange}
        onKeyDown={handleKeyDown}
      />
    </box>
  );
}
