import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  getDreamRunEvents,
  getDreamRuns,
  getProposal,
  getProposals,
  postApplyProposal,
  postRejectProposal,
} from "../api/client";
import type { DreamRunSummaryModel, ProposalSummaryModel } from "../api/types";
import { getPlatform } from "../platform";
import { trapTabKey } from "../shell/focusTrap";
import "./DreamsPage.css";

function EventRow({ event }: { event: Record<string, unknown> }) {
  const { t } = useTranslation();
  if (event.event === "region_iteration") {
    const result = (event.result as Record<string, unknown>) ?? {};
    const outcome = result.verdict ?? result.statement ?? result.reason ?? "";
    return (
      <li>
        <span className="type-label">
          #{String(event.iteration)} [{String(event.region_kind)}]
        </span>{" "}
        {String(event.region_label)} &rarr; {String(result.action)} {String(outcome).slice(0, 100)}
      </li>
    );
  }
  if (event.event === "council") {
    return (
      <li>
        <span className="type-label">{t("dreams.council")}</span> #{String(event.council_id)} --{" "}
        {t("councils.examined")}: {String(event.examined)}, {t("councils.overturned")}: {String(event.overturned)}
      </li>
    );
  }
  if (event.event === "embedding_maintenance") {
    return (
      <li>
        <span className="type-label">{t("dreams.embeddingMaintenance")}</span> {String(event.chunks_embedded)}{" "}
        {t("dreams.chunks")}, {String(event.axioms_embedded)} {t("dreams.axioms")}
      </li>
    );
  }
  if (event.event === "ontology_proposal") {
    return (
      <li>
        <span className="type-label">{t("dreams.proposalEmitted")}</span> {String(event.motif)}
      </li>
    );
  }
  return (
    <li>
      <span className="type-label">{t("dreams.noRegionAvailable")}</span>
    </li>
  );
}

function DreamRunEvents({ stamp }: { stamp: string }) {
  const eventsQuery = useQuery({ queryKey: ["dream-run-events", stamp], queryFn: () => getDreamRunEvents(stamp) });
  const { t } = useTranslation();

  if (eventsQuery.status === "pending") return <p>{t("command.loading")}</p>;
  if (eventsQuery.status === "error") return <p>{t("command.unreachable")}</p>;

  return (
    <ul className="dreams-event-list type-mono">
      {eventsQuery.data.events.map((event, i) => (
        <EventRow key={i} event={event} />
      ))}
    </ul>
  );
}

function DreamRunCard({ run, isSelected, onSelect }: { run: DreamRunSummaryModel; isSelected: boolean; onSelect: () => void }) {
  const { t } = useTranslation();
  return (
    <li>
      <button type="button" className={`dreams-run-card${isSelected ? " dreams-run-card--selected" : ""}`} onClick={onSelect}>
        <div className="dreams-run-header type-mono">
          <span>{run.started_at}</span>
          <span>
            {t("dreams.iterations")}: {run.iterations}/{run.budget_cycles}
          </span>
          <span>
            {t("dreams.regionsExplored")}: {run.regions_explored}
          </span>
          <span>
            {t("dreams.proposalsWritten")}: {run.proposals_written}
          </span>
        </div>
      </button>
      {isSelected && <DreamRunEvents stamp={run.stamp} />}
    </li>
  );
}

