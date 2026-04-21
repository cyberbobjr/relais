const IS_MAC = process.platform === "darwin";

export async function writeToClipboard(text: string): Promise<void> {
  let cmd: string[];
  if (IS_MAC) {
    cmd = ["pbcopy"];
  } else if (process.platform === "win32") {
    cmd = ["clip"];
  } else {
    cmd = ["xclip", "-selection", "clipboard"];
  }
  const proc = Bun.spawn(cmd, { stdin: "pipe" });
  proc.stdin.write(text);
  proc.stdin.end();
  await proc.exited;
}

/**
 * The modifier label for UI hints.
 * macOS uses "Cmd"; all other platforms use "Ctrl".
 */
export const MOD_LABEL = IS_MAC ? "Cmd" : "Ctrl";

/**
 * Returns true when `ev` matches the platform copy key + `keyName`.
 *
 * macOS: accepts Cmd+key (super) OR Ctrl+key as fallback — Cmd events are never
 * forwarded by Terminal.app, so Ctrl is the practical fallback there.
 * Other platforms: Ctrl+key only.
 */
export function matchesCopyKey(
  ev: { ctrl: boolean; super?: boolean; name: string },
  keyName: string,
): boolean {
  if (ev.name !== keyName) return false;
  if (IS_MAC) return ev.super === true || ev.ctrl === true;
  return ev.ctrl === true;
}
