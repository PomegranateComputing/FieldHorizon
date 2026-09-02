import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { getMetrics } from "../api/client";
import { useCapabilities } from "../hooks/useCapabilities";
import { useHealth } from "../hooks/useHealth";
import { useSystemInfo } from "../hooks/useSystemInfo";
import "./SystemPage.css";

const STATUS_CLASS: Record<string, string> = {
  ok: "system-status--ok",
  present: "system-status--ok",
  degraded: "system-status--warning",
  partial: "system-status--warning",
  failed: "system-status--danger",
  absent: "system-status--dim",
};

export function SystemPage() {
  const { t } = useTranslation();
  const health = useHealth();
  const systemInfo = useSystemInfo();
  const capabilities = useCapabilities();
  const metricsQuery = useQuery({ queryKey: ["metrics"], queryFn: getMetrics });

  return (
    <div className="section-page system-page">
      <h1>SYSTEM</h1>

      <section className="panel system-panel">
        <div className="type-label">{t("systemPage.health")}</div>
        {health.status === "loading" && <p>{t("systemPage.loading")}</p>}
        {health.status === "error" && <p className="type-mono">{t("systemPage.unreachable")}</p>}
        {health.status === "loaded" && (
          <ul className="system-check-list type-mono">
            {health.data.checks.map((check) => (
              <li key={check.name}>
                <span className={STATUS_CLASS[check.status] ?? ""}>{check.status}</span> {check.name}
                {check.detail ? ` -- ${check.detail}` : ""}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="panel system-panel">
        <div className="type-label">{t("systemPage.systemInfo")}</div>
        {systemInfo.status === "loaded" && (
          <dl className="system-info-fields type-mono">
            <dt>{t("systemPage.version")}</dt>
            <dd>{systemInfo.data.version}</dd>
            <dt>{t("systemPage.model")}</dt>
            <dd>{systemInfo.data.default_model}</dd>
            <dt>{t("systemPage.embeddingModel")}</dt>
            <dd>{systemInfo.data.embedding_model}</dd>
            <dt>{t("systemPage.mode")}</dt>
            <dd>{systemInfo.data.mode}</dd>
            <dt>{t("systemPage.database")}</dt>
            <dd>{systemInfo.data.database_path}</dd>
          </dl>
        )}
      </section>

      <section className="panel system-panel">
        <div className="type-label">{t("systemPage.metrics")}</div>
        {metricsQuery.status === "pending" && <p>{t("systemPage.loading")}</p>}
        {metricsQuery.status === "error" && <p>{t("systemPage.unreachable")}</p>}
        {metricsQuery.data && (
          <dl className="system-info-fields type-mono">
            <dt>{t("systemPage.cyclesRun")}</dt>
            <dd>{metricsQuery.data.cycles_run}</dd>
            <dt>{t("systemPage.canonSize")}</dt>
            <dd>{metricsQuery.data.canon_size}</dd>
            <dt>{t("systemPage.heresySize")}</dt>
            <dd>{metricsQuery.data.heresy_size}</dd>
            <dt>{t("systemPage.councilsRun")}</dt>
            <dd>{metricsQuery.data.councils_run}</dd>
            <dt>{t("systemPage.dreamRuns")}</dt>
            <dd>{metricsQuery.data.dream_runs}</dd>
            <dt>{t("systemPage.serverRequests")}</dt>
            <dd>{metricsQuery.data.server_requests_total}</dd>
            <dt>{t("systemPage.retrievalLatency")}</dt>
            <dd>
              p50={metricsQuery.data.retrieval_latency_p50_ms?.toFixed(1) ?? "--"}ms, p95=
              {metricsQuery.data.retrieval_latency_p95_ms?.toFixed(1) ?? "--"}ms
            </dd>
          </dl>
        )}
      </section>

      <section className="panel system-panel">
        <div className="type-label">{t("systemPage.capabilities")}</div>
        {capabilities.status === "loaded" && (
          <ul className="system-capability-list type-mono">
            {Object.entries(capabilities.data.capabilities).map(([name, capability]) => (
              <li key={name}>
                <span className={STATUS_CLASS[capability.status] ?? ""}>{capability.status}</span> {name}
                <span className="system-capability-detail"> -- {capability.detail}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
