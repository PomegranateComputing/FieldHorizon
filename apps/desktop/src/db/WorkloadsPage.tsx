import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useOperationsPage, useSelectedTarget, useWorkloads } from "./hooks";
import { Age, Empty, ErrorBox, Mono, Panel, formatRatio } from "./kit";
import { StatusChip } from "./status";

/** Screen E: what is measured (versioned workloads) and what the measurements concluded, per workload. */
export function WorkloadsPage() {
  const { t } = useTranslation();
  const workloads = useWorkloads();
  const { target } = useSelectedTarget();
  const history = useOperationsPage({ target: target ?? undefined, kind: "index_optimization", limit: 50, order: "desc" });
  return (
    <div className="ops-page">
      <header className="ops-page-head">
        <h1 className="ops-page-title">{t("ops.workloads.title")}</h1>
        <p className="ops-page-lede">{t("ops.workloads.lede")}</p>
      </header>
      <ErrorBox error={workloads.error} />
      {!workloads.data ? <Empty>{t("ops.common.loading")}</Empty> : workloads.data.workloads.map((w) => {
        const runs = (history.data?.items ?? []).filter((op) => op.workload === w.file.replace(/\.yaml$/, ""));
        return (
          <Panel key={w.file} title={<>{w.name} <span className="ops-muted ops-small">v{w.version} · <Mono>{w.file}</Mono></span></>}>
            <div className="ops-table-wrap">
              <table className="ops-table">
                <caption className="sr-only">{w.name}</caption>
                <thead><tr><th scope="col">{t("ops.measure.query")}</th><th scope="col">{t("ops.measure.kind")}</th><th scope="col" className="ops-num">{t("ops.measure.weight")}</th>
                  <th scope="col" className="ops-num">{t("ops.workloads.paramSets")}</th><th scope="col" className="ops-num">{t("ops.workloads.repeat")}</th><th scope="col">SQL</th></tr></thead>
                <tbody>
                  {(w.queries as { id: string; kind: string; weight: number; sql: string; control?: boolean; repeat?: number; param_sets?: number }[]).map((q) => (
                    <tr key={q.id}>
                      <td><Mono>{q.id}</Mono></td>
                      <td>{t(`ops.workloads.kind.${q.kind}`, { defaultValue: q.kind })}{q.control ? <> · <span className="ops-chip ops-chip--sm ops-chip--info"><span className="ops-chip-glyph" aria-hidden="true">◇</span>{t("ops.measure.control")}</span></> : null}</td>
                      <td className="ops-num">{q.weight}</td><td className="ops-num">{q.param_sets ?? "—"}</td><td className="ops-num">{q.repeat ?? 1}</td>
                      <td><Mono>{q.sql}</Mono></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <h3 className="ops-subtitle">{t("ops.workloads.history")}</h3>
            {runs.length === 0 ? <Empty>{t("ops.workloads.noRuns")}</Empty> : (
              <ul className="ops-list">
                {runs.slice(0, 6).map((op) => (
                  <li key={op.operation_id} className="ops-inline">
                    <StatusChip value={op.status} size="sm" />
                    <Link className="ops-link" to={`/db/operations/${op.operation_id}`}><Mono>{op.operation_id}</Mono></Link>
                    <span>{op.candidate ?? "—"}</span>
                    <span className="ops-muted">{t("ops.fields.weighted")} {formatRatio(op.weighted_ratio)}</span>
                    <Age iso={op.started_at} />
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        );
      })}
      <Panel title={t("ops.workloads.method")}>
        <ul className="ops-list ops-small">
          {(["warmup", "rounds", "aa", "controls", "vacuum", "settle", "client", "writes"] as const).map((k) => <li key={k}>{t(`ops.workloads.methodItems.${k}`)}</li>)}
        </ul>
      </Panel>
    </div>
  );
}
