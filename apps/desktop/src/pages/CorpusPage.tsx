import { useVirtualizer } from "@tanstack/react-virtual";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { getManifest, getSources, postIngestManifest } from "../api/client";
import type { SourceModel } from "../api/types";
import { useInspectorSelection } from "../shell/InspectorContext";
import { useEventStreamContext } from "../shell/EventStreamContext";
import { downloadCsv } from "../shell/exportFile";
import "./CorpusPage.css";

type SortKey = "title" | "source_type" | "weight";
type Tab = "sources" | "manifest" | "ingest";

const SOURCES_PAGE_SIZE = 200;
const SEARCH_DEBOUNCE_MS = 300;

/** Delays the effective query key until typing pauses (FABLE Sec.15's debounce requirement) -- a live per-keystroke search against the server would otherwise fire one request per character. */
function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(id);
  }, [value, delayMs]);
  return debounced;
}

function SourcesTab() {
  const { t } = useTranslation();
  const { select, selection } = useInspectorSelection();
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebouncedValue(search.trim(), SEARCH_DEBOUNCE_MS);
  const [sortKey, setSortKey] = useState<SortKey>("title");
  const parentRef = useRef<HTMLDivElement>(null);

  // Server-paginated and server-searched (FABLE Sec.15: "ne pas transférer
  // l'intégralité de la base au frontend pour afficher un tableau") -- a
  // corpus of thousands of sources loads page by page, not in one response.
  // signal comes from TanStack Query's own AbortController, so a debounced
  // search change or unmount cancels whatever page fetch is still in flight.
  const sourcesQuery = useInfiniteQuery({
    queryKey: ["sources", debouncedSearch],
    queryFn: ({ pageParam, signal }) =>
      getSources({ limit: SOURCES_PAGE_SIZE, offset: pageParam, q: debouncedSearch }, signal),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((n, page) => n + page.sources.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
  });

  const loadedSources = useMemo(() => sourcesQuery.data?.pages.flatMap((page) => page.sources) ?? [], [sourcesQuery.data]);
  const total = sourcesQuery.data?.pages[0]?.total ?? 0;

  // Sorting only reorders what's been loaded so far -- honest for a
  // progressively-loading list rather than silently wrong across pages not
  // yet fetched.
  const sorted = useMemo(() => {
    return [...loadedSources].sort((a, b) => {
      if (sortKey === "weight") return b.weight - a.weight;
      return String(a[sortKey]).localeCompare(String(b[sortKey]));
    });
  }, [loadedSources, sortKey]);

  // Virtualized (Phase UI-4's own rule for every screen): this corpus has a
  // handful of sources today, but the list doesn't re-render the whole DOM
  // as it grows -- real for a corpus of 6 sources and for one of 6,000.
  const virtualizer = useVirtualizer({
    count: sorted.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 36,
    overscan: 10,
  });

  if (sourcesQuery.status === "pending") return <p>{t("command.loading")}</p>;
  if (sourcesQuery.status === "error") return <p>{t("command.unreachable")}</p>;

  return (
    <div className="corpus-sources">
      <div className="corpus-toolbar">
        <input
          type="text"
          placeholder={t("corpus.searchPlaceholder")}
          value={search}
          onChange={(event) => setSearch(event.currentTarget.value)}
          aria-label={t("corpus.searchPlaceholder")}
        />
        <select value={sortKey} onChange={(event) => setSortKey(event.currentTarget.value as SortKey)} aria-label={t("corpus.sortBy")}>
          <option value="title">{t("corpus.colTitle")}</option>
          <option value="source_type">{t("corpus.colType")}</option>
          <option value="weight">{t("corpus.colWeight")}</option>
        </select>
        <span className="corpus-source-count type-label">{t("corpus.loadedOfTotal", { loaded: loadedSources.length, total })}</span>
        <button
          type="button"
          onClick={() =>
            downloadCsv(
              [
                ["id", "title", "source_type", "weight"],
                ...sorted.map((s) => [String(s.id), s.title, s.source_type, s.weight.toFixed(3)]),
              ],
              "field-horizon-sources.csv",
            )
          }
          disabled={sorted.length === 0}
        >
          {t("corpus.exportCsv")}
        </button>
      </div>

      {sorted.length === 0 ? (
        <p>{t("corpus.noSources")}</p>
      ) : (
        <div ref={parentRef} className="corpus-source-list" role="table" aria-label={t("corpus.sourcesTab")}>
          <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
            {virtualizer.getVirtualItems().map((virtualRow) => {
              const source: SourceModel = sorted[virtualRow.index];
              const isSelected = selection?.kind === "source" && selection.id === source.id;
              return (
                <button
                  key={source.id}
                  type="button"
                  role="row"
                  className={`corpus-source-row type-mono${isSelected ? " corpus-source-row--selected" : ""}`}
                  style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${virtualRow.start}px)` }}
                  onClick={() => select({ kind: "source", id: source.id })}
                >
                  <span className="corpus-source-title">{source.title}</span>
                  <span className="corpus-source-type">{source.source_type}</span>
                  <span className="corpus-source-weight">{source.weight.toFixed(2)}</span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {sourcesQuery.hasNextPage && (
        <button type="button" onClick={() => sourcesQuery.fetchNextPage()} disabled={sourcesQuery.isFetchingNextPage}>
          {sourcesQuery.isFetchingNextPage ? t("command.loading") : t("corpus.loadMore")}
        </button>
      )}
    </div>
  );
}

function ManifestTab() {
  const { t } = useTranslation();
  const manifestQuery = useQuery({ queryKey: ["manifest"], queryFn: getManifest });

  if (manifestQuery.status === "pending") return <p>{t("command.loading")}</p>;
  if (manifestQuery.status === "error") return <p>{t("corpus.noManifest")}</p>;

  return (
    <div className="corpus-manifest">
      <p className="type-label">{t("corpus.manifestReadOnly")}</p>
      <ul className="corpus-manifest-list type-mono">
        {manifestQuery.data.entries.map((entry) => (
          <li key={entry.id}>
            <div>
              {entry.id} -- {entry.title}
            </div>
            <div className="corpus-manifest-path">{entry.path}</div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function IngestTab() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { events } = useEventStreamContext();
  const [runId, setRunId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<{ status: "ok" | "error"; message: string } | null>(null);

  const runEvents = useMemo(() => (runId ? events.filter((e) => e.run_id === runId) : []), [events, runId]);

  async function handleIngest() {
    const correlationId = crypto.randomUUID();
    setRunId(correlationId);
    setRunning(true);
    setResult(null);
    try {
      const response = await postIngestManifest(correlationId);
      const ingestedCount = response.entries.filter((e) => e.status === "ingested").length;
      setResult({ status: "ok", message: `${ingestedCount}/${response.entries.length}` });
      await queryClient.invalidateQueries({ queryKey: ["sources"] });
      await queryClient.invalidateQueries({ queryKey: ["status"] });
    } catch (error) {
      setResult({ status: "error", message: String(error) });
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="corpus-ingest">
      <button type="button" onClick={handleIngest} disabled={running}>
        {running ? t("corpus.ingesting") : t("corpus.triggerIngest")}
      </button>

      {result && (
        <p className={result.status === "error" ? "corpus-ingest-error" : "corpus-ingest-ok"}>
          {result.status === "ok" ? t("corpus.ingestComplete", { count: result.message }) : result.message}
        </p>
      )}

      {runId && (
        <div className="corpus-ingest-log type-mono">
          {runEvents.map((event) => (
            <div key={event.event_id}>
              {event.event_type}
              {typeof event.payload.title === "string" ? ` -- ${event.payload.title}` : ""}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function CorpusPage() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>("sources");

  return (
    <div className="section-page corpus-page">
      <h1>CORPUS</h1>
      <div className="corpus-tabs type-label">
        <button type="button" className={tab === "sources" ? "corpus-tab--active" : ""} onClick={() => setTab("sources")}>
          {t("corpus.sourcesTab")}
        </button>
        <button type="button" className={tab === "manifest" ? "corpus-tab--active" : ""} onClick={() => setTab("manifest")}>
          {t("corpus.manifestTab")}
        </button>
        <button type="button" className={tab === "ingest" ? "corpus-tab--active" : ""} onClick={() => setTab("ingest")}>
          {t("corpus.ingestTab")}
        </button>
      </div>

      {tab === "sources" && <SourcesTab />}
      {tab === "manifest" && <ManifestTab />}
      {tab === "ingest" && <IngestTab />}
    </div>
  );
}
