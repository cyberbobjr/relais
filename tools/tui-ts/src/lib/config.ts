import { readFileSync, writeFileSync, mkdirSync, existsSync, chmodSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { parse as parseYaml, stringify as stringifyYaml } from "yaml";

export interface ThemeConfig {
  background: string;
  userText: string;
  assistantText: string;
  codeBlock: string;
  progress: string;
  error: string;
  metadata: string;
  statusBar: string;
  accent: string;
}

export interface Config {
  apiUrl: string;
  apiKey: string;
  historyPath: string;
  requestTimeout: number;
  lastSessionId: string;
  theme: ThemeConfig;
}

export const DEFAULT_THEME: ThemeConfig = {
  background: "#1a1a2e",
  userText: "#8be9fd",
  assistantText: "#f8f8f2",
  codeBlock: "#282a36",
  progress: "#6272a4",
  error: "#ff5555",
  metadata: "#6272a4",
  statusBar: "#16213e",
  accent: "#50fa7b",
};

const DEFAULTS: Config = {
  apiUrl: "http://localhost:8080",
  apiKey: "",
  historyPath: "~/.relais/storage/tui/history",
  requestTimeout: 300,
  lastSessionId: "",
  theme: DEFAULT_THEME,
};

export function findRelaisHome(): string | null {
  // Walk up from this file's location looking for a .env with RELAIS_HOME
  // or a .relais/ directory, whichever comes first.
  let dir = dirname(import.meta.path);
  for (let i = 0; i < 10; i++) {
    const envFile = join(dir, ".env");
    if (existsSync(envFile)) {
      try {
        const content = readFileSync(envFile, "utf8");
        const match = content.match(/^RELAIS_HOME\s*=\s*(.+)$/m);
        if (match) {
          const val = (match[1] ?? "").trim().replace(/^["']|["']$/g, "");
          // Resolve relative paths against the directory containing the .env
          return val.startsWith("/") ? val : join(dir, val);
        }
      } catch {
        // ignore unreadable .env
      }
    }
    const relaisDir = join(dir, ".relais");
    if (existsSync(relaisDir)) return relaisDir;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

function defaultConfigPath(): string {
  const home = process.env["RELAIS_HOME"] || findRelaisHome() || "";
  if (home) return join(home, "config", "tui", "config.yaml");
  return join(homedir(), ".relais", "config", "tui", "config.yaml");
}

// YAML files use snake_case keys — map them on read/write
function fromYamlKey(k: string): string {
  return k.replace(/_([a-z])/g, (_, c: string) => (c as string).toUpperCase());
}

function toYamlKey(k: string): string {
  return k.replace(/([A-Z])/g, "_$1").toLowerCase();
}

function buildConfig(raw: Record<string, unknown>): Config {
  const camel: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(raw)) camel[fromYamlKey(k)] = v;

  const themeRaw = (camel["theme"] as Record<string, unknown>) ?? {};
  const themeCamel: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(themeRaw)) themeCamel[fromYamlKey(k)] = v;

  const theme: ThemeConfig = { ...DEFAULT_THEME, ...(themeCamel as Partial<ThemeConfig>) };

  return {
    apiUrl: (camel["apiUrl"] as string) ?? DEFAULTS.apiUrl,
    apiKey: (camel["apiKey"] as string) ?? DEFAULTS.apiKey,
    historyPath: (camel["historyPath"] as string) ?? DEFAULTS.historyPath,
    requestTimeout: (camel["requestTimeout"] as number) ?? DEFAULTS.requestTimeout,
    lastSessionId: (camel["lastSessionId"] as string) ?? DEFAULTS.lastSessionId,
    theme,
  };
}

function applyEnv(cfg: Config): Config {
  const envKey = process.env["RELAIS_TUI_API_KEY"] ?? "";
  return envKey ? { ...cfg, apiKey: envKey } : cfg;
}

export function loadConfig(path?: string): Config {
  const resolved = path ?? defaultConfigPath();

  if (!existsSync(resolved)) {
    saveConfig(DEFAULTS, resolved);
    return applyEnv(DEFAULTS);
  }

  const raw = parseYaml(readFileSync(resolved, "utf8")) as Record<string, unknown> | null;
  return applyEnv(buildConfig(raw ?? {}));
}

export function saveConfig(config: Config, path?: string): void {
  const resolved = path ?? defaultConfigPath();
  mkdirSync(dirname(resolved), { recursive: true });

  // Convert camelCase keys back to snake_case for YAML
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(config)) {
    if (k === "theme") {
      const themeOut: Record<string, unknown> = {};
      for (const [tk, tv] of Object.entries(config.theme)) themeOut[toYamlKey(tk)] = tv;
      out["theme"] = themeOut;
    } else {
      out[toYamlKey(k)] = v;
    }
  }

  writeFileSync(resolved, stringifyYaml(out, { lineWidth: 0 }), "utf8");
  chmodSync(resolved, 0o600);
}

export function withSessionId(cfg: Config, sessionId: string): Config {
  return { ...cfg, lastSessionId: sessionId };
}
