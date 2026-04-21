import { appendFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { findRelaisHome } from "./config.ts";

function resolveLogPath(): string {
  const relaisHome = process.env["RELAIS_HOME"] ?? findRelaisHome();
  if (!relaisHome) throw new Error("RELAIS_HOME is not set and no .relais directory found");
  const logsDir = join(relaisHome, "logs");
  mkdirSync(logsDir, { recursive: true });
  return join(logsDir, "tui-ts.log");
}

const LOG_PATH = resolveLogPath();

function write(level: string, ...args: unknown[]): void {
  const line = `${new Date().toISOString()} [${level}] ${args.map(String).join(" ")}\n`;
  try {
    appendFileSync(LOG_PATH, line);
  } catch {
    // silently ignore write failures — we cannot use stderr in a TUI
  }
}

export const logger = {
  info: (...args: unknown[]) => write("INFO", ...args),
  warn: (...args: unknown[]) => write("WARN", ...args),
  error: (...args: unknown[]) => write("ERROR", ...args),
  debug: (...args: unknown[]) => write("DEBUG", ...args),
  path: LOG_PATH,
};