function ApplyConfirmDialog({
  proposal,
  onCancel,
  onDone,
}: {
  proposal: ProposalSummaryModel;
  onCancel: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<"confirm" | "applying" | "success" | "error">("confirm");
  const [resultPath, setResultPath] = useState<string>("");
  const [errorDetail, setErrorDetail] = useState<string>("");
  const dialogRef = useRef<HTMLDivElement>(null);

  // FABLE Sec.16: real keyboard/focus behavior for the app's own hand-built
  // dialog (native platform.confirm() elsewhere already gets this for free
  // from the OS/browser).
  useEffect(() => {
    dialogRef.current?.querySelector<HTMLElement>("button")?.focus();
  }, []);

  function handleEscape() {
    if (state === "confirm" || state === "error") onCancel();
    else if (state === "success") onDone();
    // "applying" has no in-flight-cancel affordance -- Escape is a no-op mid-request.
  }

  async function handleConfirm() {
    setState("applying");
    try {
      const response = await postApplyProposal(proposal.number);
      setResultPath(response.path);
      setState("success");
    } catch (e) {
      setErrorDetail(e instanceof ApiError ? e.detail || e.remediation : String(e));
      setState("error");
    }
  }

  return (
    <div className="dreams-modal-backdrop">
      <div
        ref={dialogRef}
        className="panel dreams-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="dreams-apply-dialog-title"
        onKeyDown={(event) => {
          if (event.key === "Escape") handleEscape();
          else if (dialogRef.current) trapTabKey(event, dialogRef.current);
        }}
      >
        <h2 id="dreams-apply-dialog-title" className="type-literary">{t("dreams.applyConfirmTitle")}</h2>
        <p>{t("dreams.applyConfirmBody", { name: proposal.name })}</p>
        <p className="dreams-modal-warning">{t("dreams.applyConfirmWarning")}</p>

        {state === "confirm" && (
          <div className="dreams-modal-actions">
            <button type="button" onClick={onCancel}>
              {t("dreams.cancel")}
            </button>
            <button type="button" className="dreams-apply-confirm" onClick={handleConfirm}>
              {t("dreams.confirmApply")}
            </button>
          </div>
        )}
        {state === "applying" && <p>{t("dreams.applying")}</p>}
        {state === "success" && (
          <>
            <p className="dreams-apply-success">{t("dreams.applySuccess", { path: resultPath })}</p>
            <div className="dreams-modal-actions">
              <button type="button" onClick={onDone}>
                {t("dreams.close")}
              </button>
            </div>
          </>
        )}
        {state === "error" && (
          <>
            <div className="type-label">{t("dreams.parityCheckResult")}</div>
            <pre className="dreams-apply-error type-mono">{errorDetail}</pre>
            <div className="dreams-modal-actions">
              <button type="button" onClick={onCancel}>
                {t("dreams.close")}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function ProposalCard({ proposal }: { proposal: ProposalSummaryModel }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [showApplyDialog, setShowApplyDialog] = useState(false);
  const [rejectError, setRejectError] = useState<string | null>(null);
  const applyButtonRef = useRef<HTMLButtonElement>(null);

  function closeApplyDialog() {
    setShowApplyDialog(false);
    applyButtonRef.current?.focus();
  }
  const detailQuery = useQuery({
    queryKey: ["proposal-detail", proposal.number],
    queryFn: () => getProposal(proposal.number),
    enabled: expanded,
  });

  async function handleReject() {
    setRejectError(null);
    const platform = await getPlatform();
    const confirmed = await platform.confirm(t("dreams.rejectConfirmBody", { name: proposal.name }), {
      title: t("dreams.rejectConfirmTitle"),
      kind: "warning",
    });
    if (!confirmed) return;
    try {
      await postRejectProposal(proposal.number);
      await queryClient.invalidateQueries({ queryKey: ["proposals"] });
    } catch (e) {
      setRejectError(e instanceof ApiError ? e.remediation : String(e));
    }
  }

  return (
    <li className="panel dreams-proposal-card">
      <div className="dreams-proposal-header type-mono">
        <button type="button" onClick={() => setExpanded((v) => !v)} className="dreams-proposal-toggle">
          #{proposal.number.toString().padStart(4, "0")} -- {proposal.name}
        </button>
        <span>
          {t("dreams.motif")}: {proposal.motif} ({proposal.count})
        </span>
      </div>

      {expanded && detailQuery.status === "success" && (
        <pre className="dreams-proposal-json type-mono">{JSON.stringify(detailQuery.data, null, 2)}</pre>
      )}

      <div className="dreams-proposal-actions">
        <button type="button" onClick={handleReject}>
          {t("dreams.reject")}
        </button>
        <button type="button" ref={applyButtonRef} onClick={() => setShowApplyDialog(true)}>
          {t("dreams.apply")}
        </button>
      </div>
      {rejectError && <p className="dreams-modal-warning">{rejectError}</p>}

      {showApplyDialog && (
        <ApplyConfirmDialog
          proposal={proposal}
          onCancel={closeApplyDialog}
          onDone={() => {
            closeApplyDialog();
            queryClient.invalidateQueries({ queryKey: ["proposals"] });
          }}
        />
      )}
    </li>
  );
}

export function DreamsPage() {
  const { t } = useTranslation();
  const runsQuery = useQuery({ queryKey: ["dream-runs"], queryFn: getDreamRuns });
  const proposalsQuery = useQuery({ queryKey: ["proposals"], queryFn: getProposals });
  const [selectedStamp, setSelectedStamp] = useState<string | null>(null);

  return (
    <div className="section-page dreams-page">
      <h1>DREAMS</h1>

      <section>
        <div className="type-label dreams-section-label">{t("dreams.runLedger")}</div>
        {runsQuery.status === "pending" && <p>{t("command.loading")}</p>}
        {runsQuery.status === "error" && <p>{t("command.unreachable")}</p>}
        {runsQuery.status === "success" && runsQuery.data.runs.length === 0 && <p>{t("dreams.noRunsYet")}</p>}
        <ul className="dreams-run-list">
          {runsQuery.data?.runs.map((run) => (
            <DreamRunCard
              key={run.stamp}
              run={run}
              isSelected={selectedStamp === run.stamp}
              onSelect={() => setSelectedStamp(selectedStamp === run.stamp ? null : run.stamp)}
            />
          ))}
        </ul>
      </section>

      <section>
        <div className="type-label dreams-section-label">{t("dreams.proposalsHeading")}</div>
        {proposalsQuery.status === "pending" && <p>{t("command.loading")}</p>}
        {proposalsQuery.status === "error" && <p>{t("command.unreachable")}</p>}
        {proposalsQuery.status === "success" && proposalsQuery.data.proposals.length === 0 && (
          <p>{t("dreams.noProposalsYet")}</p>
        )}
        <ul className="dreams-proposal-list">
          {proposalsQuery.data?.proposals.map((proposal) => (
            <ProposalCard key={proposal.number} proposal={proposal} />
          ))}
        </ul>
      </section>
    </div>
  );
}
