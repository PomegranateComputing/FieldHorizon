import type { PlatformAdapter } from "./types";

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

let cached: PlatformAdapter | null = null;

/**
 * One built bundle serves both targets (Tauri webview and the /ui browser
 * build) -- this picks the real implementation at runtime instead of at
 * build time, and dynamically imports the Tauri-only module so the plain
 * web build never has to load it.
 */
export async function getPlatform(): Promise<PlatformAdapter> {
  if (cached) return cached;
  if (isTauri()) {
    const { tauriPlatform } = await import("./tauri");
    cached = tauriPlatform;
  } else {
    const { webPlatform } = await import("./web");
    cached = webPlatform;
  }
  return cached;
}

export type {
  ConfirmOptions,
  DialogFilter,
  ManagedBackendConfig,
  ManagedBackendControls,
  OpenDialogOptions,
  PlatformAdapter,
  SaveDialogOptions,
} from "./types";
