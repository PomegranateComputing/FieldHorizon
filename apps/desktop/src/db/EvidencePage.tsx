import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { Verification } from "./api";
import { dbApi } from "./api";
import { useOperationsPage, useSelectedTarget } from "./hooks";
import { Age, Empty, ErrorBox, Mono, Panel, downloadBlob } from "./kit";
import { StatusChip, Verdict } from "./status";

/** Screen F: evidence per operation -- integrity checked on demand, decisions recomputed, redacted export. */
export function EvidencePage() {
  const { t } = useTranslation();
  const { target } = useSelectedTarget();
  const page = useOperationsPage({ target: target ?? undefined, limit: 40, order: "desc" });
  const [results, setResults] = useState<Record<string, Verification>>({});
  const verify = useMutation({ mutationFn: (id: string) => dbApi.verify(id), onSuccess: (v) => setResults((r) => ({ ...r, [v.operation_id]: v })) });
  const exporter = useMutation({ mutationFn: async (id: string) => downloadBlob(await dbApi.evidence(id), `${id}-evidence.zip`) });
  return (
    <div className="ops-page">
      <header className="ops-page-head">
        <h1 className="ops-page-title">{t("ops.evidencePage.title")}</h1>
        <p className="ops-page-lede">{t("ops.evidencePage.lede")}</p>
      </header>
      <Panel title={t("ops.evidencePage.meaning")}>
        <ul className="ops-list ops-small">
          <li><strong>{t("ops.evidencePage.integrity")}</strong> — {t("ops.evidencePage.integrityText")}</li>
          <li><strong>{t("ops.evidencePage.recompute")}</strong> — {t("ops.evidencePage.recomputeText")}</li>
          <li><strong>{t("ops.evidencePage.attestation")}</strong> — {t("ops.evidencePage.attestationText")}</li>
          <li><strong>{t("ops.evidencePage.redaction")}</strong> — {t("ops.evidencePage.redactionText")}</li>
        </ul>
      </Panel>
      <Panel title={t("ops.evidencePage.operations")}>
        <ErrorBox error={page.error ?? verify.error ?? exporter.error} />
        {!page.data ? <Empty>{t("ops.common.loading")}</Empty> : (
          <div className="ops-table-wrap">
            <table className="ops-table">
              <caption className="sr-only">{t("ops.evidencePage.operations")}</caption>
              <thead><tr><th scope="col">{t("ops.fields.status")}</th><th scope="col">{t("ops.fields.operation")}</th><th scope="col">{t("ops.fields.started")}</th>
                <th scope="col">{t("ops.evidence.integrityAll")}</th><th scope="col">{t("ops.evidence.recomputation")}</th><th scope="col">{t("ops.evidencePage.actions")}</th></tr></thead>
              <tbody>
                {page.data.items.map((op) => {
                  const r = results[op.operation_id];
                  const integrity = r?.integrity as { all_intact?: boolean } | undefined;
                  const recomputation = r?.recomputation as { available?: boolean; matches?: boolean; reason?: string } | undefined;
                  return (
                    <tr key={op.operation_id}>
                      <td><StatusChip value={op.status} size="sm" /></td>
                      <td><Link className="ops-link" to={`/db/operations/${op.operation_id}`}><Mono>{op.operation_id}</Mono></Link></td>
                      <td><Age iso={op.started_at} /></td>
                      <td>{r ? <Verdict value={integrity?.all_intact ?? null} yes={t("ops.evidence.intact")} no={t("ops.evidence.altered")} unknown="—" /> : <span className="ops-muted">—</span>}</td>
                      <td>{r ? (recomputation?.available ? <Verdict value={recomputation.matches ?? null} yes={t("ops.evidence.matches")} no={t("ops.evidence.differs")} unknown="—" />
                        : <span className="ops-muted ops-small">{t("ops.evidencePage.notRecomputable")}</span>) : <span className="ops-muted">—</span>}</td>
                      <td className="ops-inline">
                        <button type="button" className="ops-btn ops-btn--sm" onClick={() => verify.mutate(op.operation_id)} disabled={verify.isPending}>{t("ops.evidence.verify")}</button>
                        <button type="button" className="ops-btn ops-btn--sm" onClick={() => exporter.mutate(op.operation_id)} disabled={exporter.isPending}>{t("ops.evidence.export")}</button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
