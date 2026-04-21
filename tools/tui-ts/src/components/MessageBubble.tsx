import { SyntaxStyle } from "@opentui/core";
import type { Message } from "../lib/store.ts";

const ROLE_WIDTH = 9;

const syntaxStyle = SyntaxStyle.create();

interface Props {
  msg: Message;
}

export function MessageBubble({ msg }: Props) {
  const isUser = msg.role === "user";
  const prefix = isUser ? "You    › " : "Relais › ";
  const fgContent = isUser ? "#8be9fd" : "#f8f8f2";

  return (
    <box flexDirection="row" marginBottom={1} width="100%">
      <text fg="#6272a4" width={ROLE_WIDTH}>{prefix}</text>
      {isUser ? (
        <text fg={fgContent} flexGrow={1} wrapMode="word">
          {msg.text}{msg.streaming ? "▌" : ""}
        </text>
      ) : (
        <markdown
          content={msg.text + (msg.streaming ? "▌" : "")}
          syntaxStyle={syntaxStyle}
          fg={fgContent}
          conceal
          streaming={msg.streaming}
          flexGrow={1}
        />
      )}
    </box>
  );
}
