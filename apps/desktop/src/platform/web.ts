import type { ConfirmOptions, PlatformAdapter } from "./types";

const UNAVAILABLE = "Not available in the browser build -- this action requires the desktop app.";

export const webPlatform: PlatformAdapter = {
  kind: "web",
  async openFileDialog() {
    throw new Error(UNAVAILABLE);
  },
  async saveFileDialog() {
    throw new Error(UNAVAILABLE);
  },
  async confirm(message: string, _options?: ConfirmOptions) {
    return window.confirm(message);
  },
  backend: null,
};
