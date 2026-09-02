import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { CHART_COLORS, CHART_PALETTE, EChart } from "../charts/EChart";
import { getRecentCycles, getWeather, getWeatherAsOf } from "../api/client";
import "./WeatherPage.css";

type Scope = "canon" | "surface";

function axisLabel(t: (key: string) => string, axis: string): string {
  const key = `weather.axis.${axis}`;
  const translated = t(key);
  return translated === key ? axis : translated;
}

function Sparkline({ axis, values, label }: { axis: string; values: number[]; label: string }) {
  const last = values.length > 0 ? values[values.length - 1] : null;
  const option = useMemo(
    () => ({
      grid: { left: 0, right: 0, top: 4, bottom: 0 },
      xAxis: { type: "category", show: false, data: values.map((_, i) => i) },
      yAxis: { type: "value", show: false, min: -1, max: 1 },
      series: [
        {
          type: "line",
          data: values,
          showSymbol: false,
          lineStyle: { color: CHART_COLORS.terminal, width: 1.5 },
          areaStyle: { color: CHART_COLORS.terminal, opacity: 0.08 },
        },
      ],
      tooltip: { show: false },
    }),
    [values],
  );

  return (
    <div className="weather-sparkline-tile panel" key={axis}>
      <div className="type-label">{label}</div>
      <div className="weather-sparkline-value type-mono">{last !== null ? last.toFixed(3) : "--"}</div>
      {values.length > 1 ? <EChart option={option} height={40} /> : <p className="weather-empty-inline">--</p>}
    </div>
  );
}

function CanonSurfaceRadar({
  axes,
  canon,
  surface,
  bySchool,
  t,
}: {
  axes: string[];
  canon: Record<string, number>;
  surface: Record<string, number>;
  bySchool: { school_id: number; name: string; readings: Record<string, number> }[];
  t: (key: string) => string;
}) {
  const indicator = axes.map((a) => ({ name: axisLabel(t, a), min: -1, max: 1 }));
  const series = [
    { name: t("weather.deep"), value: axes.map((a) => canon[a] ?? 0) },
    { name: t("weather.surface"), value: axes.map((a) => surface[a] ?? 0) },
    ...bySchool.map((s) => ({ name: s.name, value: axes.map((a) => s.readings[a] ?? 0) })),
  ];
  const option = {
    color: CHART_PALETTE,
    legend: { top: 0, textStyle: { color: CHART_COLORS.boneDim, fontFamily: "monospace", fontSize: 11 } },
    radar: {
      indicator,
      axisName: { color: CHART_COLORS.boneDim, fontSize: 10 },
      splitLine: { lineStyle: { color: CHART_COLORS.line } },
      axisLine: { lineStyle: { color: CHART_COLORS.line } },
      splitArea: { show: false },
    },
    series: [
      {
        type: "radar",
        data: series.map((s) => ({ name: s.name, value: s.value })),
        lineStyle: { width: 1.5 },
        areaStyle: { opacity: 0.05 },
        symbolSize: 3,
      },
    ],
    tooltip: {},
  };
  return <EChart option={option} height={340} exportFilename="field-horizon-weather-radar.png" />;
}

function HistoryChart({ axes, history, t }: { axes: string[]; history: Record<string, number[]>; t: (key: string) => string }) {
  const maxLen = Math.max(0, ...axes.map((a) => (history[a] ?? []).length));
  const option = {
    color: [...CHART_PALETTE, CHART_COLORS.pomegranate, CHART_COLORS.gold, CHART_COLORS.ash, CHART_COLORS.bone, "#5b7fb6"],
    legend: { top: 0, textStyle: { color: CHART_COLORS.boneDim, fontFamily: "monospace", fontSize: 11 } },
    grid: { left: 40, right: 16, top: 32, bottom: 24 },
    xAxis: {
      type: "category",
      data: Array.from({ length: maxLen }, (_, i) => i + 1),
      name: t("weather.readingIndex"),
      nameLocation: "middle",
      nameGap: 22,
      nameTextStyle: { color: CHART_COLORS.ash, fontSize: 10 },
      axisLine: { lineStyle: { color: CHART_COLORS.line } },
      axisLabel: { color: CHART_COLORS.ash, fontSize: 10 },
    },
    yAxis: {
      type: "value",
      min: -1,
      max: 1,
      axisLine: { lineStyle: { color: CHART_COLORS.line } },
      splitLine: { lineStyle: { color: CHART_COLORS.line } },
      axisLabel: { color: CHART_COLORS.ash, fontSize: 10 },
    },
    series: axes.map((axis) => ({
      name: axisLabel(t, axis),
      type: "line",
      // Canon-scope history is sparse (one new reading per council, not
      // per cycle) -- showSymbol:false would make a lone point invisible.
      showSymbol: true,
      symbolSize: 4,
      data: history[axis] ?? [],
    })),
    tooltip: { trigger: "axis" },
  };
  return <EChart option={option} height={280} exportFilename="field-horizon-weather-history.png" />;
}

