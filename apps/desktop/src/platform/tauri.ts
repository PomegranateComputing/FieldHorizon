import { invoke } from "@tauri-apps/api/core";
import { confirm as tauriConfirm, open, save } from "@tauri-apps/plugin-dialog";

import type {
  ConfirmOptions,
  ManagedBackendConfig,
  ManagedBackendControls,
  OpenDialogOptions,
  PlatformAdapter,
  SaveDialogOptions,
} from "./types";

const backend: ManagedBackendControls = {
  async launch(config: ManagedBackendConfig): Promise<void> {
    await invoke("launch_backend", {
      pythonBin: config.pythonBin,
      repoRoot: config.repoRoot,
      port: config.port,
      tokenFile: config.tokenFile,
    });
  },
  async stop(): Promise<void> {
    await invoke("stop_backend");
  },
  async isManaged(): Promise<boolean> {
    return await invoke<boolean>("backend_is_managed");
  },
};

export const tauriPlatform: PlatformAdapter = {
  kind: "tauri",
  async openFileDialog(options?: OpenDialogOptions) {
    return await open({
      directory: options?.directory,
      multiple: options?.multiple,
      filters: options?.filters,
      title: options?.title,
    });
  },
  async saveFileDialog(options?: SaveDialogOptions) {
    return await save({
      defaultPath: options?.defaultPath,
      filters: options?.filters,
      title: options?.title,
    });
  },
  async confirm(message: string, options?: ConfirmOptions) {
    return await tauriConfirm(message, { title: options?.title, kind: options?.kind });
  },
  backend,
};
