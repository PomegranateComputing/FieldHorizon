/**
 * The bearer token for the local API is never bundled into the build --
 * there is no compiled-in constant anywhere in this tree. In the browser
 * build, it lives only in sessionStorage for the tab's lifetime, entered
 * once via TokenGate (see docs/gui/TOKEN_FLOW.md).
 */
const STORAGE_KEY = "field-horizon-api-token";

export function getStoredToken(): string | null {
  return window.sessionStorage.getItem(STORAGE_KEY);
}

export function setStoredToken(token: string): void {
  window.sessionStorage.setItem(STORAGE_KEY, token);
}

export function clearStoredToken(): void {
  window.sessionStorage.removeItem(STORAGE_KEY);
}
