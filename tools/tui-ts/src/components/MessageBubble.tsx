import { SyntaxStyle } from "@opentui/core";
import { useTerminalDimensions } from "@opentui/solid";
import type { Message } from "../lib/store.ts";
import { theme } from "../lib/theme.ts";

const ROLE_WIDTH = 9;
const RIGHT_MARGIN = 4;

const syntaxStyle = SyntaxStyle.create();

interface Props {
  msg: Message;
}

export function MessageBubble({ msg }: Props) {
  const isUser = msg.role === "user";
  const prefix = isUser ? "You    › " : "Relais › ";
  const fgContent = () => isUser ? theme.userText : theme.assistantText;
  const dims = useTerminalDimensions();
  const contentWidth = () => (dims().width ?? 80) - ROLE_WIDTH - RIGHT_MARGIN;

  return (
    <box flexDirection="row" marginBottom={1} width="100%">
      <text fg={theme.metadata} width={ROLE_WIDTH}>{prefix}</text>
      {isUser ? (
        <text fg={fgContent()} width={contentWidth()} wrapMode="word">
          {msg.text}{msg.streaming ? "▌" : ""}
        </text>
      ) : (
        <markdown
          content={msg.text + (msg.streaming ? "▌" : "")}
          syntaxStyle={syntaxStyle}
          fg={fgContent()}
          conceal
          streaming={msg.streaming}
          width={contentWidth()}
        />
      )}
    </box>
  );
}
