import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { getContradictions } from "../api/client";
import { useCapabilities } from "../hooks/useCapabilities";
import { downloadCsv, downloadJson } from "../shell/exportFile";
import "./ContradictionsPage.css";

export function ContradictionsPage() {
  const { t } = useTranslation();
  const capabilities = useCapabilities();
  const contradictionsQuery = useQuery({ queryKey: ["contradictions-full"], queryFn: getContradictions });

  const capability = capabilities.status === "loaded" ? capabilities.data.capabilities.contradictions : undefined;

  return (
    <div className="section-page contradictions-page">
      <h1>CONTRADICTIONS</h1>

      {capability && capability.status !== "present" && (
        <div className="panel contradictions-capability-notice">
          <span className="type-label">{t(`contradictions.status.${capability.status}`)}</span>
          <p>{capability.detail}</p>
        </div>
      )}

      {contradictionsQuery.status === "pending" && <p>{t("command.loading")}</p>}
      {contradictionsQuery.status === "error" && <p>{t("command.unreachable")}</p>}
      {contradictionsQuery.status === "success" && contradictionsQuery.data.contradictions.length === 0 && (
        <p>{t("contradictions.noneYet")}</p>
      )}

      {contradictionsQuery.data && contradictionsQuery.data.contradictions.length > 0 && (
        <div className="contradictions-export-bar">
          <button
            type="button"
            onClick={() =>
              downloadCsv(
                [
                  ["source_id", "target_id", "pressure_type", "pressure_score", "reason"],
                  ...contradictionsQuery.data.contradictions.map((c) => [
                    c.source_id,
                    c.target_id,
                    c.pressure_type,
                    c.pressure_score.toFixed(3),
                    c.reason,
                  ]),
                ],
                "field-horizon-contradictions.csv",
              )
            }
          >
            {t("contradictions.exportCsv")}
          </button>
          <button
            type="button"
            onClick={() => downloadJson(contradictionsQuery.data.contradictions, "field-horizon-contradictions.json")}
          >
            {t("contradictions.exportJson")}
          </button>
        </div>
      )}

      <ul className="contradictions-list">
        {contradictionsQuery.data?.contradictions.map((c, i) => (
          <li key={i} className="panel contradictions-card">
            <div className="contradictions-card-header type-mono">
              <span className="type-label">{c.pressure_type}</span>
              <span>
                {t("contradictions.pressureScore")}: {c.pressure_score.toFixed(3)}
              </span>
            </div>
            <p className="contradictions-reason">{c.reason}</p>
            <div className="contradictions-side-by-side">
              <div className="contradictions-statement">
                <Link to={`/provenance?id=${c.source_id}`} className="type-label">
                  #{c.source_id}
                </Link>
                <p>{c.statement_a}</p>
              </div>
              <div className="contradictions-vs type-mono">&harr;</div>
              <div className="contradictions-statement">
                <Link to={`/provenance?id=${c.target_id}`} className="type-label">
                  #{c.target_id}
                </Link>
                <p>{c.statement_b}</p>
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
