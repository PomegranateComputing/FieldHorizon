import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { postPlannerPreview } from "../api/client";
import type { RetrievalPlanPreviewResponse, StrategyAvailabilityModel } from "../api/types";
import { useCapabilities } from "../hooks/useCapabilities";
import "./PlannerPage.css";

function StrategyRow({ strategy, isSelected }: { strategy: StrategyAvailabilityModel; isSelected: boolean }) {
  return (
    <li className={`planner-strategy-row${isSelected ? " planner-strategy-row--selected" : ""}`}>
      <div className="planner-strategy-header type-mono">
        <span className="type-label">{strategy.name}</span>
        {isSelected && <span className="planner-selected-badge">SELECTED</span>}
      </div>
      <p className="planner-strategy-reason">{strategy.reason}</p>
    </li>
  );
}

export function PlannerPage() {
  const { t } = useTranslation();
  const capabilities = useCapabilities();
  const [query, setQuery] = useState("");
  const [asOf, setAsOf] = useState("");
  const [result, setResult] = useState<RetrievalPlanPreviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const plannerCapability = capabilities.status === "loaded" ? capabilities.data.capabilities.retrieval_planner : undefined;
  const modesCapability = capabilities.status === "loaded" ? capabilities.data.capabilities.planner_modes : undefined;

  async function handlePreview(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const response = await postPlannerPreview({ query, as_of: asOf || null });
      setResult(response);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="section-page planner-page">
      <h1>PLANNER</h1>

      {modesCapability && modesCapability.status === "absent" && (
        <div className="panel planner-capability-notice">
          <span className="type-label">{t("planner.noModes")}</span>
          <p>{modesCapability.detail}</p>
        </div>
      )}
      {plannerCapability && (
        <p className="planner-capability-detail">{plannerCapability.detail}</p>
      )}

      <form className="planner-form" onSubmit={handlePreview}>
        <input
          type="text"
          className="planner-query-input"
          placeholder={t("planner.queryPlaceholder")}
          value={query}
          onChange={(e) => setQuery(e.currentTarget.value)}
        />
        <input
          type="text"
          placeholder={t("planner.asOfPlaceholder")}
          value={asOf}
          onChange={(e) => setAsOf(e.currentTarget.value)}
        />
        <button type="submit" disabled={!query.trim() || loading}>
          {loading ? t("planner.previewing") : t("planner.preview")}
        </button>
        <Link to="/retrieval" className="planner-retrieval-link">
          {t("planner.goToRetrieval")}
        </Link>
      </form>

      {error && <p className="planner-error">{error}</p>}

      {result && (
        <>
          <section className="panel planner-panel">
            <div className="type-label">{t("planner.strategies")}</div>
            <ul className="planner-strategy-list">
              {result.strategies_available.map((s) => (
                <StrategyRow key={s.name} strategy={s} isSelected={result.strategies_selected.includes(s.name)} />
              ))}
            </ul>
          </section>

          <section className="panel planner-panel">
            <div className="type-label">{t("planner.classification")}</div>
            <pre className="planner-classification type-mono">{JSON.stringify(result.classification, null, 2)}</pre>
          </section>
        </>
      )}
    </div>
  );
}
