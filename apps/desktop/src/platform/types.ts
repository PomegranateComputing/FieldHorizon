export interface DialogFilter {
  name: string;
  extensions: string[];
}

export interface OpenDialogOptions {
  directory?: boolean;
  multiple?: boolean;
  filters?: DialogFilter[];
  title?: string;
}

export interface SaveDialogOptions {
  defaultPath?: string;
  filters?: DialogFilter[];
  title?: string;
}

export interface ConfirmOptions {
  title?: string;
  kind?: "warning" | "info" | "error";
}

export interface ManagedBackendConfig {
  pythonBin: string;
  repoRoot: string;
  port: number;
  tokenFile: string;
}

/**
 * Spawn/stop/detect a backend process this app itself launched (FABLE
 * Sec.8.4). Only present on the Tauri adapter -- a browser tab cannot spawn
 * a local process, so the web adapter's `backend` is always null.
 */
export interface ManagedBackendControls {
  launch(config: ManagedBackendConfig): Promise<void>;
  stop(): Promise<void>;
  isManaged(): Promise<boolean>;
}

/**
 * One interface, two implementations (tauri.ts / web.ts), picked at runtime
 * by index.ts -- the rest of the app never branches on which shell it's
 * running in. File dialogs throw a clear "not available" error on the web
 * adapter rather than silently no-op-ing; `backend` is simply null there.
 */
export interface PlatformAdapter {
  readonly kind: "tauri" | "web";
  openFileDialog(options?: OpenDialogOptions): Promise<string | string[] | null>;
  saveFileDialog(options?: SaveDialogOptions): Promise<string | null>;
  confirm(message: string, options?: ConfirmOptions): Promise<boolean>;
  backend: ManagedBackendControls | null;
}
