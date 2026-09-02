import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { getCouncilDetail, getCouncils } from "../api/client";
import type { CouncilCaseModel, CouncilModel } from "../api/types";
import "./CouncilsPage.css";

function CaseList({ label, cases }: { label: string; cases: CouncilCaseModel[] }) {
  if (cases.length === 0) return null;
  return (
    <div className="councils-case-group">
      <div className="type-label">{label}</div>
      <ul className="councils-case-list type-mono">
        {cases.map((c) => (
          <li key={c.cycle_id}>
            <Link to={`/provenance?id=${c.cycle_id}`}>#{c.cycle_id}</Link> {c.verdict} ({c.final_score.toFixed(2)}) --{" "}
            {c.query.slice(0, 60)}
          </li>
        ))}
      </ul>
    </div>
  );
}

function CouncilDetailPanel({ councilId, overturned }: { councilId: number; overturned: number }) {
  const { t } = useTranslation();
  const detailQuery = useQuery({ queryKey: ["council-detail", councilId], queryFn: () => getCouncilDetail(councilId) });

  if (detailQuery.status === "pending") return <p>{t("command.loading")}</p>;
  if (detailQuery.status === "error") return <p>{t("command.unreachable")}</p>;

  const detail = detailQuery.data;
  const caseCount = detail.rehabilitated.length + detail.retired.length + detail.promoted.length;
  const isEmpty = caseCount === 0;
  // The councils table's own overturned count was written synchronously by
  // this exact run -- always accurate. The case lists below are
  // reconstructed from provenance edges, which can't exist for a council
  // that ran before Implementation Brief III's domain-event chain did
  // (see provenance.backfill_provenance_edges's own "unattributed
  // canon_events" gap). overturned>0 with empty lists means real changes
  // happened but which cycles can no longer be recovered -- a materially
  // different, more honest message than "nothing changed here."
  const hasUnattributedChanges = isEmpty && overturned > 0;

  return (
    <div className="councils-detail">
      {isEmpty && !hasUnattributedChanges && <p className="councils-empty-inline">{t("councils.noChanges")}</p>}
      {hasUnattributedChanges && (
        <p className="councils-empty-inline">{t("councils.unattributedChanges", { count: overturned })}</p>
      )}
      <CaseList label={t("councils.rehabilitated")} cases={detail.rehabilitated} />
      <CaseList label={t("councils.promoted")} cases={detail.promoted} />
      <CaseList label={t("councils.retired")} cases={detail.retired} />
    </div>
  );
}

function CouncilCard({ council, isSelected, onSelect }: { council: CouncilModel; isSelected: boolean; onSelect: () => void }) {
  const { t } = useTranslation();
  return (
    <li>
      <button type="button" className={`councils-card${isSelected ? " councils-card--selected" : ""}`} onClick={onSelect}>
        <div className="councils-card-header type-mono">
          <span className="type-label">#{council.council_id}</span>
          <span>{council.started_at}</span>
          <span>
            {t("councils.examined")}: {council.examined}
          </span>
          <span>
            {t("councils.overturned")}: {council.overturned}
          </span>
          {!council.finished_at && <span className="councils-in-progress">{t("councils.inProgress")}</span>}
        </div>
      </button>
      {isSelected && <CouncilDetailPanel councilId={council.council_id} overturned={council.overturned} />}
    </li>
  );
}

export function CouncilsPage() {
  const { t } = useTranslation();
  const councilsQuery = useQuery({ queryKey: ["councils"], queryFn: () => getCouncils(50) });
  const [selectedId, setSelectedId] = useState<number | null>(null);

  return (
    <div className="section-page councils-page">
      <h1>COUNCILS</h1>

      {councilsQuery.status === "pending" && <p>{t("command.loading")}</p>}
      {councilsQuery.status === "error" && <p>{t("command.unreachable")}</p>}
      {councilsQuery.status === "success" && councilsQuery.data.councils.length === 0 && <p>{t("councils.noneYet")}</p>}

      <ul className="councils-list">
        {councilsQuery.data?.councils.map((council) => (
          <CouncilCard
            key={council.council_id}
            council={council}
            isSelected={selectedId === council.council_id}
            onSelect={() => setSelectedId(selectedId === council.council_id ? null : council.council_id)}
          />
        ))}
      </ul>
    </div>
  );
}
