import { useMutation } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { postMultiCycle } from "../api/client";
import type { EventEnvelope } from "../api/types";
import { useEventStreamContext } from "../shell/EventStreamContext";
import "./CyclesPage.css";

/**
 * FABLE Sec.10.6/10.7. The launcher exposes only parameters run_multi_cycle
 * really accepts -- workspace/planner-mode/context-budget/seed/tags/notes
 * from FABLE's own field list have no real backing anywhere in this engine
 * and are not here. No Pause button: cycles run synchronously end to end,
 * the backend has no suspend mechanism at all.
 */
const OPTIONAL_MODEL_FIELDS = [
  "model",
  "agent_model",
  "synthesizer_model",
  "critic_model",
  "rewrite_model",
  "interpreter_model",
] as const;

function AgentPanels({ events }: { events: EventEnvelope[] }) {
  const { t } = useTranslation();
  const agentEvents = events.filter((e) => e.event_type === "AgentCompleted");
  if (agentEvents.length === 0) return null;
  return (
    <section className="cycles-panel">
      <div className="type-label">{t("cycles.agents")}</div>
      <div className="cycles-agent-grid">
        {agentEvents.map((event) => (
          <div key={event.event_id} className="cycles-agent-card type-mono">
            <div className="type-label">{String(event.payload.agent_name ?? "?")}</div>
            <div>
              {t("cycles.contentLength")}: {String(event.payload.content_length ?? "--")}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function RetrievalPanel({ events }: { events: EventEnvelope[] }) {
  const { t } = useTranslation();
  const event = events.find((e) => e.event_type === "RetrievalCompleted");
  if (!event) return null;
  return (
    <section className="cycles-panel">
      <div className="type-label">{t("cycles.retrieval")}</div>
      <ul className="type-mono cycles-kv-list">
        <li>
          {t("cycles.bookCount")}: {String(event.payload.book_count ?? "--")}
        </li>
        <li>
          {t("cycles.jsonCount")}: {String(event.payload.json_count ?? "--")}
        </li>
        <li>
          {t("cycles.canonCount")}: {String(event.payload.canon_count ?? "--")}
        </li>
      </ul>
    </section>
  );
}

function EvaluationPanel({ events }: { events: EventEnvelope[] }) {
  const { t } = useTranslation();
  const event = events.find((e) => e.event_type === "EvaluationCompleted");
  if (!event) return null;
  return (
    <section className="cycles-panel">
      <div className="type-label">{t("cycles.evaluation")}</div>
      <ul className="type-mono cycles-kv-list">
        <li>
          {t("cycles.verdict")}: {String(event.payload.verdict ?? "--")}
        </li>
        <li>
          {t("cycles.finalScore")}: {String(event.payload.final_score ?? "--")}
        </li>
        <li>
          {t("cycles.wasRewritten")}: {event.payload.was_rewritten ? t("cycles.yes") : t("cycles.no")}
        </li>
      </ul>
    </section>
  );
}

function RuntimeStats({ events }: { events: EventEnvelope[] }) {
  const { t } = useTranslation();
  const started = events.find((e) => e.event_type === "CycleStarted");
  const finished = events.find((e) => e.event_type === "CycleCompleted" || e.event_type === "CycleFailed");
  if (!started) return null;
  const elapsedMs = finished ? new Date(finished.timestamp).getTime() - new Date(started.timestamp).getTime() : null;
  return (
    <section className="cycles-panel">
      <div className="type-label">{t("cycles.runtime")}</div>
      <ul className="type-mono cycles-kv-list">
        <li>
          {t("cycles.elapsed")}: {elapsedMs !== null ? `${(elapsedMs / 1000).toFixed(2)}s` : t("cycles.inProgress")}
        </li>
        <li>
          {t("cycles.eventCount")}: {events.length}
        </li>
      </ul>
    </section>
  );
}

function Timeline({ events }: { events: EventEnvelope[] }) {
  const { t } = useTranslation();
  return (
    <section className="cycles-panel">
      <div className="type-label">{t("cycles.timeline")}</div>
      <ol className="type-mono cycles-timeline">
        {events.map((event) => (
          <li key={event.event_id}>
            {event.timestamp} -- {event.event_type}
          </li>
        ))}
      </ol>
    </section>
  );
}

export function CyclesPage() {
  const { t } = useTranslation();
  const { events } = useEventStreamContext();

  const [query, setQuery] = useState("");
  const [dryRun, setDryRun] = useState(true);
  const [autoRewrite, setAutoRewrite] = useState(true);
  const [jsonDomain, setJsonDomain] = useState("");
  const [modelFields, setModelFields] = useState<Record<(typeof OPTIONAL_MODEL_FIELDS)[number], string>>({
    model: "",
    agent_model: "",
    synthesizer_model: "",
    critic_model: "",
    rewrite_model: "",
    interpreter_model: "",
  });
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: async () => {
      const correlationId = crypto.randomUUID();
      setActiveRunId(correlationId);
      return postMultiCycle({
        query,
        dry_run: dryRun,
        auto_rewrite: autoRewrite,
        json_domain: jsonDomain.trim() || null,
        correlation_id: correlationId,
        ...Object.fromEntries(OPTIONAL_MODEL_FIELDS.map((key) => [key, modelFields[key].trim() || null])),
      });
    },
  });

  const liveEvents = useMemo(
    () => (activeRunId ? events.filter((e) => e.run_id === activeRunId) : []),
    [events, activeRunId],
  );
  const canonOutcome = liveEvents.find((e) => e.event_type === "CycleCompleted");

  return (
    <div className="section-page cycles-page">
      <h1>CYCLES</h1>

      <form
        className="cycles-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (query.trim()) mutation.mutate();
        }}
      >
        <input
          type="text"
          className="cycles-query-input"
          placeholder={t("cycles.queryPlaceholder")}
          value={query}
          onChange={(event) => setQuery(event.currentTarget.value)}
        />
        <div className="cycles-model-fields">
          {OPTIONAL_MODEL_FIELDS.map((key) => (
            <input
              key={key}
              type="text"
              placeholder={t(`cycles.field.${key}`)}
              value={modelFields[key]}
              onChange={(event) => setModelFields((prev) => ({ ...prev, [key]: event.currentTarget.value }))}
            />
          ))}
          <input
            type="text"
            placeholder={t("cycles.jsonDomain")}
            value={jsonDomain}
            onChange={(event) => setJsonDomain(event.currentTarget.value)}
          />
        </div>
        <label className="type-label cycles-checkbox">
          <input type="checkbox" checked={dryRun} onChange={(event) => setDryRun(event.currentTarget.checked)} />
          {t("cycles.dryRun")}
        </label>
        <label className="type-label cycles-checkbox">
          <input
            type="checkbox"
            checked={autoRewrite}
            onChange={(event) => setAutoRewrite(event.currentTarget.checked)}
          />
          {t("cycles.autoRewrite")}
        </label>
        <button type="submit" disabled={!query.trim() || mutation.isPending}>
          {mutation.isPending ? t("cycles.running") : t("cycles.launch")}
        </button>
      </form>

      {mutation.isError && <p className="cycles-error">{String(mutation.error)}</p>}
      {mutation.isSuccess && (
        <p className="cycles-success">
          {t("cycles.cycleId")}: {mutation.data.cycle_id} --{" "}
          <Link to={`/provenance?id=${mutation.data.cycle_id}`}>{t("cycles.viewProvenance")}</Link>
        </p>
      )}

      {activeRunId && (
        <div className="cycles-live">
          <RuntimeStats events={liveEvents} />
          <RetrievalPanel events={liveEvents} />
          <AgentPanels events={liveEvents} />
          <EvaluationPanel events={liveEvents} />
          {canonOutcome && (
            <section className="cycles-panel cycles-canon-outcome">
              <div className="type-label">{t("cycles.canonOutcome")}</div>
              <div className="type-mono">{String(canonOutcome.payload.verdict ?? "--")}</div>
            </section>
          )}
          <Timeline events={liveEvents} />
        </div>
      )}
    </div>
  );
}
