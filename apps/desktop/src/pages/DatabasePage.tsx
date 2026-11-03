import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getDbOperation,
  getDbOperationEvidence,
  getDbOperationReport,
  getDbOperations,
  getDbSnapshots,
  getDbTargets,
  getDbWorkloads,
  postDbDiscover,
  postDbOperation,
} from "../api/client";
import type { DbOperationDetailResponse } from "../api/types";
import { downloadMarkdown } from "../shell/exportFile";
import "./DatabasePage.css";

const STATUS_CLASS: Record<string, string> = {
  RETAINED: "db-status--ok",
  REVERTED: "db-status--warning",
  REJECTED: "db-status--warning",
  NO_CANDIDATE: "db-status--dim",
  BLOCKED: "db-status--danger",
  FAILED_RECOVERED: "db-status--danger",
  FAILED_NEEDS_RECOVERY: "db-status--danger",
  RECOVERED_REVERTED: "db-status--warning",
  RUNNING: "db-status--running",
  pass: "db-status--ok",
  fail: "db-status--danger",
  unverified: "db-status--warning",
  completed: "db-status--ok",
  failed: "db-status--danger",
  started: "db-status--running",
};

function Status({ value }: { value: string }) {
  return <span className={`db-status ${STATUS_CLASS[value] ?? ""}`}>{value}</span>;
}

