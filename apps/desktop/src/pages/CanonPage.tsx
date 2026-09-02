import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { getActiveCanon, getCanonAsOfTimestamp, getRecentCycles, getWhyChanged } from "../api/client";
import type { CanonEntryModel } from "../api/types";
import { CHART_COLORS } from "../charts/EChart";
import "./CanonPage.css";

/** Depth = 1 + max(parent depth); a parent missing from this snapshot (retired, or outside the fetched limit) is treated as a root. */
function computeDepths(entries: CanonEntryModel[]): Map<number, number> {
  const byId = new Map(entries.map((e) => [e.cycle_id, e]));
  const depths = new Map<number, number>();

  function depthOf(id: number, seen: Set<number>): number {
    if (depths.has(id)) return depths.get(id)!;
    if (seen.has(id)) return 0; // cycle guard -- should never happen, but never infinite-loop on bad data
    const entry = byId.get(id);
    if (!entry || entry.parent_cycle_ids.length === 0) {
      depths.set(id, 0);
      return 0;
    }
    const next = new Set(seen).add(id);
    const d = 1 + Math.max(...entry.parent_cycle_ids.map((p) => depthOf(p, next)));
    depths.set(id, d);
    return d;
  }

  for (const entry of entries) depthOf(entry.cycle_id, new Set());
  return depths;
}

function buildGraph(entries: CanonEntryModel[]): { nodes: Node[]; edges: Edge[] } {
  const depths = computeDepths(entries);
  const perDepthCount = new Map<number, number>();
  const nodes: Node[] = entries.map((entry) => {
    const depth = depths.get(entry.cycle_id) ?? 0;
    const column = perDepthCount.get(depth) ?? 0;
    perDepthCount.set(depth, column + 1);
    return {
      id: String(entry.cycle_id),
      type: "canonNode",
      position: { x: depth * 260, y: column * 110 },
      data: { entry },
    };
  });

  const idsPresent = new Set(entries.map((e) => e.cycle_id));
  const edges: Edge[] = entries.flatMap((entry) =>
    entry.parent_cycle_ids
      .filter((parentId) => idsPresent.has(parentId))
      .map((parentId) => ({
        id: `${parentId}-${entry.cycle_id}`,
        source: String(parentId),
        target: String(entry.cycle_id),
        style: { stroke: CHART_COLORS.terminal },
      })),
  );

  return { nodes, edges };
}

