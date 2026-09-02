import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, postEvaluationPreview } from "../api/client";
import type { EvaluationPreviewResponse } from "../api/types";
import "./EvaluationPage.css";

// Real, fixed component weights (evaluate.py) -- renormalized constants, not
// runtime config, so there is nothing to fetch: these never drift silently.
const COMPONENT_WEIGHTS = [
  { key: "doctrinal_enforcement", weight: 0.364 },
  { key: "length_score", weight: 0.272 },
  { key: "symbolic_density", weight: 0.182 },
  { key: "structure_score", weight: 0.182 },
];

function ScoreRow({ label, value, weight }: { label: string; value: number; weight?: number }) {
  return (
    <li className="evaluation-score-row type-mono">
      <span className="type-label">{label}</span>
      <span>{value.toFixed(3)}</span>
      {weight !== undefined && <span className="evaluation-weight">{`× ${weight}`}</span>}
    </li>
  );
}

export function EvaluationPage() {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const [result, setResult] = useState<EvaluationPreviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleEvaluate(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      setResult(await postEvaluationPreview({ text }));
    } catch (err) {
      setError(err instanceof ApiError ? err.remediation : String(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="section-page evaluation-page">
      <h1>EVALUATION</h1>
      <p className="evaluation-intro">{t("evaluation.intro")}</p>

      <form className="evaluation-form" onSubmit={handleEvaluate}>
        <textarea
          className="evaluation-textarea"
          placeholder={t("evaluation.textPlaceholder")}
          value={text}
          onChange={(e) => setText(e.currentTarget.value)}
          rows={10}
        />
        <button type="submit" disabled={!text.trim() || loading}>
          {loading ? t("evaluation.evaluating") : t("evaluation.evaluate")}
        </button>
      </form>

      {error && <p className="evaluation-error">{error}</p>}

      {result && (
        <section className="panel evaluation-result">
          <div className="evaluation-verdict-row">
            <span className={`evaluation-verdict evaluation-verdict--${result.verdict.toLowerCase()}`}>
              {result.verdict}
            </span>
            <span className="type-mono evaluation-final-score">{result.final_score.toFixed(3)}</span>
          </div>

          <div className="type-label evaluation-section-label">{t("evaluation.components")}</div>
          <ul className="evaluation-score-list">
            <ScoreRow
              label={t("evaluation.doctrinalEnforcement")}
              value={result.doctrinal_enforcement}
              weight={COMPONENT_WEIGHTS[0].weight}
            />
            <ScoreRow label={t("evaluation.lengthScore")} value={result.length_score} weight={COMPONENT_WEIGHTS[1].weight} />
            <ScoreRow
              label={t("evaluation.symbolicDensity")}
              value={result.symbolic_density}
              weight={COMPONENT_WEIGHTS[2].weight}
            />
            <ScoreRow
              label={t("evaluation.structureScore")}
              value={result.structure_score}
              weight={COMPONENT_WEIGHTS[3].weight}
            />
            <ScoreRow label={t("evaluation.stuffingPenalty")} value={result.stuffing_penalty} />
            <ScoreRow label={t("evaluation.genericPenalty")} value={result.generic_penalty} />
          </ul>
          <p className="evaluation-method type-mono">
            {t("evaluation.enforcementMethod")}: {result.doctrinal_enforcement_method} --{" "}
            {t("evaluation.densityMethod")}: {result.symbolic_density_method}
          </p>

          {result.notes.length > 0 && (
            <>
              <div className="type-label evaluation-section-label">{t("evaluation.notes")}</div>
              <ul className="evaluation-notes-list">
                {result.notes.map((note, i) => (
                  <li key={i}>{note}</li>
                ))}
              </ul>
            </>
          )}
        </section>
      )}
    </div>
  );
}
