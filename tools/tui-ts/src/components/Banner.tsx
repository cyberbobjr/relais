import { For } from "solid-js";
import pkg from "../../package.json";
import { theme } from "../lib/theme.ts";

const ASCII_LINES = [
  "██████╗ ███████╗██╗      █████╗ ██╗███████╗",
  "██╔══██╗██╔════╝██║     ██╔══██╗██║██╔════╝",
  "██████╔╝█████╗  ██║     ███████║██║███████╗",
  "██╔══██╗██╔══╝  ██║     ██╔══██║██║╚════██║",
  "██║  ██║███████╗███████╗██║  ██║██║███████║",
  "╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝╚═╝╚══════╝",
];

const CORE_VERSION = "0.1.0";
const TUI_VERSION = pkg.version ?? "unknown";

export function Banner() {
  return (
    <box width="100%" flexDirection="column" marginTop={1} marginBottom={1}>
      <For each={ASCII_LINES}>
        {(line) => <text fg={theme.userText} marginLeft={2}>{line}</text>}
      </For>
      <text fg={theme.metadata} marginLeft={2}>
        {`core v${CORE_VERSION}  ·  tui v${TUI_VERSION}  ·  Autonomous AI assistant`}
      </text>
      <text fg={theme.metadata} marginLeft={2}>
        /exit to quit · /clear to reset session · Shift+Enter for newline
      </text>
    </box>
  );
}
