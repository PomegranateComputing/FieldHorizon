import { useTranslation } from "react-i18next";

import { useSystemInfo } from "../hooks/useSystemInfo";
import "./SettingsPage.css";

/**
 * FABLE Sec.10.15's "never silently overwrite config" is satisfied the
 * simplest honest way: there is no config-write path in this codebase at
 * all (config.yaml is hand-edited, same "curation as code" discipline as
 * sources.yaml/ontology.yaml). This is a read-only view of the real
 * current config with no illusion of an in-app save button.
 */
export function SettingsPage() {
  const { t } = useTranslation();
  const systemInfo = useSystemInfo();

  return (
    <div className="section-page settings-page">
      <h1>SETTINGS</h1>

      <p className="settings-readonly-notice panel">{t("settings.readOnlyNotice")}</p>

      {systemInfo.status === "loading" && <p>{t("command.loading")}</p>}
      {systemInfo.status === "error" && <p>{t("command.unreachable")}</p>}
      {systemInfo.status === "loaded" && (
        <dl className="settings-fields panel type-mono">
          <dt>{t("settings.defaultModel")}</dt>
          <dd>{systemInfo.data.default_model}</dd>
          <dt>{t("settings.embeddingModel")}</dt>
          <dd>{systemInfo.data.embedding_model}</dd>
          <dt>{t("settings.ollamaBaseUrl")}</dt>
          <dd>{systemInfo.data.ollama_base_url}</dd>
          <dt>{t("settings.mode")}</dt>
          <dd>{systemInfo.data.mode}</dd>
          <dt>{t("settings.tone")}</dt>
          <dd>{systemInfo.data.tone}</dd>
          <dt>{t("settings.databasePath")}</dt>
          <dd>{systemInfo.data.database_path}</dd>
          <dt>{t("settings.retrievalWeights")}</dt>
          <dd>
            vector_similarity={systemInfo.data.default_weights.vector_similarity}, bm25=
            {systemInfo.data.default_weights.bm25}, domain_prior={systemInfo.data.default_weights.domain_prior},
            source_weight={systemInfo.data.default_weights.source_weight}, severity=
            {systemInfo.data.default_weights.severity}
          </dd>
        </dl>
      )}
    </div>
  );
}
