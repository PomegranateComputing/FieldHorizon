import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router-dom";

import type { TargetCard } from "./api";
import { useOverview, useSelectedTarget, useTargetDetail } from "./hooks";
import { Age, Empty, ErrorBox, KV, Mono, Panel } from "./kit";
import { StatusChip, Verdict } from "./status";

/** Screen B (list): every declared target, what it is, and whether the live server proves it. */
export function TargetsPage() {
  const { t } = useTranslation();
  const overview = useOverview();
  const { setTarget } = useSelectedTarget();
  return (
    <div className="ops-page">
      <header className="ops-page-head">
        <h1 className="ops-page-title">{t("ops.targets.title")}</h1>
        <p className="ops-page-lede">{t("ops.targets.lede")}</p>
      </header>
      <ErrorBox error={overview.error} />
      {!overview.data ? <Empty>{t("ops.common.loading")}</Empty> : (
        <div className="ops-cards">
          {overview.data.targets.map((card) => (
            <TargetCardView key={card.name} card={card} onSelect={() => setTarget(card.name)} />
          ))}
        </div>
      )}
    </div>
  );
}

function TargetCardView({ card, onSelect }: { card: TargetCard; onSelect: () => void }) {
  const { t } = useTranslation();
  const live = card.live;
  return (
    <article className="ops-card" aria-labelledby={`target-${card.name}`}>
      <h2 id={`target-${card.name}`} className="ops-card-title">
        <Link to={`/db/targets/${encodeURIComponent(card.name)}`} className="ops-link" onClick={onSelect}>{card.name}</Link>
      </h2>
      <KV items={[
        [t("ops.context.environment"), t(`ops.environment.${card.environment}`, { defaultValue: card.environment })],
        [t("ops.targets.endpoint"), <Mono>{card.engine} {card.host}:{card.port}/{card.dbname}</Mono>],
        [t("ops.context.identity"), <Verdict value={live?.reachable ? live.identity_verified : null} yes={t("ops.context.verified")} no={t("ops.context.mismatch")} unknown={t("ops.context.unknown")} />],
        [t("ops.context.connection"), <Verdict value={live ? live.reachable : null} yes={t("ops.context.reachable")} no={t("ops.context.unreachable")} unknown={t("ops.context.unknown")} />],
        [t("ops.targets.data"), t(`ops.targets.classification.${card.data_classification}`, { defaultValue: card.data_classification })],
        [t("ops.context.freshness"), <Age iso={live?.checked_at} />],
      ]} />
    </article>
  );
}

type Role = { rolname: string; rolsuper: boolean; rolcreaterole: boolean; rolcreatedb: boolean; rolbypassrls: boolean; rolinherit: boolean; rolcanlogin: boolean; settings: string[] };
type Membership = { member: string; role: string; inherit_option: boolean; set_option: boolean; admin_option: boolean };
type Privilege = { table: string; role: string; select: boolean; insert: boolean; update: boolean; delete: boolean; truncate: boolean };

