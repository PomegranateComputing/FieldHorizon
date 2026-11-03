import { useTranslation } from "react-i18next";

/**
 * One status vocabulary for the whole workstation. Colour is never the only
 * carrier: every status is written out and drawn with its own glyph, so it
 * reads the same in greyscale, to a screen reader, and to a colour-blind
 * operator.
 */
export type Tone = "ok" | "warning" | "danger" | "info" | "neutral" | "live";

interface StatusMeta {
  tone: Tone;
  glyph: string;
}

const META: Record<string, StatusMeta> = {
  RUNNING: { tone: "live", glyph: "▶" },
  PAUSED: { tone: "warning", glyph: "❚❚" },
  CANCELLING: { tone: "warning", glyph: "⨯" },
  INTERRUPTED: { tone: "danger", glyph: "⚡" },
  RETAINED: { tone: "ok", glyph: "✓" },
  REVERTED: { tone: "info", glyph: "↺" },
  REJECTED: { tone: "neutral", glyph: "⊘" },
  NO_CANDIDATE: { tone: "neutral", glyph: "∅" },
  BLOCKED: { tone: "warning", glyph: "⛔" },
  CANCELLED: { tone: "neutral", glyph: "⨯" },
  FAILED_RECOVERED: { tone: "warning", glyph: "⚠" },
  FAILED_NEEDS_RECOVERY: { tone: "danger", glyph: "✖" },
  RECOVERED_REVERTED: { tone: "info", glyph: "↺" },
  RECOVERY_DONE: { tone: "ok", glyph: "✓" },
  RECOVERY_NOTHING_TO_DO: { tone: "neutral", glyph: "∅" },
  RECOVERY_BLOCKED: { tone: "danger", glyph: "✖" },
  RECOVERY_DEFERRED: { tone: "info", glyph: "…" },
  pass: { tone: "ok", glyph: "✓" },
  fail: { tone: "danger", glyph: "✖" },
  unverified: { tone: "warning", glyph: "?" },
  completed: { tone: "ok", glyph: "✓" },
  failed: { tone: "danger", glyph: "✖" },
  started: { tone: "live", glyph: "▶" },
  verified: { tone: "ok", glyph: "✓" },
  contradicted: { tone: "danger", glyph: "✖" },
  intact: { tone: "ok", glyph: "✓" },
  modified: { tone: "danger", glyph: "✖" },
  missing: { tone: "danger", glyph: "✖" },
  applying: { tone: "live", glyph: "▶" },
  applied: { tone: "info", glyph: "●" },
  retained: { tone: "ok", glyph: "✓" },
  reverting: { tone: "warning", glyph: "↺" },
  reverted: { tone: "neutral", glyph: "↺" },
  blocking: { tone: "danger", glyph: "✖" },
  warning: { tone: "warning", glyph: "⚠" },
  info: { tone: "info", glyph: "i" },
};

export function statusMeta(value: string | null | undefined): StatusMeta {
  return META[value ?? ""] ?? { tone: "neutral", glyph: "•" };
}

/** A status: glyph + translated word, toned. The raw code is kept in the title for operators who search logs by it. */
export function StatusChip({ value, label, size = "md" }: { value: string | null | undefined; label?: string; size?: "sm" | "md" }) {
  const { t } = useTranslation();
  const meta = statusMeta(value);
  const text = label ?? t(`ops.status.${value}`, { defaultValue: value ?? "—" });
  return (
    <span className={`ops-chip ops-chip--${meta.tone} ops-chip--${size}`} title={value ?? undefined}>
      <span className="ops-chip-glyph" aria-hidden="true">
        {meta.glyph}
      </span>
      {text}
    </span>
  );
}

/** A yes/no/unknown fact, never shown by colour alone. */
export function Verdict({ value, yes, no, unknown }: { value: boolean | null | undefined; yes: string; no: string; unknown: string }) {
  const tone: Tone = value === true ? "ok" : value === false ? "danger" : "warning";
  const glyph = value === true ? "✓" : value === false ? "✖" : "?";
  return (
    <span className={`ops-chip ops-chip--${tone} ops-chip--sm`}>
      <span className="ops-chip-glyph" aria-hidden="true">
        {glyph}
      </span>
      {value === true ? yes : value === false ? no : unknown}
    </span>
  );
}