export function WeatherPage() {
  const { t } = useTranslation();
  const weatherQuery = useQuery({ queryKey: ["weather"], queryFn: getWeather });
  const recentCyclesQuery = useQuery({ queryKey: ["recent-cycles-weather"], queryFn: () => getRecentCycles(20) });

  const [scope, setScope] = useState<Scope>("surface");
  const [cycleAId, setCycleAId] = useState<string>("");
  const [cycleBId, setCycleBId] = useState<string>("");

  const cycleA = recentCyclesQuery.data?.entries.find((e) => String(e.cycle_id) === cycleAId);
  const cycleB = recentCyclesQuery.data?.entries.find((e) => String(e.cycle_id) === cycleBId);

  const compareAQuery = useQuery({
    queryKey: ["weather-as-of", cycleA?.created_at],
    queryFn: () => getWeatherAsOf(cycleA!.created_at),
    enabled: !!cycleA,
  });
  const compareBQuery = useQuery({
    queryKey: ["weather-as-of", cycleB?.created_at],
    queryFn: () => getWeatherAsOf(cycleB!.created_at),
    enabled: !!cycleB,
  });

  if (weatherQuery.status === "pending") {
    return (
      <div className="section-page">
        <h1>WEATHER</h1>
        <p>{t("command.loading")}</p>
      </div>
    );
  }
  if (weatherQuery.status === "error") {
    return (
      <div className="section-page">
        <h1>WEATHER</h1>
        <p>{t("command.unreachable")}</p>
      </div>
    );
  }

  const data = weatherQuery.data;
  const axes = Array.from(new Set([...Object.keys(data.canon), ...Object.keys(data.surface_history ?? {})])).sort();
  const historyAxes = Array.from(new Set(Object.keys((scope === "canon" ? data.canon_history : data.surface_history) ?? {}))).sort();

  return (
    <div className="section-page weather-page">
      <h1>WEATHER</h1>

      <section className="weather-sparkline-grid">
        {axes.length === 0 && <p>{t("weather.noReadings")}</p>}
        {axes.map((axis) => (
          <Sparkline
            key={axis}
            axis={axis}
            values={data.surface_history?.[axis] ?? []}
            label={axisLabel(t, axis)}
          />
        ))}
      </section>

      <section className="panel weather-panel">
        <div className="type-label">{t("weather.deepVsSurface")}</div>
        {axes.length === 0 ? (
          <p>{t("weather.noReadings")}</p>
        ) : (
          <CanonSurfaceRadar axes={axes} canon={data.canon} surface={data.surface} bySchool={data.by_school ?? []} t={t} />
        )}
        {(data.by_school ?? []).length === 0 && <p className="weather-empty-inline">{t("weather.noSchoolsYet")}</p>}
      </section>

      <section className="panel weather-panel">
        <div className="type-label weather-timeline-header">
          {t("weather.historicalTimeline")}
          <div className="weather-scope-toggle type-label">
            <button type="button" className={scope === "surface" ? "weather-scope--active" : ""} onClick={() => setScope("surface")}>
              {t("weather.surface")}
            </button>
            <button type="button" className={scope === "canon" ? "weather-scope--active" : ""} onClick={() => setScope("canon")}>
              {t("weather.deep")}
            </button>
          </div>
        </div>
        {historyAxes.length === 0 ? (
          <p>{t("weather.noReadings")}</p>
        ) : (
          <HistoryChart axes={historyAxes} history={(scope === "canon" ? data.canon_history : data.surface_history) ?? {}} t={t} />
        )}
      </section>

      <section className="panel weather-panel">
        <div className="type-label">{t("weather.cycleComparison")}</div>
        <div className="weather-compare-form">
          <select value={cycleAId} onChange={(e) => setCycleAId(e.currentTarget.value)}>
            <option value="">{t("weather.pickCycleA")}</option>
            {recentCyclesQuery.data?.entries.map((entry) => (
              <option key={entry.cycle_id} value={entry.cycle_id}>
                #{entry.cycle_id} -- {entry.query.slice(0, 30)}
              </option>
            ))}
          </select>
          <select value={cycleBId} onChange={(e) => setCycleBId(e.currentTarget.value)}>
            <option value="">{t("weather.pickCycleB")}</option>
            {recentCyclesQuery.data?.entries.map((entry) => (
              <option key={entry.cycle_id} value={entry.cycle_id}>
                #{entry.cycle_id} -- {entry.query.slice(0, 30)}
              </option>
            ))}
          </select>
        </div>
        {cycleA && cycleB && compareAQuery.data && compareBQuery.data ? (
          Object.keys(compareAQuery.data.canon).length === 0 && Object.keys(compareBQuery.data.canon).length === 0 ? (
            <p>{t("weather.noReadingsAtThatTime")}</p>
          ) : (
            <EChart
              height={340}
              option={{
                color: CHART_PALETTE,
                legend: { top: 0, textStyle: { color: CHART_COLORS.boneDim, fontFamily: "monospace", fontSize: 11 } },
                radar: {
                  indicator: axes.map((a) => ({ name: axisLabel(t, a), min: -1, max: 1 })),
                  axisName: { color: CHART_COLORS.boneDim, fontSize: 10 },
                  splitLine: { lineStyle: { color: CHART_COLORS.line } },
                  axisLine: { lineStyle: { color: CHART_COLORS.line } },
                  splitArea: { show: false },
                },
                series: [
                  {
                    type: "radar",
                    data: [
                      { name: `#${cycleA.cycle_id}`, value: axes.map((a) => compareAQuery.data.canon[a] ?? 0) },
                      { name: `#${cycleB.cycle_id}`, value: axes.map((a) => compareBQuery.data.canon[a] ?? 0) },
                    ],
                    lineStyle: { width: 1.5 },
                    areaStyle: { opacity: 0.05 },
                    symbolSize: 3,
                  },
                ],
                tooltip: {},
              }}
            />
          )
        ) : (
          <p className="weather-empty-inline">{t("weather.pickTwoCycles")}</p>
        )}
      </section>
    </div>
  );
}