function CanonNode({ data, selected }: NodeProps) {
  const entry = (data as { entry: CanonEntryModel }).entry;
  return (
    <div className={`canon-node${selected ? " canon-node--selected" : ""}`}>
      <Handle type="target" position={Position.Left} />
      <div className="type-label">#{entry.cycle_id}</div>
      <div className="canon-node-fragment">{entry.fragment.slice(0, 80)}</div>
      <div className="canon-node-score type-mono">{entry.final_score.toFixed(2)}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const NODE_TYPES = { canonNode: CanonNode };

function WhyChangedPanel({ cycleId }: { cycleId: number }) {
  const { t } = useTranslation();
  const query = useQuery({ queryKey: ["why-changed", cycleId], queryFn: () => getWhyChanged(cycleId) });

  if (query.status === "pending") return <p>{t("command.loading")}</p>;
  if (query.status === "error") return <p>{t("command.unreachable")}</p>;

  return (
    <div className="canon-detail">
      <Link to={`/provenance?id=${cycleId}`}>{t("canon.viewProvenance")}</Link>
      {query.data.transitions.length === 0 ? (
        <p className="canon-empty-inline">{t("canon.noTransitions")}</p>
      ) : (
        <ul className="canon-transition-list type-mono">
          {query.data.transitions.map((t2, i) => (
            <li key={i}>
              {t2.valid_from} -- {t2.from_verdict ?? t("canon.created")} &rarr; {t2.to_verdict}
              {t2.edge_relation_type ? ` (${t2.edge_relation_type})` : ""}
              {!t2.active && <span className="canon-superseded"> [{t("canon.superseded")}]</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function CanonPage() {
  const { t } = useTranslation();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [asOfCycleId, setAsOfCycleId] = useState<string>("");
  const [compareAId, setCompareAId] = useState<string>("");
  const [compareBId, setCompareBId] = useState<string>("");

  const recentCyclesQuery = useQuery({ queryKey: ["recent-cycles-canon"], queryFn: () => getRecentCycles(30) });
  const liveCanonQuery = useQuery({ queryKey: ["canon-active"], queryFn: () => getActiveCanon(100) });

  const asOfEntry = recentCyclesQuery.data?.entries.find((e) => String(e.cycle_id) === asOfCycleId);
  const asOfQuery = useQuery({
    queryKey: ["canon-as-of", asOfEntry?.created_at],
    queryFn: () => getCanonAsOfTimestamp(asOfEntry!.created_at),
    enabled: !!asOfEntry,
  });

  const compareA = recentCyclesQuery.data?.entries.find((e) => String(e.cycle_id) === compareAId);
  const compareB = recentCyclesQuery.data?.entries.find((e) => String(e.cycle_id) === compareBId);
  const compareAQuery = useQuery({
    queryKey: ["canon-as-of", compareA?.created_at],
    queryFn: () => getCanonAsOfTimestamp(compareA!.created_at),
    enabled: !!compareA,
  });
  const compareBQuery = useQuery({
    queryKey: ["canon-as-of", compareB?.created_at],
    queryFn: () => getCanonAsOfTimestamp(compareB!.created_at),
    enabled: !!compareB,
  });

  const displayedEntries = asOfEntry ? asOfQuery.data?.entries : liveCanonQuery.data?.entries;
  const { nodes, edges } = useMemo(() => buildGraph(displayedEntries ?? []), [displayedEntries]);

  const compareDiff = useMemo(() => {
    if (!compareAQuery.data || !compareBQuery.data) return null;
    const aIds = new Set(compareAQuery.data.entries.map((e) => e.cycle_id));
    const bIds = new Set(compareBQuery.data.entries.map((e) => e.cycle_id));
    return {
      onlyInA: compareAQuery.data.entries.filter((e) => !bIds.has(e.cycle_id)),
      onlyInB: compareBQuery.data.entries.filter((e) => !aIds.has(e.cycle_id)),
      inBoth: compareAQuery.data.entries.filter((e) => bIds.has(e.cycle_id)).length,
    };
  }, [compareAQuery.data, compareBQuery.data]);

  return (
    <div className="section-page canon-page">
      <h1>CANON</h1>

      <div className="canon-toolbar">
        <label className="type-label">
          {t("canon.viewAsOf")}
          <select value={asOfCycleId} onChange={(e) => setAsOfCycleId(e.currentTarget.value)}>
            <option value="">{t("canon.liveOption")}</option>
            {recentCyclesQuery.data?.entries.map((entry) => (
              <option key={entry.cycle_id} value={entry.cycle_id}>
                #{entry.cycle_id} -- {entry.created_at}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="canon-graph-shell panel">
        {(liveCanonQuery.status === "pending" || (asOfEntry && asOfQuery.status === "pending")) && (
          <p>{t("command.loading")}</p>
        )}
        {liveCanonQuery.status === "success" && nodes.length === 0 && <p>{t("canon.noneYet")}</p>}
        {nodes.length > 0 && (
          <div style={{ height: 420, overflow: "hidden", position: "relative" }}>
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={NODE_TYPES}
              onNodeClick={(_, node) => setSelectedId(Number(node.id))}
              fitView
              proOptions={{ hideAttribution: true }}
            >
              <Background />
              <Controls />
            </ReactFlow>
          </div>
        )}
      </div>

      {selectedId !== null && (
        <section className="panel canon-panel">
          <div className="type-label">
            {t("canon.history")} #{selectedId}
          </div>
          <WhyChangedPanel cycleId={selectedId} />
        </section>
      )}

      <section className="panel canon-panel">
        <div className="type-label">{t("canon.versionCompare")}</div>
        <div className="canon-compare-form">
          <select value={compareAId} onChange={(e) => setCompareAId(e.currentTarget.value)}>
            <option value="">{t("canon.pickPointA")}</option>
            {recentCyclesQuery.data?.entries.map((entry) => (
              <option key={entry.cycle_id} value={entry.cycle_id}>
                #{entry.cycle_id} -- {entry.created_at}
              </option>
            ))}
          </select>
          <select value={compareBId} onChange={(e) => setCompareBId(e.currentTarget.value)}>
            <option value="">{t("canon.pickPointB")}</option>
            {recentCyclesQuery.data?.entries.map((entry) => (
              <option key={entry.cycle_id} value={entry.cycle_id}>
                #{entry.cycle_id} -- {entry.created_at}
              </option>
            ))}
          </select>
        </div>
        {compareDiff ? (
          <div className="canon-compare-result type-mono">
            <p>{t("canon.inBoth", { count: compareDiff.inBoth })}</p>
            {compareDiff.onlyInA.length > 0 && (
              <div>
                <div className="type-label">{t("canon.onlyInA")}</div>
                <ul>
                  {compareDiff.onlyInA.map((e) => (
                    <li key={e.cycle_id}>#{e.cycle_id} -- {e.query.slice(0, 50)}</li>
                  ))}
                </ul>
              </div>
            )}
            {compareDiff.onlyInB.length > 0 && (
              <div>
                <div className="type-label">{t("canon.onlyInB")}</div>
                <ul>
                  {compareDiff.onlyInB.map((e) => (
                    <li key={e.cycle_id}>#{e.cycle_id} -- {e.query.slice(0, 50)}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        ) : (
          <p className="canon-empty-inline">{t("canon.pickTwoPoints")}</p>
        )}
      </section>
    </div>
  );
}
