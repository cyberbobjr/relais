import { createStore } from "solid-js/store";
import { DEFAULT_THEME, type ThemeConfig } from "./config.ts";

const [theme, setTheme] = createStore<ThemeConfig>({ ...DEFAULT_THEME });

export { theme };

export function initTheme(t: ThemeConfig): void {
  setTheme({ ...DEFAULT_THEME, ...t });
}
