import { useState } from "react";
import { useTranslation } from "react-i18next";

import { OperationsTable } from "./ExploitationPage";
import { useOperationsPage, useSelectedTarget } from "./hooks";
import { Empty, ErrorBox, Panel } from "./kit";

const STATUSES = ["", "RUNNING", "PAUSED", "RETAINED", "REVERTED", "REJECTED", "NO_CANDIDATE", "BLOCKED", "CANCELLED", "FAILED_RECOVERED",
  "FAILED_NEEDS_RECOVERY", "RECOVERED_REVERTED", "RECOVERY_DONE", "RECOVERY_NOTHING_TO_DO", "RECOVERY_BLOCKED"];
const PAGE = 20;

/** Screen C: every operation and recovery run, filterable, sortable, paginated -- each row opens its sheet. */
export function OperationsPage() {
  const { t } = useTranslation();
  const { target } = useSelectedTarget();
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState("");
  const [text, setText] = useState("");
  const [order, setOrder] = useState<"desc" | "asc">("desc");
  const [offset, setOffset] = useState(0);
  const page = useOperationsPage({ target: target ?? undefined, status, kind, q: text, order, offset, limit: PAGE });
  const total = page.data?.total ?? 0;
  return (
    <div className="ops-page">
      <header className="ops-page-head">
        <h1 className="ops-page-title">{t("ops.operations.title")}</h1>
        <p className="ops-page-lede">{t("ops.operations.lede")}</p>
      </header>
      <Panel title={t("ops.operations.filters")}>
        <form className="ops-filters" onSubmit={(e) => e.preventDefault()} role="search">
          <label className="ops-field">
            <span>{t("ops.fields.status")}</span>
            <select className="ops-select" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
              {STATUSES.map((s) => <option key={s} value={s}>{s ? t(`ops.status.${s}`) : t("ops.operations.any")}</option>)}
            </select>
          </label>
          <label className="ops-field">
            <span>{t("ops.fields.kind")}</span>
            <select className="ops-select" value={kind} onChange={(e) => { setKind(e.target.value); setOffset(0); }}>
              <option value="">{t("ops.operations.any")}</option>
              <option value="index_optimization">{t("ops.kind.index_optimization")}</option>
              <option value="recovery">{t("ops.kind.recovery")}</option>
            </select>
          </label>
          <label className="ops-field ops-field--grow">
            <span>{t("ops.operations.search")}</span>
            <input className="ops-input" type="search" value={text} placeholder="OP-…, orders, timeout…"
                   onChange={(e) => { setText(e.target.value); setOffset(0); }} />
          </label>
          <label className="ops-field">
            <span>{t("ops.operations.sort")}</span>
            <select className="ops-select" value={order} onChange={(e) => setOrder(e.target.value as "asc" | "desc")}>
              <option value="desc">{t("ops.operations.newest")}</option>
              <option value="asc">{t("ops.operations.oldest")}</option>
            </select>
          </label>
        </form>
      </Panel>
      <Panel title={t("ops.operations.results", { count: total })}>
        <ErrorBox error={page.error} />
        {page.data ? <OperationsTable ops={page.data.items} caption={t("ops.operations.title")} /> : <Empty>{t("ops.common.loading")}</Empty>}
        <div className="ops-pager" aria-label={t("ops.operations.pages")}>
          <button type="button" className="ops-btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>{t("ops.operations.previous")}</button>
          <span className="ops-muted" aria-live="polite">{total ? t("ops.operations.range", { from: offset + 1, to: Math.min(total, offset + PAGE), total }) : "—"}</span>
          <button type="button" className="ops-btn" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>{t("ops.operations.next")}</button>
        </div>
      </Panel>
    </div>
  );
}
