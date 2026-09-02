import { Background, Controls, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { getSemanticNeighbors, getSemanticTop } from "../api/client";
import type { SemanticNeighborModel } from "../api/types";
import { CHART_COLORS } from "../charts/EChart";
import "./SemanticsPage.css";

const KINDS = ["domain", "entity", "motif"] as const;
type Kind = (typeof KINDS)[number];

const KIND_COLOR: Record<string, string> = {
  domain: CHART_COLORS.terminal,
  entity: CHART_COLORS.gold,
  motif: CHART_COLORS.pomegranateBright,
};

function FocusNode({ data }: NodeProps) {
  const d = data as { label: string; kind: string; isFocus: boolean; count?: number };
  return (
    <div
      className={`semantics-node${d.isFocus ? " semantics-node--focus" : ""}`}
      style={{ borderColor: KIND_COLOR[d.kind] }}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div className="type-label" style={{ color: KIND_COLOR[d.kind] }}>
        {d.kind}
      </div>
      <div className="semantics-node-label">{d.label}</div>
      {d.count !== undefined && <div className="semantics-node-count type-mono">{d.count}</div>}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

const NODE_TYPES = { focusNode: FocusNode };

function buildNeighborhoodGraph(
  focusKind: string,
  focusLabel: string,
  neighbors: SemanticNeighborModel[],
): { nodes: Node[]; edges: Edge[] } {
  // A 2D grid of columns around the focus, not one long vertical column --
  // keeps the layout compact and readable even at the full 15-neighbor cap.
  const columns = neighbors.length > 6 ? 3 : neighbors.length > 3 ? 2 : 1;
  const rows = Math.ceil(neighbors.length / columns);
  const focusId = `${focusKind}:${focusLabel}`;
  const nodes: Node[] = [
    {
      id: focusId,
      type: "focusNode",
      position: { x: -260, y: (rows * 100) / 2 },
      data: { label: focusLabel, kind: focusKind, isFocus: true },
    },
    ...neighbors.map((n, i) => ({
      id: `${n.kind}:${n.label}`,
      type: "focusNode",
      position: { x: (i % columns) * 190, y: Math.floor(i / columns) * 100 },
      data: { label: n.label, kind: n.kind, isFocus: false, count: n.shared_chunk_count },
    })),
  ];
  const edges: Edge[] = neighbors.map((n) => ({
    id: `${focusId}-${n.kind}:${n.label}`,
    source: focusId,
    target: `${n.kind}:${n.label}`,
    label: String(n.shared_chunk_count),
    style: { stroke: CHART_COLORS.line },
  }));
  return { nodes, edges };
}

export function SemanticsPage() {
  const { t } = useTranslation();
  const [kind, setKind] = useState<Kind>("domain");
  const [focus, setFocus] = useState<{ kind: string; label: string } | null>(null);

  const topQuery = useQuery({ queryKey: ["semantics-top", kind], queryFn: () => getSemanticTop(kind) });
  const neighborsQuery = useQuery({
    queryKey: ["semantics-neighbors", focus?.kind, focus?.label],
    queryFn: () => getSemanticNeighbors(focus!.kind, focus!.label),
    enabled: !!focus,
  });

  const { nodes, edges } = useMemo(() => {
    if (!focus || !neighborsQuery.data) return { nodes: [], edges: [] };
    return buildNeighborhoodGraph(focus.kind, focus.label, neighborsQuery.data.neighbors);
  }, [focus, neighborsQuery.data]);

  return (
    <div className="section-page semantics-page">
      <h1>SEMANTICS</h1>

      <div className="semantics-kind-tabs">
        {KINDS.map((k) => (
          <button
            key={k}
            type="button"
            className={k === kind ? "semantics-kind-tab--active" : ""}
            onClick={() => {
              setKind(k);
              setFocus(null);
            }}
          >
            {t(`semantics.kind.${k}`)}
          </button>
        ))}
      </div>

      <section className="panel semantics-top-panel">
        <div className="type-label">{t("semantics.topNodes")}</div>
        {topQuery.status === "pending" && <p>{t("command.loading")}</p>}
        {topQuery.status === "error" && <p>{t("command.unreachable")}</p>}
        {topQuery.status === "success" && topQuery.data.nodes.length === 0 && (
          <p className="semantics-empty-inline">{t("semantics.noneYet")}</p>
        )}
        <ul className="semantics-top-list">
          {topQuery.data?.nodes.map((node) => (
            <li key={node.label}>
              <button
                type="button"
                className={focus?.kind === node.kind && focus?.label === node.label ? "semantics-top-item--active" : ""}
                onClick={() => setFocus({ kind: node.kind, label: node.label })}
              >
                {node.label} <span className="type-mono">({node.count})</span>
              </button>
            </li>
          ))}
        </ul>
      </section>

      {focus && (
        <section className="panel semantics-graph-panel">
          <div className="type-label">
            {t("semantics.neighborhoodOf")} {focus.label}
          </div>
          {neighborsQuery.status === "pending" && <p>{t("command.loading")}</p>}
          {neighborsQuery.status === "error" && <p>{t("command.unreachable")}</p>}
          {neighborsQuery.data && neighborsQuery.data.neighbors.length === 0 && (
            <p className="semantics-empty-inline">{t("semantics.noNeighbors")}</p>
          )}
          {neighborsQuery.data && neighborsQuery.data.total_neighbor_count > neighborsQuery.data.neighbors.length && (
            <p className="semantics-density-warning">
              {t("semantics.densityWarning", {
                shown: neighborsQuery.data.neighbors.length,
                total: neighborsQuery.data.total_neighbor_count,
              })}
            </p>
          )}
          {nodes.length > 0 && (
            <div style={{ height: 420, overflow: "hidden", position: "relative" }}>
              <ReactFlow
                nodes={nodes}
                edges={edges}
                nodeTypes={NODE_TYPES}
                onNodeClick={(_, node) => {
                  const d = node.data as { label: string; kind: string; isFocus: boolean };
                  if (!d.isFocus) setFocus({ kind: d.kind, label: d.label });
                }}
                fitView
                proOptions={{ hideAttribution: true }}
              >
                <Background />
                <Controls />
              </ReactFlow>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
