import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router-dom";

import type { OperationView, Verification } from "./api";
import { dbApi } from "./api";
import { MeasurementStrips } from "./MeasurementStrips";
import { OperationControls } from "./OperationControls";
import { useMeasurements, useOperation } from "./hooks";
import { Age, Duration, Empty, ErrorBox, KV, Mono, Panel, Tabs, downloadBlob, formatBytes, formatMs, formatRatio, formatSeconds, shortHash } from "./kit";
import { StatusChip, Verdict } from "./status";

type TabId = "timeline" | "decision" | "measurements" | "authority" | "agents" | "evidence";
type Check = { name: string; status: string; detail: string };

export function OperationSheet() {
  const { t } = useTranslation();
  const { operationId } = useParams();
  const view = useOperation(operationId);
  const [tab, setTab] = useState<TabId>("timeline");
  const data = view.data;
  if (view.isError) return <div className="ops-page"><ErrorBox error={view.error} /></div>;
  if (!data) return <div className="ops-page"><Empty>{t("ops.common.loading")}</Empty></div>;
  const op = data.operation;
  const isRecovery = op.kind === "recovery";
  const tabs: { id: TabId; label: string }[] = [
    { id: "timeline", label: t("ops.sheet.tabs.timeline") },
    ...(isRecovery ? [] : [{ id: "decision" as TabId, label: t("ops.sheet.tabs.decision") }, { id: "measurements" as TabId, label: t("ops.sheet.tabs.measurements") }]),
    { id: "authority", label: isRecovery ? t("ops.sheet.tabs.recovery") : t("ops.sheet.tabs.authority") },
    ...(data.claims ? [{ id: "agents" as TabId, label: t("ops.sheet.tabs.agents") }] : []),
    { id: "evidence", label: t("ops.sheet.tabs.evidence") },
  ];
  return (
    <div className="ops-page">
      <nav aria-label={t("ops.common.breadcrumb")} className="ops-breadcrumb">
        <Link to="/db/operations" className="ops-link">{t("ops.nav.operations")}</Link> / <Mono>{op.operation_id}</Mono>
      </nav>
      <header className="ops-sheet-head">
        <div className="ops-sheet-status">
          <StatusChip value={op.status} />
          {op.cause && <span className="ops-muted">{t("ops.fields.cause")} : {t(`ops.cause.${op.cause}`, { defaultValue: op.cause })} (<Mono>{op.cause}</Mono>)</span>}
        </div>
        <h1 className="ops-page-title">{isRecovery ? t("ops.sheet.recoveryTitle") : t("ops.sheet.title")} <Mono>{op.operation_id}</Mono></h1>
        {op.message && <p className="ops-sheet-message"><span className="ops-muted">{t("ops.sheet.journalMessage")}</span> {op.message}</p>}
      </header>
      <div className="ops-grid-sheet">
        <Panel title={t("ops.sheet.summary")}>
          <KV columns={2} items={[
            [t("ops.fields.target"), op.target],
            [t("ops.fields.kind"), t(`ops.kind.${op.kind}`, { defaultValue: op.kind })],
            [t("ops.fields.workload"), op.workload ?? "—"],
            [t("ops.fields.candidate"), <Mono>{op.candidate ?? "—"}</Mono>],
            [t("ops.fields.outcome"), op.outcome ? t(`ops.outcome.${op.outcome}`, { defaultValue: op.outcome }) : "—"],
            [t("ops.fields.weighted"), formatRatio(op.weighted_ratio)],
            [t("ops.fields.started"), <Age iso={op.started_at} />],
            [t("ops.fields.duration"), <Duration from={op.started_at} to={op.finished_at} />],
            [t("ops.fields.leaseToken"), op.lease_token ?? "—"],
            [t("ops.fields.llm"), op.llm ? t("ops.sheet.llmOn") : t("ops.sheet.llmOff")],
            ...(op.recovery_mode ? [[t("ops.fields.recoveryMode"), <>{t(`ops.mode.${op.recovery_mode}`, { defaultValue: op.recovery_mode })}{op.recovered_by ? <> · <Link className="ops-link" to={`/db/operations/${op.recovered_by}`}><Mono>{op.recovered_by}</Mono></Link></> : null}</>] as [string, React.ReactNode]] : []),
          ]} />
        </Panel>
        <Panel title={t("ops.sheet.controls")}>
          {op.live ? (
            <>
              <p className="ops-help">{t("ops.running.browserNote")}</p>
              <OperationControls operationId={op.operation_id} status={op.status} available={data.controls_available} />
            </>
          ) : <Empty>{t("ops.sheet.finishedNoControls")}</Empty>}
          {data.controls.length > 0 && (
            <ul className="ops-list ops-small" aria-label={t("ops.sheet.controlHistory")}>
              {(data.controls as { seq: number; action: string; requested_by: string; requested_at: string; effect?: string | null }[]).map((c) => (
                <li key={c.seq}><Mono>#{c.seq}</Mono> {t(`ops.controls.${c.action}`)} · {c.requested_by} · <Age iso={c.requested_at} /> → {c.effect ?? t("ops.controls.pending")}</li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
      <Tabs tabs={tabs} value={tab} onChange={setTab} label={t("ops.sheet.sections")} />
      <div role="tabpanel" id={`tabpanel-${tab}`} aria-labelledby={`tab-${tab}`} className="ops-tabpanel">
        {tab === "timeline" && <Timeline data={data} />}
        {tab === "decision" && <Decision data={data} />}
        {tab === "measurements" && <MeasurementsTab data={data} />}
        {tab === "authority" && <Authority data={data} />}
        {tab === "agents" && <Agents data={data} />}
        {tab === "evidence" && <Evidence data={data} />}
      </div>
    </div>
  );
}

function stepSummary(detail: Record<string, unknown>): string {
  const keys = ["candidate", "index", "outcome", "eligible", "candidates", "lease_token", "dropped", "consistent", "error", "detail", "recovery", "samples"];
  return keys.filter((k) => detail[k] !== undefined && detail[k] !== null).map((k) => `${k}: ${JSON.stringify(detail[k])}`).join(" · ").slice(0, 260);
}

function Timeline({ data }: { data: OperationView }) {
  const { t } = useTranslation();
  const steps = data.timeline as { seq: number; step: string; status: string; started_at: string; finished_at?: string | null; detail: Record<string, unknown> }[];
  if (!steps.length) return <Empty>{t("ops.sheet.noSteps")}</Empty>;
  return (
    <ol className="ops-timeline">
      {steps.map((s) => (
        <li key={s.seq} className={`ops-timeline-step ops-timeline-step--${s.status}`}>
          <div className="ops-timeline-head">
            <StatusChip value={s.status} size="sm" />
            <strong>{t(`ops.step.${s.step}`, { defaultValue: s.step })}</strong>
            <span className="ops-muted ops-small"><Duration from={s.started_at} to={s.finished_at ?? null} /></span>
          </div>
          <div className="ops-timeline-detail ops-small">{stepSummary(s.detail)}</div>
        </li>
      ))}
    </ol>
  );
}

function Decision({ data }: { data: OperationView }) {
  const { t } = useTranslation();
  const decision = data.decision as { outcome: string; criteria: Check[]; weighted: { before: number; after: number; ratio: number };
    policy?: { version: number; sha256: string }; side_effects?: Record<string, { tables?: Record<string, { n_dead_tup?: number }>; sequences_consumed?: Record<string, number>; database_bytes?: number }> } | null;
  if (!decision) return <Empty>{t("ops.sheet.noDecision")}</Empty>;
  return (
    <div className="ops-stack">
      <Panel title={t("ops.decision.criteria")} actions={<StatusChip value={decision.outcome === "RETAIN" ? "pass" : "fail"} label={decision.outcome} />}>
        <p className="ops-help">{t("ops.decision.rule", { version: decision.policy?.version ?? "?" })}</p>
        <div className="ops-table-wrap">
          <table className="ops-table">
            <caption className="sr-only">{t("ops.decision.criteria")}</caption>
            <thead><tr><th scope="col">{t("ops.decision.criterion")}</th><th scope="col">{t("ops.fields.status")}</th><th scope="col">{t("ops.decision.detail")}</th></tr></thead>
            <tbody>
              {decision.criteria.map((c) => (
                <tr key={c.name}><td><Mono>{c.name}</Mono></td><td><StatusChip value={c.status} size="sm" /></td><td className="ops-small">{c.detail}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel title={t("ops.decision.weighted")}>
        <KV items={[
          [t("ops.decision.before"), formatMs(decision.weighted.before)],
          [t("ops.decision.after"), formatMs(decision.weighted.after)],
          [t("ops.decision.ratio"), formatRatio(decision.weighted.ratio)],
        ]} />
        <p className="ops-help">{t("ops.decision.weightedNote")}</p>
      </Panel>
      {decision.side_effects && (
        <Panel title={t("ops.decision.sideEffects")}>
          <p className="ops-help">{t("ops.decision.sideEffectsNote")}</p>
          <KV items={Object.entries(decision.side_effects).map(([phase, fx]) => [
            t(`ops.decision.phase.${phase}`, { defaultValue: phase }),
            t("ops.decision.sideEffectsLine", {
              dead: Object.values(fx.tables ?? {}).reduce((a, v) => a + (v.n_dead_tup ?? 0), 0),
              ids: Object.values(fx.sequences_consumed ?? {}).reduce((a, v) => a + v, 0),
              bytes: formatBytes(fx.database_bytes ?? 0),
            }),
          ])} />
        </Panel>
      )}
    </div>
  );
}

function MeasurementsTab({ data }: { data: OperationView }) {
  const { t } = useTranslation();
  const m = useMeasurements(data.operation.operation_id, data.measurements_available, data.operation.finished_at ?? data.operation.state);
  if (!data.measurements_available) return <Empty>{t("ops.measure.none")}</Empty>;
  if (m.isError) return <ErrorBox error={m.error} />;
  if (!m.data) return <Empty>{t("ops.common.loading")}</Empty>;
  const queries = m.data.queries as unknown as Parameters<typeof MeasurementStrips>[0]["queries"];
  return (
    <div className="ops-stack">
      <Panel title={t("ops.measure.title")}>
        <p className="ops-help">{t("ops.measure.units")}</p>
        <MeasurementStrips queries={queries} />
      </Panel>
      <Panel title={t("ops.measure.table")}>
        <div className="ops-table-wrap">
          <table className="ops-table">
            <caption className="sr-only">{t("ops.measure.table")}</caption>
            <thead><tr>
              <th scope="col">{t("ops.measure.query")}</th><th scope="col">{t("ops.measure.kind")}</th><th scope="col" className="ops-num">{t("ops.measure.weight")}</th>
              <th scope="col" className="ops-num">{t("ops.measure.serverBefore")}</th><th scope="col" className="ops-num">{t("ops.measure.serverAfter")}</th>
              <th scope="col" className="ops-num">{t("ops.measure.ratio")}</th><th scope="col" className="ops-num">{t("ops.measure.clientRatio")}</th>
              <th scope="col" className="ops-num">{t("ops.measure.samples")}</th>
            </tr></thead>
            <tbody>
              {(m.data.queries as { query_id: string; kind?: string; weight?: number; control?: boolean; phases: Record<string, unknown[]>;
                stats?: { baseline?: { median?: number }; after?: { median?: number }; median_ratio?: number; client?: { median_ratio?: number } } }[]).map((q) => (
                <tr key={q.query_id}>
                  <td><Mono>{q.query_id}</Mono></td>
                  <td>{q.kind}{q.control ? ` (${t("ops.measure.control")})` : ""}</td>
                  <td className="ops-num">{q.weight}</td>
                  <td className="ops-num">{formatMs(q.stats?.baseline?.median)}</td>
                  <td className="ops-num">{formatMs(q.stats?.after?.median)}</td>
                  <td className="ops-num">{formatRatio(q.stats?.median_ratio)}</td>
                  <td className="ops-num">{formatRatio(q.stats?.client?.median_ratio)}</td>
                  <td className="ops-num">{(q.phases.baseline?.length ?? 0) + (q.phases.after?.length ?? 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}

function Authority({ data }: { data: OperationView }) {
  const { t } = useTranslation();
  const authority = (data.authority as { authority?: { checks?: Check[]; eligible?: boolean } } | null)?.authority;
  const authz = (data.authorization as { authorization?: { verified_at?: string; policy_sha256?: string; target_fingerprint?: string; lease?: { fencing_token?: number; backend_pid?: number } }; verdict?: { checks?: Check[] } } | null);
  const change = data.change as { index?: { schema: string; name: string; size_bytes: number; oid?: number; definition?: string }; build_seconds?: number } | null;
  const compensation = data.compensation as { drop?: { dropped?: boolean; proof?: { basis?: string; proven?: boolean } }; post_check?: { catalog?: { index_absent?: boolean; plans_use_removed_index?: boolean }; target_ratio?: number | null } } | null;
  const failure = data.execution_failure as { code?: string; detail?: string; cleanup?: Record<string, unknown> } | null;
  const recovery = data.recovery as { run_id?: string; mode?: string; status?: string; message?: string; actions?: { change?: string; index?: string; result?: string; proof?: { basis?: string } }[]; blocked?: { code?: string; detail?: string; index?: string }[] } | null;
  return (
    <div className="ops-stack">
      {authority && (
        <Panel title={t("ops.authority.write")}>
          <ul className="ops-checks">
            {(authority.checks ?? []).map((c) => <li key={c.name}><StatusChip value={c.status} size="sm" /> <Mono>{c.name}</Mono> <span className="ops-small ops-muted">{c.detail}</span></li>)}
          </ul>
        </Panel>
      )}
      {authz?.authorization && (
        <Panel title={t("ops.authority.reverification")}>
          <p className="ops-help">{t("ops.authority.reverificationNote")}</p>
          <KV items={[
            [t("ops.authority.verifiedAt"), <Age iso={authz.authorization.verified_at} />],
            [t("ops.authority.policy"), <Mono title={authz.authorization.policy_sha256}>{shortHash(authz.authorization.policy_sha256)}</Mono>],
            [t("ops.authority.targetFingerprint"), <Mono>{shortHash(authz.authorization.target_fingerprint)}</Mono>],
            [t("ops.fields.leaseToken"), authz.authorization.lease?.fencing_token ?? "—"],
            [t("ops.authority.backend"), authz.authorization.lease?.backend_pid ?? "—"],
          ]} />
          <ul className="ops-checks">
            {(authz.verdict?.checks ?? []).map((c) => <li key={c.name}><StatusChip value={c.status} size="sm" /> <Mono>{c.name}</Mono> <span className="ops-small ops-muted">{c.detail}</span></li>)}
          </ul>
        </Panel>
      )}
      {change?.index && (
        <Panel title={t("ops.authority.change")}>
          <KV items={[
            [t("ops.indexes.index"), <Mono>{change.index.schema}.{change.index.name}</Mono>],
            [t("ops.authority.definition"), <Mono>{change.index.definition}</Mono>],
            [t("ops.indexes.size"), formatBytes(change.index.size_bytes)],
            [t("ops.authority.build"), formatSeconds(change.build_seconds ?? NaN)],
          ]} />
        </Panel>
      )}
      {compensation && (
        <Panel title={t("ops.authority.compensation")}>
          <p className="ops-help">{t("ops.authority.compensationNote")}</p>
          <KV items={[
            [t("ops.authority.dropped"), <Verdict value={compensation.drop?.dropped ?? null} yes={t("ops.common.yes")} no={t("ops.common.no")} unknown="—" />],
            [t("ops.authority.proof"), compensation.drop?.proof?.basis ?? "—"],
            [t("ops.authority.catalogAbsent"), <Verdict value={compensation.post_check?.catalog?.index_absent ?? null} yes={t("ops.authority.confirmed")} no={t("ops.authority.stillPresent")} unknown="—" />],
            [t("ops.authority.plansClean"), <Verdict value={compensation.post_check?.catalog?.plans_use_removed_index === undefined ? null : !compensation.post_check.catalog.plans_use_removed_index} yes={t("ops.common.yes")} no={t("ops.common.no")} unknown="—" />],
            [t("ops.authority.postRatio"), formatRatio(compensation.post_check?.target_ratio ?? null)],
          ]} />
        </Panel>
      )}
      {failure && (
        <Panel title={t("ops.authority.failure")}>
          <KV items={[[t("ops.fields.cause"), <Mono>{failure.code}</Mono>], [t("ops.decision.detail"), failure.detail], [t("ops.authority.cleanup"), <Mono>{JSON.stringify(failure.cleanup)}</Mono>]]} />
        </Panel>
      )}
      {recovery && (
        <Panel title={t("ops.authority.recovery")}>
          <KV items={[
            [t("ops.startup.result"), <StatusChip value={recovery.status ?? ""} />],
            [t("ops.startup.mode"), t(`ops.mode.${recovery.mode ?? ""}`, { defaultValue: recovery.mode ?? "—" })],
            [t("ops.startup.message"), recovery.message ?? "—"],
          ]} />
          <ul className="ops-list ops-small">
            {(recovery.actions ?? []).map((a, i) => <li key={i}>{a.index ?? a.change} → {a.result} {a.proof?.basis ? `(${a.proof.basis})` : ""}</li>)}
            {(recovery.blocked ?? []).map((b, i) => <li key={`b${i}`} className="ops-text-danger">{b.index} — <Mono>{b.code}</Mono> {b.detail}</li>)}
          </ul>
        </Panel>
      )}
      {!authority && !authz && !change && !compensation && !failure && !recovery && <Empty>{t("ops.authority.none")}</Empty>}
    </div>
  );
}

function Agents({ data }: { data: OperationView }) {
  const { t } = useTranslation();
  const claims = data.claims as { summary: Record<string, number>; by_role: Record<string, { claim: string; verdict: string; relevance: string; evidence: string[];
    assertions: { type: string; subject: string; result: string; detail: string }[] }[]>; method: string } | null;
  if (!claims) return <Empty>{t("ops.agents.none")}</Empty>;
  return (
    <div className="ops-stack">
      <Panel title={t("ops.agents.title")}>
        <p className="ops-help">{t("ops.agents.note")}</p>
        <KV items={[
          [t("ops.agents.verified"), claims.summary.verified ?? 0],
          [t("ops.agents.contradicted"), claims.summary.contradicted ?? 0],
          [t("ops.agents.unverified"), claims.summary.unverified ?? 0],
          [t("ops.agents.irrelevant"), claims.summary.irrelevant_citations ?? 0],
        ]} />
      </Panel>
      {Object.entries(claims.by_role).map(([role, items]) => (
        <Panel key={role} title={t(`ops.agents.role.${role}`, { defaultValue: role })}>
          {items.length === 0 ? <Empty>{t("ops.agents.noClaims")}</Empty> : (
            <ul className="ops-claims">
              {items.map((c, i) => (
                <li key={i}>
                  <StatusChip value={c.verdict} size="sm" /> <span className="ops-muted ops-small">{t(`ops.agents.relevance.${c.relevance}`, { defaultValue: c.relevance })}</span>
                  <p className="ops-claim-text">{c.claim}</p>
                  <ul className="ops-small">
                    {c.assertions.map((a, j) => <li key={j}><StatusChip value={a.result} size="sm" /> <Mono>{a.type}</Mono> {a.subject} — {a.detail}</li>)}
                  </ul>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      ))}
    </div>
  );
}

function Evidence({ data }: { data: OperationView }) {
  const { t } = useTranslation();
  const op = data.operation;
  const [verification, setVerification] = useState<Verification | null>(null);
  const [report, setReport] = useState<string | null>(null);
  const verify = useMutation({ mutationFn: () => dbApi.verify(op.operation_id), onSuccess: setVerification });
  const exporter = useMutation({ mutationFn: () => dbApi.evidence(op.operation_id), onSuccess: (blob) => downloadBlob(blob, `${op.operation_id}-evidence.zip`) });
  const loadReport = useMutation({ mutationFn: () => dbApi.report(op.operation_id), onSuccess: setReport });
  const integrity = verification?.integrity as { all_intact?: boolean; files?: { name: string; integrity: string }[] } | undefined;
  const recomputation = verification?.recomputation as { available?: boolean; matches?: boolean; reason?: string; recorded?: { outcome?: string }; recomputed?: { outcome?: string };
    differences?: { criterion: string; recorded?: string; recomputed?: string }[] } | undefined;
  const byName = new Map((integrity?.files ?? []).map((f) => [f.name, f.integrity]));
  return (
    <div className="ops-stack">
      <Panel title={t("ops.evidence.files")} actions={
        <div className="ops-inline">
          <button type="button" className="ops-btn" onClick={() => verify.mutate()} disabled={verify.isPending}>{t("ops.evidence.verify")}</button>
          <button type="button" className="ops-btn" onClick={() => exporter.mutate()} disabled={exporter.isPending}>{t("ops.evidence.export")}</button>
          {data.report_available && <button type="button" className="ops-btn" onClick={() => loadReport.mutate()}>{t("ops.evidence.report")}</button>}
        </div>
      }>
        <p className="ops-help">{t("ops.evidence.note")}</p>
        <div className="ops-table-wrap">
          <table className="ops-table">
            <caption className="sr-only">{t("ops.evidence.files")}</caption>
            <thead><tr><th scope="col">{t("ops.evidence.name")}</th><th scope="col">{t("ops.evidence.kind")}</th><th scope="col" className="ops-num">{t("ops.evidence.bytes")}</th>
              <th scope="col">sha256</th><th scope="col">{t("ops.evidence.integrity")}</th></tr></thead>
            <tbody>
              {(data.artifacts as { name: string; kind: string; bytes: number; sha256: string }[]).map((a) => (
                <tr key={a.name}>
                  <td><Mono>{a.name}</Mono></td><td>{a.kind}</td><td className="ops-num">{formatBytes(a.bytes)}</td>
                  <td><Mono title={a.sha256}>{shortHash(a.sha256, 16)}</Mono></td>
                  <td>{byName.has(a.name) ? <StatusChip value={byName.get(a.name)} size="sm" /> : <span className="ops-muted">{t("ops.evidence.notChecked")}</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <ErrorBox error={verify.error ?? exporter.error ?? loadReport.error} />
      </Panel>
      {verification && (
        <Panel title={t("ops.evidence.verification")}>
          <KV items={[
            [t("ops.evidence.integrityAll"), <Verdict value={integrity?.all_intact ?? null} yes={t("ops.evidence.intact")} no={t("ops.evidence.altered")} unknown="—" />],
            [t("ops.evidence.recomputation"), recomputation?.available
              ? <><Verdict value={recomputation.matches ?? null} yes={t("ops.evidence.matches")} no={t("ops.evidence.differs")} unknown="—" /> {t("ops.evidence.recordedVsRecomputed", { recorded: recomputation.recorded?.outcome, recomputed: recomputation.recomputed?.outcome })}</>
              : <span className="ops-muted">{recomputation?.reason}</span>],
            [t("ops.evidence.attestation"), <span className="ops-chip ops-chip--sm ops-chip--warning"><span className="ops-chip-glyph" aria-hidden="true">?</span>{t("ops.evidence.noAttestation")}</span>],
          ]} />
          {(recomputation?.differences ?? []).length > 0 && (
            <ul className="ops-list ops-small">
              {recomputation?.differences?.map((d) => <li key={d.criterion}><Mono>{d.criterion}</Mono>: {d.recorded} → {d.recomputed}</li>)}
            </ul>
          )}
          <p className="ops-help">{t("ops.evidence.meaning")}</p>
        </Panel>
      )}
      {report && (
        <Panel title={t("ops.evidence.report")}>
          <pre className="ops-pre">{report}</pre>
        </Panel>
      )}
    </div>
  );
}
