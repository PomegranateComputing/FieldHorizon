import * as echarts from "echarts/core";
import { LineChart, RadarChart } from "echarts/charts";
import { GridComponent, LegendComponent, RadarComponent, TitleComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsCoreOption } from "echarts/core";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import { downloadDataUrl } from "../shell/exportFile";

echarts.use([LineChart, RadarChart, GridComponent, LegendComponent, RadarComponent, TitleComponent, TooltipComponent, CanvasRenderer]);

/**
 * FABLE Sec.10.2's "Charts sober (ECharts), garnet/ivory palette, no
 * decoration without data." One thin wrapper, tree-shaken imports (only
 * the chart/component types WEATHER and CANON actually use), no
 * echarts-for-react dependency for something this small. Colors are the
 * real Pomegranate Interactive palette (src/styles/tokens.css) as literal
 * values -- canvas fillStyle doesn't resolve CSS custom properties, so
 * these can't just reference var(--pomegranate) the way component CSS does.
 */
export const CHART_COLORS = {
  ink: "#0f1114",
  bone: "#e6e8eb",
  boneDim: "#b3b9c1",
  ash: "#949ca6",
  pomegranate: "#8f0d2f",
  pomegranateBright: "#ec7385",
  terminal: "#5cbf86",
  gold: "#d8a03a",
  cold: "#69b3d6",
  line: "rgba(214, 222, 232, 0.10)",
} as const;

export const CHART_PALETTE: string[] = [CHART_COLORS.cold, CHART_COLORS.gold, CHART_COLORS.terminal, CHART_COLORS.boneDim];

/**
 * exportFilename is opt-in per chart (FABLE Sec.18's per-content-type PNG
 * export) -- WEATHER's 7 small sparklines don't each need their own export
 * button, but the one radar chart genuinely worth saving does. getDataURL()
 * is ECharts' own built-in raster export; no extra library needed the way
 * a React Flow graph screenshot would require.
 */
export function EChart({
  option,
  height = 200,
  exportFilename,
}: {
  option: EChartsCoreOption;
  height?: number;
  exportFilename?: string;
}) {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = echarts.init(containerRef.current, null, { renderer: "canvas" });
    chartRef.current = chart;

    const resizeObserver = new ResizeObserver(() => chart.resize());
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(option, true);
  }, [option]);

  return (
    <div>
      <div ref={containerRef} style={{ width: "100%", height }} />
      {exportFilename && (
        <button
          type="button"
          className="echart-export-button"
          onClick={() => {
            const dataUrl = chartRef.current?.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: CHART_COLORS.ink });
            if (dataUrl) downloadDataUrl(dataUrl, exportFilename);
          }}
        >
          {t("weather.exportChartPng")}
        </button>
      )}
    </div>
  );
}
