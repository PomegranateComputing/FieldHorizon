import { useQueries, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  getContradictions,
  getRecentCycles,
  getRecentEvents,
  getRecentEventsByType,
  getSchools,
  getStatus,
  getSystemInfo,
  getWeather,
} from "../api/client";
import type { EventModel } from "../api/types";
import { deriveActiveCycles } from "../hooks/deriveActiveCycles";
import { useCapabilities } from "../hooks/useCapabilities";
import { useHealth } from "../hooks/useHealth";
import { useEventStreamContext } from "../shell/EventStreamContext";
import "./CommandPage.css";

/**
 * FABLE Sec.10.2. Two of the field list's tiles are deliberately absent, not
 * omitted by oversight: "files d'exécution" (execution queues) has no real
 * backing -- the engine serializes /cycle behind a single lock, not a queue,
 * so this shows busy/idle instead of a fabricated depth. "Liens sémantiques"
 * has no referent anywhere in the codebase at all (provenance_edges is
 * lineage, not a similarity graph) -- inventing a count for it would be
 * exactly the kind of fake facade FABLE Sec.3.2 forbids, so it's not here.
 */
const FAILURE_EVENT_TYPES = ["CycleFailed", "CouncilFailed", "DreamFailed"];

function StatTile({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="command-stat-tile panel">
      <div className="type-label">{label}</div>
      <div className="command-stat-value type-mono">{value}</div>
    </div>
  );
}

export function CommandPage() {
  const { t } = useTranslation();
  const health = useHealth();
  const capabilities = useCapabilities();
  const { events } = useEventStreamContext();

  const statusQuery = useQuery({ queryKey: ["status"], queryFn: getStatus });
  const systemInfoQuery = useQuery({ queryKey: ["system-info"], queryFn: getSystemInfo });
  const recentCyclesQuery = useQuery({ queryKey: ["recent-cycles"], queryFn: () => getRecentCycles(8) });
  const recentEventsQuery = useQuery({ queryKey: ["recent-events"], queryFn: () => getRecentEvents(15) });
  const contradictionsQuery = useQuery({ queryKey: ["contradictions"], queryFn: getContradictions });
  const weatherQuery = useQuery({ queryKey: ["weather"], queryFn: getWeather });
  const schoolsQuery = useQuery({ queryKey: ["schools"], queryFn: getSchools });

  const recentErrorsQueries = useQueries({
    queries: FAILURE_EVENT_TYPES.map((eventType) => ({
      queryKey: ["recent-errors", eventType],
      queryFn: () => getRecentEventsByType(eventType, 5),
    })),
  });
  const recentErrors: EventModel[] = recentErrorsQueries
    .flatMap((query) => query.data?.events ?? [])
    .sort((a, b) => b.occurred_at.localeCompare(a.occurred_at))
    .slice(0, 5);

  const activeCycles = deriveActiveCycles(events);
  const plannerModes = capabilities.status === "loaded" ? capabilities.data.capabilities.planner_modes : undefined;
  const plannerLabel = plannerModes?.status === "absent" ? t("topBar.plannerSingleMode") : (plannerModes?.status ?? "--");

  return (
    <div className="section-page command-page">
      <h1>COMMAND</h1>

      <section className="command-tiles">
        <StatTile
          label={t("command.health")}
          value={health.status === "loaded" ? health.data.status : health.status === "error" ? t("command.unreachable") : t("command.loading")}
        />
        <StatTile label={t("command.database")} value={systemInfoQuery.data?.database_path ?? "--"} />
        <StatTile label={t("command.version")} value={systemInfoQuery.data?.version ?? "--"} />
        <StatTile label={t("command.model")} value={systemInfoQuery.data?.default_model ?? "--"} />
        <StatTile label={t("command.sources")} value={statusQuery.data?.sources_count ?? "--"} />
        <StatTile label={t("command.chunks")} value={statusQuery.data?.chunks_count ?? "--"} />
        <StatTile label={t("command.jsonEntries")} value={statusQuery.data?.json_entries_count ?? "--"} />
        <StatTile label={t("command.semanticProfiles")} value={statusQuery.data?.tagged_chunks_count ?? "--"} />
        <StatTile label={t("command.cycles")} value={statusQuery.data?.cycles_total ?? "--"} />
        <StatTile label={t("command.plannerMode")} value={plannerLabel} />
        <StatTile
          label={t("command.executionState")}
          value={activeCycles > 0 ? t("command.busy") : t("command.idle")}
        />
      </section>

      <section className="command-pipeline panel">
        <div className="type-label">{t("command.pipeline")}</div>
        <div className="command-pipeline-strip type-mono">
          {["INTERPRETER", "RETRIEVAL", "AGENTS", "SYNTHESIS", "EVALUATION", "CRITIC/REWRITE", "CANON"].map(
            (stage, index, all) => (
              <span key={stage} className="command-pipeline-stage">
                {stage}
                {index < all.length - 1 && <span className="command-pipeline-arrow"> &rarr; </span>}
              </span>
            ),
          )}
        </div>
      </section>

      <section className="command-links">
        <Link to="/weather" className="panel command-link-tile">
          <div className="type-label">{t("command.viewWeather")}</div>
          <div className="type-mono">
            {weatherQuery.data ? `${Object.keys(weatherQuery.data.canon).length} axes` : "--"}
          </div>
        </Link>
        <Link to="/schools" className="panel command-link-tile">
          <div className="type-label">{t("command.activeSchools")}</div>
          <div className="type-mono">{schoolsQuery.data?.schools.length ?? "--"}</div>
        </Link>
      </section>

      <div className="command-columns">
        <section className="panel command-column">
          <div className="type-label">{t("command.recentCycles")}</div>
          {recentCyclesQuery.data && recentCyclesQuery.data.entries.length === 0 && <p>{t("command.noneYet")}</p>}
          <ul className="type-mono command-list">
            {recentCyclesQuery.data?.entries.map((entry) => (
              <li key={entry.cycle_id}>
                <Link to={`/provenance?id=${entry.cycle_id}`}>#{entry.cycle_id}</Link> {entry.verdict} -- {entry.query.slice(0, 40)}
              </li>
            ))}
          </ul>
        </section>

        <section className="panel command-column">
          <div className="type-label">{t("command.recentEvents")}</div>
          {recentEventsQuery.data && recentEventsQuery.data.events.length === 0 && <p>{t("command.noneYet")}</p>}
          <ul className="type-mono command-list">
            {recentEventsQuery.data?.events.map((event) => (
              <li key={event.event_id}>
                {event.occurred_at} {event.event_type}
              </li>
            ))}
          </ul>
        </section>

        <section className="panel command-column">
          <div className="type-label">{t("command.recentContradictions")}</div>
          {contradictionsQuery.data && contradictionsQuery.data.contradictions.length === 0 && <p>{t("command.noneYet")}</p>}
          <ul className="type-mono command-list">
            {/* ContradictionModel has no stable id field to key on -- index is the honest option here. */}
            {contradictionsQuery.data?.contradictions.map((contradiction, index) => (
              <li key={index}>{contradiction.reason}</li>
            ))}
          </ul>
        </section>

        <section className="panel command-column">
          <div className="type-label">{t("command.recentErrors")}</div>
          {recentErrors.length === 0 && <p>{t("command.noneYet")}</p>}
          <ul className="type-mono command-list">
            {recentErrors.map((event) => (
              <li key={event.event_id}>
                {event.occurred_at} {event.event_type}
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}
