import { useState } from "react";
import { Trans, useTranslation } from "react-i18next";

import { getStoredToken, setStoredToken } from "./tokenStorage";

interface TokenGateProps {
  children: React.ReactNode;
}

/**
 * Used by both delivery targets for now (Phase UI-3 item 2's web-only design
 * turned out to be a real gap: the desktop build has no other way to obtain
 * a token either, and without one the connection manager's every request
 * would 401 forever). Auto-reading the token file's contents once a managed
 * backend is launched (its path is already known then) is a real
 * improvement still deferred, not built here.
 */
export function TokenGate({ children }: TokenGateProps) {
  const { t } = useTranslation();
  const [token, setToken] = useState(() => getStoredToken());
  const [draft, setDraft] = useState("");

  if (token) {
    return <>{children}</>;
  }

  return (
    <main aria-label="Field Horizon token entry">
      <h1>{t("tokenGate.title")}</h1>
      <p>
        <Trans i18nKey="tokenGate.instructions" components={{ code: <code /> }} />
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const trimmed = draft.trim();
          if (!trimmed) return;
          setStoredToken(trimmed);
          setToken(trimmed);
        }}
      >
        <label htmlFor="token-input">{t("tokenGate.label")}</label>
        <input
          id="token-input"
          type="password"
          autoComplete="off"
          value={draft}
          onChange={(event) => setDraft(event.currentTarget.value)}
        />
        <button type="submit" disabled={!draft.trim()}>
          {t("tokenGate.connect")}
        </button>
      </form>
    </main>
  );
}