/** Screen B (detail): pinned vs live identity, and the privilege boundary as the server enforces it right now. */
export function TargetDetailPage() {
  const { t } = useTranslation();
  const { name } = useParams();
  const detail = useTargetDetail(name);
  const data = detail.data;
  if (detail.isError) return <div className="ops-page"><ErrorBox error={detail.error} /></div>;
  if (!data) return <div className="ops-page"><Empty>{t("ops.common.loading")}</Empty></div>;
  const card = data.card;
  const live = card.live;
  const authority = data.authority as { roles: Role[]; memberships: Membership[]; event_triggers: { name: string; event: string; enabled: string; function: string }[];
    dml_guards: { table: string; name: string; enabled: string }[]; tables: { schema: string; table: string; owner: string }[]; privileges: Privilege[] } | null;
  const statements = (data.statements as { status?: string; statements?: { queryid: string; query: string; calls: number; mean_ms: number; total_ms: number }[] } | null);
  const snapshot = data.snapshot as { snapshot_id?: string; captured_at?: string; summary?: { finding_list?: { kind: string; object: string; detail: string }[] } } | null;
  return (
    <div className="ops-page">
      <nav aria-label={t("ops.common.breadcrumb")} className="ops-breadcrumb">
        <Link to="/db/targets" className="ops-link">{t("ops.nav.targets")}</Link> / {card.name}
      </nav>
      <header className="ops-page-head">
        <h1 className="ops-page-title">{card.name}</h1>
        <p className="ops-page-lede"><Mono>{card.engine} {card.host}:{card.port}/{card.dbname}</Mono> · {t(`ops.environment.${card.environment}`, { defaultValue: card.environment })}</p>
      </header>
      <div className="ops-grid-2">
        <Panel title={t("ops.targets.identity")}>
          <KV items={[
            [t("ops.targets.marker"), <><Mono>{card.pinned.instance_marker ?? "—"}</Mono> {live?.instance_marker_live ? (live.instance_marker_live === card.pinned.instance_marker ? "✓" : "✖") : ""}</>],
            [t("ops.targets.sysid"), <><Mono>{card.pinned.system_identifier ?? "—"}</Mono> {live?.system_identifier_live ? (live.system_identifier_live === card.pinned.system_identifier ? "✓" : "✖") : ""}</>],
            [t("ops.context.identity"), <Verdict value={live?.reachable ? live.identity_verified : null} yes={t("ops.context.verified")} no={t("ops.context.mismatch")} unknown={t("ops.context.unknown")} />],
            [t("ops.targets.server"), live?.server_version ?? live?.detail ?? "—"],
            [t("ops.targets.fingerprint"), <Mono>{card.fingerprint}</Mono>],
            [t("ops.targets.data"), t(`ops.targets.classification.${card.data_classification}`, { defaultValue: card.data_classification })],
          ]} />
        </Panel>
        <Panel title={t("ops.targets.identities")}>
          <KV items={[
            [t("ops.targets.observer"), <Mono>{card.identities.observer ?? "—"}</Mono>],
            [t("ops.targets.measure"), <Mono>{card.identities.measure ?? "—"}</Mono>],
            [t("ops.targets.operator"), <Mono>{card.identities.operator ?? "—"}</Mono>],
            [t("ops.targets.owner"), <Mono>{card.identities.owner_role ?? "—"}</Mono>],
          ]} />
          <p className="ops-help">{t("ops.targets.identitiesNote")}</p>
        </Panel>
      </div>
      <Panel title={t("ops.targets.boundary")}>
        <p className="ops-help">{t("ops.targets.boundaryNote")}</p>
        {!authority ? <ErrorBox error={new Error(data.authority_detail ?? t("ops.targets.boundaryUnavailable"))} /> : (
          <div className="ops-stack">
            <div className="ops-table-wrap">
              <table className="ops-table">
                <caption>{t("ops.targets.roles")}</caption>
                <thead><tr><th scope="col">{t("ops.targets.role")}</th><th scope="col">login</th><th scope="col">superuser</th><th scope="col">createrole</th>
                  <th scope="col">bypassrls</th><th scope="col">{t("ops.targets.settings")}</th></tr></thead>
                <tbody>
                  {authority.roles.map((r) => (
                    <tr key={r.rolname}><td><Mono>{r.rolname}</Mono></td><td>{r.rolcanlogin ? "✓" : "—"}</td>
                      <td>{r.rolsuper ? <span className="ops-text-danger">✖ {t("ops.common.yes")}</span> : "—"}</td><td>{r.rolcreaterole ? "✓" : "—"}</td>
                      <td>{r.rolbypassrls ? "✓" : "—"}</td><td><Mono>{r.settings.join(" ") || "—"}</Mono></td></tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="ops-table-wrap">
              <table className="ops-table">
                <caption>{t("ops.targets.memberships")}</caption>
                <thead><tr><th scope="col">{t("ops.targets.member")}</th><th scope="col">{t("ops.targets.of")}</th><th scope="col">INHERIT</th><th scope="col">SET</th><th scope="col">ADMIN</th></tr></thead>
                <tbody>
                  {authority.memberships.map((m) => (
                    <tr key={`${m.member}-${m.role}`}><td><Mono>{m.member}</Mono></td><td><Mono>{m.role}</Mono></td>
                      <td>{m.inherit_option ? "✓" : <span className="ops-text-ok">✖ {t("ops.targets.noInherit")}</span>}</td>
                      <td>{m.set_option ? "✓" : "—"}</td><td>{m.admin_option ? "✓" : "—"}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
            <KV items={[
              [t("ops.targets.ddlGuard"), authority.event_triggers.length ? authority.event_triggers.map((e) => <span key={e.name}><StatusChip value={e.enabled === "D" ? "fail" : "pass"} size="sm" label={`${e.name} (${e.event})`} /> </span>) : <StatusChip value="fail" size="sm" label={t("ops.targets.missing")} />],
              [t("ops.targets.dmlGuard"), authority.dml_guards.length ? authority.dml_guards.map((g) => <span key={g.table}><Mono>{g.table}</Mono> </span>) : <StatusChip value="fail" size="sm" label={t("ops.targets.missing")} />],
              [t("ops.targets.owners"), authority.tables.map((tb) => <span key={tb.table} className="ops-inline-item"><Mono>{tb.schema}.{tb.table}</Mono> → <Mono>{tb.owner}</Mono></span>)],
            ]} />
            <div className="ops-table-wrap">
              <table className="ops-table">
                <caption>{t("ops.targets.privileges")}</caption>
                <thead><tr><th scope="col">{t("ops.targets.table")}</th><th scope="col">{t("ops.targets.role")}</th><th scope="col">SELECT</th><th scope="col">INSERT</th>
                  <th scope="col">UPDATE</th><th scope="col">DELETE</th><th scope="col">TRUNCATE</th></tr></thead>
                <tbody>
                  {authority.privileges.map((p) => (
                    <tr key={`${p.table}-${p.role}`}><td><Mono>{p.table}</Mono></td><td><Mono>{p.role}</Mono></td>
                      {(["select", "insert", "update", "delete", "truncate"] as const).map((k) => <td key={k}>{p[k] ? "✓" : "—"}</td>)}</tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="ops-note ops-note--warning">{t("ops.targets.residual")}</p>
          </div>
        )}
      </Panel>
      <div className="ops-grid-2">
        <Panel title={t("ops.targets.traffic")}>
          <p className="ops-help">{t("ops.targets.trafficNote")}</p>
          {(statements?.statements ?? []).length === 0 ? <Empty>{t("ops.targets.noTraffic")}</Empty> : (
            <ol className="ops-list ops-small">
              {statements?.statements?.slice(0, 8).map((s) => <li key={s.queryid}><Mono>{s.calls}×</Mono> {Number(s.mean_ms).toFixed(2)} ms · <Mono>{s.query.slice(0, 110)}</Mono></li>)}
            </ol>
          )}
        </Panel>
        <Panel title={t("ops.targets.snapshot")}>
          {snapshot ? (
            <>
              <KV items={[[t("ops.targets.snapshotId"), <Mono>{snapshot.snapshot_id}</Mono>], [t("ops.context.freshness"), <Age iso={snapshot.captured_at} />]]} />
              <ul className="ops-list ops-small">
                {(snapshot.summary?.finding_list ?? []).map((f, i) => <li key={i}><Mono>{f.kind}</Mono> {f.object} — {f.detail}</li>)}
              </ul>
            </>
          ) : <Empty>{t("ops.targets.noSnapshot")}</Empty>}
        </Panel>
      </div>
    </div>
  );
}
