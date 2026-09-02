import { useTranslation } from "react-i18next";

import { useCapabilities } from "../hooks/useCapabilities";
import { useHealth } from "../hooks/useHealth";
import { useSystemInfo } from "../hooks/useSystemInfo";
import { deriveActiveCycles } from "../hooks/deriveActiveCycles";
import { SUPPORTED_LANGUAGES } from "../i18n";
import type { SupportedLanguage } from "../i18n";
import { useEventStreamContext } from "./EventStreamContext";

function basename(path: string): string {
  const parts = path.split("/");
  return parts[parts.length - 1] || path;
}

/** FABLE Sec.9's top bar: workspace name, backend state, active database, active model, planner mode, active cycles, language, command-palette/global-search access. */
export function TopBar({ onOpenPalette, onOpenSearch }: { onOpenPalette: () => void; onOpenSearch: () => void }) {
  const { t, i18n } = useTranslation();
  const health = useHealth();
  const systemInfo = useSystemInfo();
  const capabilities = useCapabilities();
  const { events } = useEventStreamContext();

  const backendStatus = health.status === "loaded" ? health.data.status : health.status === "error" ? "failed" : "loading";
  const activeCycles = deriveActiveCycles(events);

  const plannerModes = capabilities.status === "loaded" ? capabilities.data.capabilities.planner_modes : undefined;
  const plannerLabel = plannerModes?.status === "absent" ? t("topBar.plannerSingleMode") : (plannerModes?.status ?? "--");

  return (
    <header className="panel top-bar">
      <div className="top-bar-identity type-literary">FIELD HORIZON</div>

      <div className="top-bar-metrics type-label">
        <span className={`top-bar-metric backend-status backend-status--${backendStatus}`}>
          <span className={`status-dot ${backendStatus === "ok" ? "status-dot--active" : ""}`} />
          {backendStatus}
        </span>

        <span className="top-bar-metric">
          {t("topBar.db")}: {systemInfo.status === "loaded" ? basename(systemInfo.data.database_path) : "--"}
        </span>

        <span className="top-bar-metric">
          {t("topBar.model")}: {systemInfo.status === "loaded" ? systemInfo.data.default_model : "--"}
        </span>

        <span className="top-bar-metric">
          {t("topBar.planner")}: {plannerLabel}
        </span>

        <span className="top-bar-metric">
          {t("topBar.cycles")}: {activeCycles}
        </span>

        <span className="top-bar-metric top-bar-language">
          {SUPPORTED_LANGUAGES.map((lang: SupportedLanguage) => (
            <button
              key={lang}
              type="button"
              className={lang === i18n.language ? "top-bar-language-active" : ""}
              onClick={() => void i18n.changeLanguage(lang)}
            >
              {lang.toUpperCase()}
            </button>
          ))}
        </span>

        <button type="button" className="top-bar-metric" onClick={onOpenPalette} title="Ctrl/Cmd+K">
          {t("topBar.commandPalette")}
        </button>
        <button type="button" className="top-bar-metric" onClick={onOpenSearch} title="Ctrl/Cmd+Shift+K">
          {t("topBar.globalSearch")}
        </button>
      </div>
    </header>
  );
}
