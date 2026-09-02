import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { useConsoleVisibility } from "./ConsoleVisibilityContext";
import { useEventStreamContext } from "./EventStreamContext";
import { downloadJson } from "./exportFile";

function exportEventsAsJson(events: unknown[]): void {
  downloadJson(events, `field-horizon-console-${new Date().toISOString().replace(/[:.]/g, "-")}.json`);
}

/**
 * FABLE Sec.9's bottom console: resizable (native CSS `resize: vertical` --
 * no custom drag handle needed for that), filterable, exportable, fed by the
 * SSE stream. "Cleared visually without deleting the logs" means exactly
 * this: Clear only resets locally-displayed state, never calls any delete
 * endpoint -- the real record stays in domain_events regardless.
 *
 * Not yet built: detaching into its own window (FABLE Sec.9 mentions it;
 * deferred rather than faked with a non-functional button).
 */
export function Console() {
  const { t } = useTranslation();
  const { events, connected } = useEventStreamContext();
  const { visible, toggle } = useConsoleVisibility();
  const [filter, setFilter] = useState("");
  const [clearedBefore, setClearedBefore] = useState<string | null>(null);

  const visibleEvents = useMemo(() => {
    let list = events;
    if (clearedBefore) {
      const index = list.findIndex((event) => event.event_id === clearedBefore);
      list = index >= 0 ? list.slice(index + 1) : list;
    }
    if (!filter.trim()) {
      return list;
    }
    const needle = filter.toLowerCase();
    return list.filter(
      (event) => event.event_type.toLowerCase().includes(needle) || JSON.stringify(event.payload).toLowerCase().includes(needle),
    );
  }, [events, filter, clearedBefore]);

  if (!visible) {
    return (
      <section className="panel console console--collapsed" aria-label="Console">
        <div className="console-toolbar">
          <span className="type-label">
            {t("console.title")} {connected ? <span className="status-dot status-dot--active" /> : <span className="status-dot" />}
          </span>
          <button type="button" onClick={toggle}>
            {t("console.show")}
          </button>
        </div>
      </section>
    );
  }

  return (
    <section className="panel console" aria-label="Console">
      <div className="console-toolbar">
        <span className="type-label">
          {t("console.title")} {connected ? <span className="status-dot status-dot--active" /> : <span className="status-dot" />}
        </span>
        <input
          className="console-filter"
          type="text"
          placeholder={t("console.filterPlaceholder")}
          value={filter}
          onChange={(event) => setFilter(event.currentTarget.value)}
          aria-label={t("console.filterPlaceholder")}
        />
        <button type="button" onClick={() => exportEventsAsJson(visibleEvents)} disabled={visibleEvents.length === 0}>
          {t("console.export")}
        </button>
        <button
          type="button"
          onClick={() => setClearedBefore(events.length > 0 ? events[events.length - 1].event_id : null)}
          disabled={events.length === 0}
        >
          {t("console.clear")}
        </button>
        <button type="button" onClick={toggle}>
          {t("console.hide")}
        </button>
      </div>
      <div className="console-log type-mono">
        {visibleEvents.length === 0 ? (
          <p className="console-empty">{t("console.noEvents")}</p>
        ) : (
          visibleEvents.map((event) => (
            <div key={event.event_id} className="console-line">
              <span className="console-timestamp">{event.timestamp}</span> <span>{event.event_type}</span>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
