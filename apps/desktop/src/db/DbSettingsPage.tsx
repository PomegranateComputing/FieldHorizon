import { useTranslation } from "react-i18next";

import { SUPPORTED_LANGUAGES } from "../i18n";
import { usePolicy, useSettings } from "./hooks";
import { Empty, ErrorBox, KV, Mono, Panel } from "./kit";
import { Verdict } from "./status";

/** Screen G: where things are, which policy is in force (read-only here: it is a versioned file), and what each control means. */
export function DbSettingsPage() {
  const { t, i18n } = useTranslation();
  const settings = useSettings();
  const policy = usePolicy();
  return (
    <div className="ops-page">
      <header className="ops-page-head">
        <h1 className="ops-page-title">{t("ops.settings.title")}</h1>
        <p className="ops-page-lede">{t("ops.settings.lede")}</p>
      </header>
      <div className="ops-grid-2">
        <Panel title={t("ops.settings.language")}>
          <div className="ops-inline" role="group" aria-label={t("ops.settings.language")}>
            {SUPPORTED_LANGUAGES.map((lang) => (
              <button key={lang} type="button" className={`ops-btn${i18n.language === lang ? " ops-btn--primary" : ""}`} aria-pressed={i18n.language === lang}
                      onClick={() => void i18n.changeLanguage(lang)}>
                {lang === "fr" ? "Français" : "English"}
              </button>
            ))}
          </div>
        </Panel>
        <Panel title={t("ops.settings.paths")}>
          <ErrorBox error={settings.error} />
          {settings.data ? (
            <KV items={[
              [t("ops.settings.targetsFile"), <Mono>{settings.data.targets_file}</Mono>],
              [t("ops.settings.workloadsDir"), <Mono>{settings.data.workloads_dir}</Mono>],
              [t("ops.settings.journal"), <Mono>{settings.data.journal_database}</Mono>],
              [t("ops.settings.evidence"), <Mono>{settings.data.evidence_root}</Mono>],
              [t("ops.settings.llm"), <Mono>{settings.data.llm_model}</Mono>],
              [t("ops.settings.passfile"), Object.entries(settings.data.passfile_configured).map(([name, ok]) => (
                <span key={name} className="ops-inline-item">{name}: <Verdict value={ok} yes={t("ops.settings.configured")} no={t("ops.settings.notConfigured")} unknown="—" /></span>
              ))],
            ]} />
          ) : <Empty>{t("ops.common.loading")}</Empty>}
          <p className="ops-help">{t("ops.settings.noSecrets")}</p>
        </Panel>
      </div>
      <Panel title={t("ops.settings.controls")}>
        {settings.data && (
          <KV items={Object.entries(settings.data.controls).map(([action, text]) => [t(`ops.controls.${action}`), text])} />
        )}
      </Panel>
      <Panel title={t("ops.settings.policy")}>
        <ErrorBox error={policy.error} />
        {policy.data ? (
          <>
            <KV items={[
              [t("ops.settings.policyVersion"), `v${policy.data.version}`],
              [t("ops.settings.policyFile"), <Mono>{policy.data.source}</Mono>],
              ["sha256", <Mono>{policy.data.sha256}</Mono>],
            ]} />
            <p className="ops-help">{t("ops.settings.policyReadOnly")}</p>
            <pre className="ops-pre" tabIndex={0} aria-label={t("ops.settings.policyText")}>{policy.data.text}</pre>
          </>
        ) : <Empty>{t("ops.common.loading")}</Empty>}
      </Panel>
    </div>
  );
}
