import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { getSchoolDistances, getSchoolsAllRuns, postRunSchools } from "../api/client";
import type { SchoolRunEntryModel } from "../api/types";
import { useEventStreamContext } from "../shell/EventStreamContext";
import { useInspectorSelection } from "../shell/InspectorContext";
import "./SchoolsPage.css";

function DistancesPanel({ runAt }: { runAt: string }) {
  const { t } = useTranslation();
  const distancesQuery = useQuery({ queryKey: ["school-distances", runAt], queryFn: () => getSchoolDistances(runAt) });

  if (distancesQuery.status === "pending") return <p>{t("command.loading")}</p>;
  if (distancesQuery.status === "error") return <p>{t("command.unreachable")}</p>;
  if (distancesQuery.data.distances.length === 0) return <p className="schools-empty-inline">{t("schools.noDistances")}</p>;

  return (
    <table className="schools-distances-table type-mono">
      <tbody>
        {distancesQuery.data.distances.map((d) => (
          <tr key={`${d.school_a_id}-${d.school_b_id}`}>
            <td>#{d.school_a_id}</td>
            <td>&harr;</td>
            <td>#{d.school_b_id}</td>
            <td>{d.distance.toFixed(3)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RunSection({
  runAt,
  schools,
  isLatest,
}: {
  runAt: string;
  schools: SchoolRunEntryModel[];
  isLatest: boolean;
}) {
  const { t } = useTranslation();
  const { select, selection } = useInspectorSelection();
  const [showDistances, setShowDistances] = useState(false);

  return (
    <section className="panel schools-run-section">
      <div className="schools-run-header">
        <div className="type-label">
          {t("schools.run")} {runAt} {isLatest && <span className="schools-run-active-badge">{t("schools.active")}</span>}
        </div>
        <button type="button" onClick={() => setShowDistances((v) => !v)}>
          {showDistances ? t("schools.hideDistances") : t("schools.showDistances")}
        </button>
      </div>

      {showDistances && <DistancesPanel runAt={runAt} />}

      <ul className="schools-list">
        {schools.map((school) => {
          const isSelected = selection?.kind === "school" && selection.id === school.id;
          return (
            <li key={school.id}>
              <button
                type="button"
                className={`schools-card${isSelected ? " schools-card--selected" : ""}`}
                onClick={() => select({ kind: "school", id: school.id })}
              >
                <div className="schools-card-header type-mono">
                  <span className="type-label">#{school.id}</span>
                  <span className="schools-card-name">{school.name}</span>
                  <span>{t("schools.memberCount", { count: school.member_count })}</span>
                </div>
                <p className="schools-card-summary">{school.summary}</p>
                {school.previous_school_id !== null && (
                  <div className="schools-lineage type-label">
                    {t("schools.formerly")} #{school.previous_school_id}
                  </div>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function SchoolsPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { events } = useEventStreamContext();

  const schoolsQuery = useQuery({ queryKey: ["schools-all-runs"], queryFn: getSchoolsAllRuns });

  const [runId, setRunId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set directly from the (synchronous, already-complete-by-the-time-it-
  // resolves) POST response, not derived from the SSE stream: that stream
  // is polled every 0.5s (server.py's _SSE_POLL_INTERVAL_SECONDS), so
  // waiting for a SchoolsCompleted *event* to arrive before showing "done"
  // races the HTTP response and can lose -- the live named-count below
  // still reads real events, just not the terminal state.
  const [completedCount, setCompletedCount] = useState<number | null>(null);

  const runEvents = useMemo(() => (runId ? events.filter((e) => e.run_id === runId) : []), [events, runId]);
  const namedEvents = runEvents.filter((e) => e.event_type === "SchoolNamed");
  const started = runEvents.some((e) => e.event_type === "SchoolsStarted");

  async function handleRun() {
    const correlationId = crypto.randomUUID();
    setRunId(correlationId);
    setRunning(true);
    setError(null);
    setCompletedCount(null);
    try {
      const response = await postRunSchools({ k: "auto", seed: 42, model: null, correlation_id: correlationId });
      if (response.schools.length === 0) {
        setError(t("schools.notEnoughCycles"));
      }
      setCompletedCount(response.schools.length);
      await queryClient.invalidateQueries({ queryKey: ["schools-all-runs"] });
      await queryClient.invalidateQueries({ queryKey: ["schools"] });
    } catch (e) {
      setError(String(e));
    } finally {
      setRunning(false);
    }
  }

  const runsByDate = useMemo(() => {
    const data = schoolsQuery.data?.schools ?? [];
    const grouped = new Map<string, SchoolRunEntryModel[]>();
    for (const school of data) {
      const list = grouped.get(school.run_at) ?? [];
      list.push(school);
      grouped.set(school.run_at, list);
    }
    return Array.from(grouped.entries()).sort((a, b) => b[0].localeCompare(a[0]));
  }, [schoolsQuery.data]);

  return (
    <div className="section-page schools-page">
      <h1>SCHOOLS</h1>

      <div className="schools-toolbar">
        <button type="button" onClick={handleRun} disabled={running}>
          {running ? t("schools.running") : t("schools.runClustering")}
        </button>
        {error && <p className="schools-error">{error}</p>}
      </div>

      {runId && running && (
        <div className="panel schools-progress type-mono">
          {!started && <p>{t("command.loading")}</p>}
          {started && <p>{t("schools.namedCount", { count: namedEvents.length })}</p>}
        </div>
      )}
      {completedCount !== null && completedCount > 0 && (
        <p className="schools-run-complete">{t("schools.runCompleteWithCount", { count: completedCount })}</p>
      )}

      {schoolsQuery.status === "pending" && <p>{t("command.loading")}</p>}
      {schoolsQuery.status === "error" && <p>{t("command.unreachable")}</p>}
      {schoolsQuery.status === "success" && runsByDate.length === 0 && <p>{t("schools.noneYet")}</p>}

      {runsByDate.map(([runAt, schools], index) => (
        <RunSection key={runAt} runAt={runAt} schools={schools} isLatest={index === 0} />
      ))}
    </div>
  );
}
