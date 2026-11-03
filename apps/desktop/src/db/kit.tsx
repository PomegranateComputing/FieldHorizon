import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";

/** A titled region: one landmark per panel, so screen-reader users can jump between them. */
export function Panel({ title, actions, children, className = "", id }: {
  title: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; id?: string;
}) {
  const generated = useId();
  const headingId = id ?? `panel-${generated}`;
  return (
    <section className={`ops-panel ${className}`} aria-labelledby={headingId}>
      <header className="ops-panel-head">
        <h2 id={headingId} className="ops-panel-title">{title}</h2>
        {actions && <div className="ops-panel-actions">{actions}</div>}
      </header>
      <div className="ops-panel-body">{children}</div>
    </section>
  );
}

export function Tile({ label, children, hint }: { label: ReactNode; children: ReactNode; hint?: ReactNode }) {
  return (
    <div className="ops-tile">
      <div className="ops-tile-label">{label}</div>
      <div className="ops-tile-value">{children}</div>
      {hint && <div className="ops-tile-hint">{hint}</div>}
    </div>
  );
}

export function KV({ items, columns = 1 }: { items: [ReactNode, ReactNode][]; columns?: 1 | 2 }) {
  return (
    <dl className={`ops-kv${columns === 2 ? " ops-kv--2col" : ""}`}>
      {items.map(([k, v], i) => (
        <div key={i} className="ops-kv-row">
          <dt>{k}</dt>
          <dd>{v ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Mono({ children, title }: { children: ReactNode; title?: string }) {
  return <span className="ops-mono" title={title}>{children}</span>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="ops-empty">{children}</p>;
}

/** An API error as the operator needs it: what was refused, the code to search for, and what to do. */
export function ErrorBox({ error }: { error: unknown }) {
  const { t } = useTranslation();
  if (!error) return null;
  const api = error instanceof ApiError ? error : null;
  return (
    <div className="ops-error" role="alert">
      <span className="ops-chip-glyph" aria-hidden="true">✖</span>
      <div>
        <strong>{api ? api.detail || api.code : String((error as Error)?.message ?? error)}</strong>
        {api && (
          <div className="ops-error-meta">
            <Mono>{api.code}</Mono> · {api.remediation}
          </div>
        )}
        {!api && <div className="ops-error-meta">{t("ops.common.unexpected")}</div>}
      </div>
    </div>
  );
}

/** Relative time that stays current, with the exact timestamp on hover. */
export function Age({ iso, prefix }: { iso: string | null | undefined; prefix?: string }) {
  const { t, i18n } = useTranslation();
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((x) => x + 1), 5000);
    return () => clearInterval(id);
  }, []);
  if (!iso) return <span>—</span>;
  const when = new Date(iso.replace(" ", "T"));
  if (Number.isNaN(when.getTime())) return <span>{iso}</span>;
  const seconds = Math.max(0, Math.round((Date.now() - when.getTime()) / 1000));
  const rtf = new Intl.RelativeTimeFormat(i18n.language, { numeric: "auto" });
  const text = seconds < 60 ? rtf.format(-seconds, "second") : seconds < 3600 ? rtf.format(-Math.round(seconds / 60), "minute")
    : seconds < 86400 ? rtf.format(-Math.round(seconds / 3600), "hour") : rtf.format(-Math.round(seconds / 86400), "day");
  return (
    <time dateTime={when.toISOString()} title={when.toLocaleString(i18n.language)}>
      {prefix ? `${prefix} ` : ""}{seconds < 5 ? t("ops.common.justNow") : text}
    </time>
  );
}

export function Duration({ from, to }: { from: string | null | undefined; to: string | null | undefined }) {
  if (!from) return <span>—</span>;
  const start = new Date(from.replace(" ", "T")).getTime();
  const end = to ? new Date(to.replace(" ", "T")).getTime() : Date.now();
  return <span className="ops-mono">{formatSeconds((end - start) / 1000)}</span>;
}

export function formatSeconds(seconds: number): string {
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const m = Math.floor(seconds / 60);
  return `${m} min ${Math.round(seconds - m * 60)} s`;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  if (ms < 1) return `${ms.toFixed(3)} ms`;
  if (ms < 100) return `${ms.toFixed(2)} ms`;
  return `${ms.toFixed(0)} ms`;
}

export function formatRatio(r: number | null | undefined): string {
  if (r === null || r === undefined || !Number.isFinite(r)) return "—";
  return r < 0.01 ? `×${r.toExponential(1)}` : `×${r.toFixed(3)}`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["B", "KiB", "MiB", "GiB"];
  let value = bytes;
  let unit = 0;
  while (Math.abs(value) >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

export function shortHash(hash: string | null | undefined, n = 12): string {
  return hash ? `${hash.slice(0, n)}…` : "—";
}

/**
 * A modal confirmation that is a real dialog: labelled, focus moved in and kept
 * in, Escape cancels, focus returns to the opener. It confirms an intent the
 * backend still checks -- it never replaces a backend check.
 */
export function ConfirmDialog({ open, title, children, confirmLabel, tone = "primary", onConfirm, onCancel }: {
  open: boolean; title: ReactNode; children: ReactNode; confirmLabel: string; tone?: "primary" | "danger";
  onConfirm: () => void; onCancel: () => void;
}) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement>(null);
  const opener = useRef<Element | null>(null);
  const titleId = useId();
  useEffect(() => {
    if (!open) return;
    opener.current = document.activeElement;
    const node = ref.current;
    node?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onCancel();
      }
      if (event.key === "Tab" && node) {
        const focusable = Array.from(node.querySelectorAll<HTMLElement>("button, [href], input, select, textarea"));
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      (opener.current as HTMLElement | null)?.focus?.();
    };
  }, [open, onCancel]);
  if (!open) return null;
  return (
    <div className="ops-dialog-backdrop">
      <div className="ops-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={ref}>
        <h2 id={titleId} className="ops-dialog-title">{title}</h2>
        <div className="ops-dialog-body">{children}</div>
        <div className="ops-dialog-actions">
          <button type="button" className="ops-btn" onClick={onCancel} data-autofocus>{t("ops.common.cancel")}</button>
          <button type="button" className={`ops-btn ops-btn--${tone}`} onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange, label }: {
  tabs: { id: T; label: ReactNode }[]; value: T; onChange: (id: T) => void; label: string;
}) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  function onKeyDown(event: React.KeyboardEvent, index: number) {
    const delta = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!delta) return;
    event.preventDefault();
    const next = tabs[(index + delta + tabs.length) % tabs.length];
    onChange(next.id);
    refs.current[next.id]?.focus();
  }
  return (
    <div className="ops-tabs" role="tablist" aria-label={label}>
      {tabs.map((tab, i) => (
        <button
          key={tab.id}
          ref={(el) => { refs.current[tab.id] = el; }}
          type="button"
          role="tab"
          id={`tab-${tab.id}`}
          aria-selected={tab.id === value}
          aria-controls={`tabpanel-${tab.id}`}
          tabIndex={tab.id === value ? 0 : -1}
          className={`ops-tab${tab.id === value ? " ops-tab--active" : ""}`}
          onClick={() => onChange(tab.id)}
          onKeyDown={(e) => onKeyDown(e, i)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