function fmt(value: unknown, digits = 3): string {
  return typeof value === "number" ? value.toFixed(digits) : String(value ?? "--");
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

interface QueryMeasure {
  role: string;
  weight: number;
  baseline: { median: number };
  after: { median: number };
  median_ratio: number;
  p_faster: number;
  p_slower: number;
}

function OperationDetail({ detail }: { detail: DbOperationDetailResponse }) {
  const { t } = useTranslation();
  const op = detail.operation;
  const decision = detail.decision as
    | { outcome: string; criteria: { name: string; status: string; detail: string }[]; queries: Record<string, QueryMeasure>;
        weighted: { before: number; after: number; ratio: number } }
    | null;
  const done = op.finished_at !== null;

  return (
    <section className="panel db-panel db-detail">
      <div className="type-label">{t("database.operation")} {op.operation_id}</div>
      <p className="type-mono">
        <Status value={op.status} /> {op.target} -- {op.message ?? t("database.inProgress", { state: op.state })}
      </p>

      <div className="type-label db-subtitle">{t("database.progress")}</div>
      <table className="db-table type-mono">
        <tbody>
          {detail.steps.map((s) => (
            <tr key={s.seq as number}>
              <td>{s.seq as number}</td>
              <td>{s.step as string}</td>
              <td><Status value={s.status as string} /></td>
              <td className="db-cell-wrap">{JSON.stringify(s.detail).slice(0, 180)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {detail.candidates.length > 0 && (
        <>
          <div className="type-label db-subtitle">{t("database.candidates")}</div>
          <table className="db-table type-mono">
            <thead><tr><th>{t("database.candidate")}</th><th>{t("database.source")}</th><th>{t("database.policy")}</th><th>{t("database.justification")}</th></tr></thead>
            <tbody>
              {detail.candidates.map((c) => {
                const verdict = detail.verdicts[c.candidate_id as string] as { eligible: boolean; blocking: string[] } | undefined;
                return (
                  <tr key={c.candidate_id as string}>
                    <td>{c.key as string}</td>
                    <td>{c.source as string}</td>
                    <td>{verdict ? (verdict.eligible ? <Status value="pass" /> : <><Status value="fail" /> {verdict.blocking.join(", ")}</>) : "--"}</td>
                    <td className="db-cell-wrap">{c.rationale as string}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}

      {decision && (
        <>
          <div className="type-label db-subtitle">{t("database.benchmark")}</div>
          <table className="db-table type-mono">
            <thead>
              <tr><th>{t("database.query")}</th><th>{t("database.kind")}</th><th>{t("database.weight")}</th><th>{t("database.before")}</th>
                <th>{t("database.after")}</th><th>{t("database.ratio")}</th><th>p&lt;</th><th>p&gt;</th></tr>
            </thead>
            <tbody>
              {Object.entries(decision.queries).map(([qid, q]) => (
                <tr key={qid}>
                  <td>{qid}</td><td>{q.role}</td><td>{q.weight}</td><td>{fmt(q.baseline.median)}</td><td>{fmt(q.after.median)}</td>
                  <td>{fmt(q.median_ratio)}</td><td>{q.p_faster.toExponential(1)}</td><td>{q.p_slower.toExponential(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="type-mono">
            {t("database.weighted")}: {fmt(decision.weighted.before)} → {fmt(decision.weighted.after)} ({t("database.ratio")} {fmt(decision.weighted.ratio)})
          </p>
          <div className="type-label db-subtitle">{t("database.criteria")} -- <Status value={decision.outcome === "RETAIN" ? "RETAINED" : "REVERTED"} /></div>
          <ul className="db-list type-mono">
            {decision.criteria.map((c) => (
              <li key={c.name}><Status value={c.status} /> {c.name} -- {c.detail}</li>
            ))}
          </ul>
        </>
      )}

      {done && (
        <>
          <div className="type-label db-subtitle">{t("database.evidence")}</div>
          <ul className="db-list type-mono">
            {detail.artifacts.map((a) => (
              <li key={a.name as string}>{a.name as string} -- sha256 {(a.sha256 as string).slice(0, 16)}… ({a.bytes as number} o)</li>
            ))}
          </ul>
          <div className="db-actions">
            <button type="button" onClick={async () => downloadMarkdown(await getDbOperationReport(op.operation_id), `${op.operation_id}-report.md`)}>
              {t("database.downloadReport")}
            </button>
            <button type="button" onClick={async () => downloadBlob(await getDbOperationEvidence(op.operation_id), `${op.operation_id}-evidence.zip`)}>
              {t("database.downloadEvidence")}
            </button>
          </div>
        </>
      )}
    </section>
  );
}

export function DatabasePage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const targets = useQuery({ queryKey: ["db-targets"], queryFn: () => getDbTargets(true) });
  const workloads = useQuery({ queryKey: ["db-workloads"], queryFn: getDbWorkloads });
  const [target, setTarget] = useState("");
  const [workload, setWorkload] = useState("bench_orders.yaml");
  const [candidates, setCandidates] = useState("");
  const [useLlm, setUseLlm] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    if (!target && targets.data?.targets.length) setTarget(targets.data.targets[0].name);
  }, [target, targets.data]);

  const snapshots = useQuery({ queryKey: ["db-snapshots", target], queryFn: () => getDbSnapshots(target), enabled: !!target });
  const operations = useQuery({ queryKey: ["db-operations"], queryFn: getDbOperations, refetchInterval: 2000 });
  const detail = useQuery({
    queryKey: ["db-operation", selected],
    queryFn: () => getDbOperation(selected as string),
    enabled: !!selected,
    refetchInterval: (query) => (query.state.data?.operation.finished_at ? false : 1000),
  });

  const discover = useMutation({
    mutationFn: () => postDbDiscover(target),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["db-snapshots", target] }),
  });
  const launch = useMutation({
    mutationFn: () =>
      postDbOperation({
        target,
        workload,
        candidates: candidates.split("\n").map((c) => c.trim()).filter(Boolean),
        use_llm: useLlm,
        llm_model: null,
      }),
    onSuccess: (res) => {
      setSelected(res.operation_id);
      void queryClient.invalidateQueries({ queryKey: ["db-operations"] });
    },
  });

  const latest = snapshots.data?.snapshots[0];
  const summary = latest?.summary as
    | { availability: Record<string, string>; finding_list?: { kind: string; object: string; detail?: string }[] }
    | undefined;
  const running = operations.data?.running;

  return (
    <div className="section-page database-page">
      <h1>DATABASE</h1>

      <section className="panel db-panel">
        <div className="type-label">{t("database.targets")}</div>
        {targets.isError && <p className="type-mono">{t("database.noTargets")}</p>}
        <table className="db-table type-mono">
          <tbody>
            {targets.data?.targets.map((tg) => (
              <tr key={tg.name}>
                <td>{tg.name}</td>
                <td>{tg.engine} {tg.host}:{tg.port}/{tg.dbname}</td>
                <td>{tg.environment}{tg.writable ? ` (${t("database.writable")})` : ` (${t("database.readOnly")})`}</td>
                <td>{tg.reachable ? <Status value="pass" /> : <Status value="fail" />} {tg.server_version ?? tg.detail}</td>
                <td>{t("database.identity")}: {tg.identity_matches ? <Status value="pass" /> : <Status value="fail" />}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="panel db-panel">
        <div className="type-label">{t("database.discovery")}</div>
        <div className="db-actions">
          <select value={target} onChange={(e) => setTarget(e.currentTarget.value)} aria-label={t("database.target")}>
            {targets.data?.targets.map((tg) => <option key={tg.name} value={tg.name}>{tg.name}</option>)}
          </select>
          <button type="button" disabled={!target || discover.isPending} onClick={() => discover.mutate()}>
            {t("database.discover")}
          </button>
        </div>
        {latest && summary && (
          <div className="type-mono">
            <p>{latest.snapshot_id} -- {latest.captured_at.slice(0, 19)} -- sha256 {latest.sha256.slice(0, 16)}…</p>
            <p>{Object.entries(summary.availability).map(([k, v]) => `${k}: ${v}`).join(" · ")}</p>
            <ul className="db-list">
              {(summary.finding_list ?? []).map((f) => (
                <li key={`${f.kind}-${f.object}`}>{f.kind}: {f.object} {f.detail ?? ""}</li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <section className="panel db-panel">
        <div className="type-label">{t("database.autonomousOperation")}</div>
        <p className="type-mono db-note">{t("database.authorityNote")}</p>
        <div className="db-actions">
          <select value={workload} onChange={(e) => setWorkload(e.currentTarget.value)} aria-label={t("database.workload")}>
            {workloads.data?.workloads.map((w) => <option key={w.file} value={w.file}>{w.name}</option>)}
          </select>
          <label className="type-label">
            <input type="checkbox" checked={useLlm} onChange={(e) => setUseLlm(e.currentTarget.checked)} /> {t("database.llmAgents")}
          </label>
        </div>
        <textarea
          className="db-candidates type-mono"
          placeholder={t("database.candidatesPlaceholder")}
          value={candidates}
          onChange={(e) => setCandidates(e.currentTarget.value)}
        />
        <div className="db-actions">
          <button type="button" disabled={!target || !!running || launch.isPending} onClick={() => launch.mutate()}>
            {running ? t("database.running", { id: running }) : t("database.launch")}
          </button>
        </div>
        {launch.isError && <p className="type-mono db-status--danger">{String(launch.error)}</p>}
      </section>

      <section className="panel db-panel">
        <div className="type-label">{t("database.operations")}</div>
        <table className="db-table type-mono">
          <tbody>
            {operations.data?.operations.map((op) => (
              <tr key={op.operation_id} className={op.operation_id === selected ? "db-row--selected" : ""} onClick={() => setSelected(op.operation_id)}>
                <td>{op.operation_id}</td>
                <td><Status value={op.finished_at ? op.status : "RUNNING"} /></td>
                <td>{op.started_at.slice(0, 19)}</td>
                <td className="db-cell-wrap">{op.message ?? op.state}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {detail.data && <OperationDetail detail={detail.data} />}
    </div>
  );
}
