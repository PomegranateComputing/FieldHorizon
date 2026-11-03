import { useTranslation } from "react-i18next";

import { formatMs, formatRatio } from "./kit";

interface Sample { round: number; param: number; server_ms: number; client_ms?: number | null; index_used?: boolean }
interface QuerySeries {
  query_id: string;
  kind?: string;
  weight?: number;
  control?: boolean;
  phases: Record<string, Sample[]>;
  stats?: { median_ratio?: number; baseline?: { median?: number }; after?: { median?: number }; p_faster?: number; p_slower?: number };
}

// Drawn at 1:1 in CSS pixels (the figure is capped at the same width), so text stays text-sized.
const WIDTH = 880;
const ROW = 44;
const LEFT = 230;
const RIGHT = 80;
const BAND = 12;

/** Deterministic vertical spread inside a band, so 32 samples read as a distribution, not a blob. */
function spread(j: number): number {
  return ((j * 7) % 13) / 12 * BAND;
}

function median(values: number[]): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
}

/**
 * Every server-side sample of every query, before and after, on one log axis
 * (a 2000x speed-up and a 5 % write cost must both stay readable). Hollow grey
 * dots = baseline, filled cold dots = after, bars = medians. The table below
 * carries the same numbers for anyone who cannot use the picture.
 */
export function MeasurementStrips({ queries }: { queries: QuerySeries[] }) {
  const { t } = useTranslation();
  const all = queries.flatMap((q) => [...(q.phases.baseline ?? []), ...(q.phases.after ?? [])].map((s) => s.server_ms)).filter((v) => v > 0);
  if (!all.length) return null;
  const lo = Math.log10(Math.min(...all)) - 0.15;
  const hi = Math.log10(Math.max(...all)) + 0.15;
  const x = (ms: number) => LEFT + ((Math.log10(Math.max(ms, 1e-6)) - lo) / (hi - lo)) * (WIDTH - LEFT - RIGHT);
  const height = queries.length * ROW + 34;
  const ticks: number[] = [];
  for (let e = Math.ceil(lo); e <= Math.floor(hi); e += 1) ticks.push(10 ** e);
  const summary = queries.map((q) => `${q.query_id}: ${formatMs(q.stats?.baseline?.median)} → ${formatMs(q.stats?.after?.median)}`).join("; ");
  return (
    <figure className="ops-figure">
      <svg viewBox={`0 0 ${WIDTH} ${height}`} className="ops-strips" role="img" aria-label={`${t("ops.measure.chartLabel")}: ${summary}`}>
        {ticks.map((tick) => (
          <g key={tick}>
            <line x1={x(tick)} x2={x(tick)} y1={4} y2={height - 26} className="ops-strips-grid" />
            <text x={x(tick)} y={height - 10} textAnchor="middle" className="ops-strips-axis">{formatMs(tick)}</text>
          </g>
        ))}
        {queries.map((q, i) => {
          const y = 18 + i * ROW;
          const base = (q.phases.baseline ?? []).map((s) => s.server_ms);
          const after = (q.phases.after ?? []).map((s) => s.server_ms);
          const mb = median(base);
          const ma = median(after);
          return (
            <g key={q.query_id}>
              <text x={8} y={y + 4} className="ops-strips-label">{q.query_id}</text>
              <text x={8} y={y + 18} className="ops-strips-sub">{q.kind}{q.control ? ` · ${t("ops.measure.control")}` : ""} · w={q.weight}</text>
              {base.map((v, j) => <circle key={`b${j}`} cx={x(v)} cy={y - 10 + spread(j)} r={2} className="ops-strips-base" />)}
              {after.map((v, j) => <circle key={`a${j}`} cx={x(v)} cy={y + 8 + spread(j)} r={2} className="ops-strips-after" />)}
              {mb !== null && <line x1={x(mb)} x2={x(mb)} y1={y - 12} y2={y + 4} className="ops-strips-median-base" />}
              {ma !== null && <line x1={x(ma)} x2={x(ma)} y1={y + 6} y2={y + 22} className="ops-strips-median-after" />}
              <text x={WIDTH - 4} y={y + 10} textAnchor="end" className="ops-strips-ratio">{formatRatio(q.stats?.median_ratio)}</text>
            </g>
          );
        })}
      </svg>
      <figcaption className="ops-legend">
        <span><svg width="12" height="12" aria-hidden="true"><circle cx="6" cy="6" r="4" className="ops-strips-base" /></svg> {t("ops.measure.baseline")}</span>
        <span><svg width="12" height="12" aria-hidden="true"><circle cx="6" cy="6" r="4" className="ops-strips-after" /></svg> {t("ops.measure.after")}</span>
        <span>{t("ops.measure.axis")}</span>
      </figcaption>
    </figure>
  );
}
