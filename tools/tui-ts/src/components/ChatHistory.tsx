import { For, createSignal } from "solid-js";
import type { MouseEvent } from "@opentui/core";
import { state } from "../lib/store.ts";
import { MessageBubble } from "./MessageBubble.tsx";
import { Banner } from "./Banner.tsx";

interface Props {
  height: number;
}

export function ChatHistory({ height }: Props) {
  const [autoFollow, setAutoFollow] = createSignal(true);
  let scrollRef: any;

  return (
    <scrollbox
      ref={scrollRef}
      width="100%"
      height={height}
      stickyScroll={autoFollow()}
      stickyStart="bottom"
      onMouseScroll={(ev: MouseEvent) => {
        if (ev.scroll?.direction === "up") {
          setAutoFollow(false);
        } else if (ev.scroll?.direction === "down") {
          // Resume auto-follow when user scrolls back to bottom
          if (scrollRef) {
            const atBottom =
              scrollRef.scrollTop + height >= (scrollRef.scrollHeight ?? 0) - 2;
            if (atBottom) setAutoFollow(true);
          }
        }
      }}
    >
      <Banner />
      <For each={state.messages}>
        {(msg) => <MessageBubble msg={msg} />}
      </For>
    </scrollbox>
  );
}
