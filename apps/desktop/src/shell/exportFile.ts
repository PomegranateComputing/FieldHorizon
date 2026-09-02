import { EXPORT_ATTRIBUTION_LINES } from "./exportAttribution";

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

/** Never merged into `data` directly -- data can be an array or a primitive shape just as often as an object, so the attribution/metadata always lives in its own sibling field instead. */
export function downloadJson(data: unknown, filename: string): void {
  const payload = { generated_at: new Date().toISOString(), attribution: EXPORT_ATTRIBUTION_LINES, data };
  triggerDownload(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }), filename);
}

export function downloadMarkdown(content: string, filename: string): void {
  const full = `${content}\n\n---\n\n${EXPORT_ATTRIBUTION_LINES.join("  \n")}\n`;
  triggerDownload(new Blob([full], { type: "text/markdown" }), filename);
}

function escapeCsvCell(value: string): string {
  return /[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

export function downloadCsv(rows: string[][], filename: string): void {
  const body = rows.map((row) => row.map(escapeCsvCell).join(",")).join("\n");
  const full = `${body}\n\n# ${EXPORT_ATTRIBUTION_LINES.join(" | ")}\n`;
  triggerDownload(new Blob([full], { type: "text/csv" }), filename);
}

/** dataUrl comes from a canvas/chart's own toDataURL()/getDataURL() -- no re-encoding here, just the download plumbing. Attribution is not embeddable in a raster image without redrawing it, so PNG exports rely on the filename + the page they were exported from for provenance instead. */
export function downloadDataUrl(dataUrl: string, filename: string): void {
  const link = document.createElement("a");
  link.href = dataUrl;
  link.download = filename;
  link.click();
}
