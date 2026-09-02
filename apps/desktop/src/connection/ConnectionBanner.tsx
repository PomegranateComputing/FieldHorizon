import { useTranslation } from "react-i18next";

import { useConnection } from "./ConnectionContext";
import "./startup.css";

/**
 * FABLE Sec.17: connection loss / reconnecting / degraded, shown as a slim
 * strip over the still-visible shell (stale data stays on screen) -- not a
 * full-screen takeover. That's StartupScreen's job, and only before the
 * first successful connection.
 */
export function ConnectionBanner() {
  const { t } = useTranslation();
  const { phase, attempt, retry } = useConnection();

  if (phase === "connected" || phase === "connecting") {
    return null;
  }

  if (phase === "reconnecting") {
    return <div className="connection-banner connection-banner--reconnecting type-label">{t("connectionBanner.reconnecting", { attempt })}</div>;
  }

  if (phase === "degraded") {
    return <div className="connection-banner connection-banner--degraded type-label">{t("connectionBanner.degraded")}</div>;
  }

  return (
    <div className="connection-banner connection-banner--disconnected type-label">
      {t("connectionBanner.disconnected")}
      <button type="button" onClick={retry}>
        {t("startup.retry")}
      </button>
    </div>
  );
}
