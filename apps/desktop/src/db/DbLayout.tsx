import type { UseQueryResult } from "@tanstack/react-query";
import { Link, Outlet } from "react-router-dom";
import { useTranslation } from "react-i18next";

import type { Overview } from "./api";
import { TargetContext, useDbEventRefresh, useOverview, useSelectedTarget, useTargetSelectionState } from "./hooks";
import { Age, ErrorBox, Mono } from "./kit";
import { StatusChip, Verdict } from "./status";
import "./db.css";

/** Every operations screen sits under this frame: one overview poll, one target selection, one context bar. */
export function DbLayout() {
  const overview = useOverview();
  useDbEventRefresh();
  const names = overview.data?.targets.map((t) => t.name) ?? [];
  const selection = useTargetSelectionState(names);
  return (
    <TargetContext.Provider value={selection}>
      <div className="ops-workstation">
        <ContextBar overview={overview} />
        <div className="ops-content">
          <Outlet />
        </div>
      </div>
    </TargetContext.Provider>
  );
}

function ContextBar({ overview }: { overview: UseQueryResult<Overview> }) {
  const { t } = useTranslation();
  const { target, setTarget } = useSelectedTarget();
  const data = overview.data;
  const card = data?.targets.find((x) => x.name === target);
  const live = card?.live;
  const running = data?.running.find((op) => op.target === target);
  if (overview.isError && !data) {
    return (
      <div className="ops-contextbar" role="region" aria-label={t("ops.context.label")}>
        <ErrorBox error={overview.error} />
      </div>
    );
  }
  return (
    <div className="ops-contextbar" role="region" aria-label={t("ops.context.label")}>
      <div className="ops-ctx-item">
        <span className="ops-ctx-label" id="ctx-target-label">{t("ops.context.target")}</span>
        {data && data.targets.length > 1 ? (
          <select aria-labelledby="ctx-target-label" value={target ?? ""} onChange={(e) => setTarget(e.target.value)} className="ops-select">
            {data.targets.map((x) => <option key={x.name} value={x.name}>{x.name}</option>)}
          </select>
        ) : (
          <strong>{target ?? (overview.isLoading ? "…" : t("ops.context.noTarget"))}</strong>
        )}
      </div>
      <div className="ops-ctx-item">
        <span className="ops-ctx-label">{t("ops.context.environment")}</span>
        {card ? (
          <span className={`ops-chip ops-chip--sm ops-chip--${card.writable ? "info" : "warning"}`}>
            <span className="ops-chip-glyph" aria-hidden="true">{card.writable ? "⚗" : "🔒"}</span>
            {t(`ops.environment.${card.environment}`, { defaultValue: card.environment })}
          </span>
        ) : "—"}
      </div>
      <div className="ops-ctx-item">
        <span className="ops-ctx-label">{t("ops.context.identity")}</span>
        <Verdict value={live?.reachable ? live.identity_verified : null} yes={t("ops.context.verified")} no={t("ops.context.mismatch")}
                 unknown={t("ops.context.unknown")} />
      </div>
      <div className="ops-ctx-item">
        <span className="ops-ctx-label">{t("ops.context.connection")}</span>
        <Verdict value={live ? live.reachable : null} yes={live?.server_version_num ? `PostgreSQL ${Math.floor(live.server_version_num / 10000)}.${live.server_version_num % 100}` : t("ops.context.reachable")}
                 no={t("ops.context.unreachable")} unknown={t("ops.context.unknown")} />
      </div>
      <div className="ops-ctx-item">
        <span className="ops-ctx-label">{t("ops.context.freshness")}</span>
        <Age iso={live?.checked_at} />
      </div>
      <div className="ops-ctx-item">
        <span className="ops-ctx-label">{t("ops.context.policy")}</span>
        {data ? (
          <Link to="/db/settings" className="ops-link">
            v{data.policy.version} · <Mono title={data.policy.sha256}>{data.policy.sha256.slice(0, 8)}</Mono>
          </Link>
        ) : "—"}
      </div>
      <div className="ops-ctx-item ops-ctx-item--grow">
        <span className="ops-ctx-label">{t("ops.context.operation")}</span>
        {card?.reconciling ? (
          <span className="ops-chip ops-chip--sm ops-chip--info"><span className="ops-chip-glyph" aria-hidden="true">…</span>{t("ops.context.reconciling")}</span>
        ) : running ? (
          <Link to={`/db/operations/${running.operation_id}`} className="ops-link">
            <StatusChip value={running.status} size="sm" /> <Mono>{running.operation_id}</Mono> · {t(`ops.step.${running.state}`, { defaultValue: running.state })}
          </Link>
        ) : (
          <span className="ops-muted">{t("ops.context.idle")}</span>
        )}
      </div>
    </div>
  );
}
