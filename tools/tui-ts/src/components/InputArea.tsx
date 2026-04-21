import type { TextareaRenderable } from "@opentui/core";
import { useRenderer } from "@opentui/solid";
import { state } from "../lib/store.ts";
import { MOD_LABEL } from "../lib/clipboard.ts";

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

  function handleSubmit() {
    const raw = taRef?.editBuffer.getText() ?? "";
    const text = raw.trim();
    if (!text) return;

    if (text === "/exit") {
      renderer.destroy();
      process.exit(0);
    }

    onSubmit(text);
    taRef?.editBuffer.setText("");
  }

  return (
    <box width="100%" height={height} flexDirection="column">
      <box width="100%" height={1} backgroundColor="#16213e" flexDirection="row">
        <text fg="#6272a4"> › Input</text>
        <text fg="#6272a4" flexGrow={1} />
        <text fg="#6272a4">Enter=send  Shift+Enter=newline  select=auto-copy  {MOD_LABEL}+Y=copy  Ctrl+C=quit </text>
      </box>
      <textarea
        ref={taRef}
        focused={!state.sending}
        width="100%"
        height={height - 1}
        backgroundColor="#1a1a2e"
        textColor="#f8f8f2"
        focusedBackgroundColor="#1a1a2e"
        focusedTextColor="#f8f8f2"
        placeholder="Type your message…"
        wrapMode="word"
        keyBindings={KEY_BINDINGS}
        onSubmit={handleSubmit}
      />
    </box>
  );
}
