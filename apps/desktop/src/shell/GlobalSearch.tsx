import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { getActiveCanon, getContradictions, getRegistryList, getSources, postRetrieve } from "../api/client";
import type { CanonEntryModel, ContradictionModel, RetrieveCandidateModel, SourceModel } from "../api/types";
import { useEventStreamContext } from "./EventStreamContext";
import { trapTabKey } from "./focusTrap";
import { useInspectorSelection } from "./InspectorContext";
import "./GlobalSearch.css";

const SEARCH_DEBOUNCE_MS = 300;

interface RegistryHit {
  id: string;
  name: string;
}

/**
 * FABLE Sec.12: results grouped by type, real backend data only -- never a
 * second retrieval engine. Each group below maps to something that
 * genuinely exists server-side:
 *   sources      -> GET /sources?q=      (server-side substring match)
 *   chunks       -> POST /retrieve        (the real hybrid retrieval engine)
 *   models       -> GET /registry/model   (client-filtered; small, real list)
 *   events       -> the live SSE buffer already held by EventStreamContext
 *   contradictions -> GET /export/contradictions (client-filtered)
 *   canon/cycles -> GET /canon            (client-filtered)
 *   link         -> a quick "open lineage/provenance for this id" jump --
 *                    there is no free-text index over the provenance graph,
 *                    so this is a direct id lookup, not a fabricated search.
 * Deliberately NOT covered: semantic profiles (would need querying every
 * kind speculatively for uncertain value) and settings/params (nothing
 * searchable exists there -- it's a fixed navigation destination, already
 * reachable from the command palette).
 */
export function GlobalSearch({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { select } = useInspectorSelection();
  const { events } = useEventStreamContext();

  const [query, setQuery] = useState("");
  const [sources, setSources] = useState<SourceModel[]>([]);
  const [chunks, setChunks] = useState<RetrieveCandidateModel[]>([]);
  const [models, setModels] = useState<RegistryHit[]>([]);
  const [contradictions, setContradictions] = useState<ContradictionModel[]>([]);
  const [canonEntries, setCanonEntries] = useState<CanonEntryModel[]>([]);
  const [loading, setLoading] = useState(false);

  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Cheap, small, real lists fetched once per open -- filtered client-side
  // as the user types rather than re-fetched per keystroke.
  useEffect(() => {
    if (!open) return;
    void getRegistryList("model").then((r) =>
      setModels(r.entries.map((entry) => ({ id: String(entry.id), name: String(entry.name ?? "") }))),
    );
    void getContradictions().then((r) => setContradictions(r.contradictions));
    void getActiveCanon(200, true).then((r) => setCanonEntries(r.entries));
  }, [open]);

  useEffect(() => {
    if (open) {
      setQuery("");
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  useEffect(() => {
    const needle = query.trim();
    if (!needle) {
      setSources([]);
      setChunks([]);
      return;
    }
    const controller = new AbortController();
    const id = setTimeout(() => {
      setLoading(true);
      Promise.all([
        getSources({ q: needle, limit: 5 }, controller.signal).then((r) => setSources(r.sources)),
        postRetrieve({ query: needle, limit: 5, explain: false, plan: false }).then((r) => setChunks(r.candidates)),
      ])
        .catch(() => {
          /* a stale/aborted request losing the race is not an error worth surfacing here */
        })
        .finally(() => setLoading(false));
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      clearTimeout(id);
      controller.abort();
    };
  }, [query]);

  const needle = query.trim().toLowerCase();

  const filteredModels = useMemo(
    () => (needle ? models.filter((m) => m.name?.toLowerCase().includes(needle)) : []).slice(0, 5),
    [models, needle],
  );
  const filteredEvents = useMemo(
    () =>
      (needle
        ? events.filter((e) => e.event_type.toLowerCase().includes(needle) || JSON.stringify(e.payload).toLowerCase().includes(needle))
        : []
      )
        .slice(-5)
        .reverse(),
    [events, needle],
  );
  const filteredContradictions = useMemo(
    () =>
      (needle
        ? contradictions.filter(
            (c) =>
              c.source_id.toLowerCase().includes(needle) ||
              c.target_id.toLowerCase().includes(needle) ||
              c.reason.toLowerCase().includes(needle),
          )
        : []
      ).slice(0, 5),
    [contradictions, needle],
  );
  const filteredCanon = useMemo(
    () =>
      (needle
        ? canonEntries.filter((c) => c.query.toLowerCase().includes(needle) || c.fragment.toLowerCase().includes(needle))
        : []
      ).slice(0, 5),
    [canonEntries, needle],
  );

  const hasAnyResults =
    sources.length > 0 ||
    chunks.length > 0 ||
    filteredModels.length > 0 ||
    filteredEvents.length > 0 ||
    filteredContradictions.length > 0 ||
    filteredCanon.length > 0;

  function close() {
    onClose();
  }

  if (!open) return null;

  return (
    <div className="palette-backdrop" onClick={close}>
      <div
        ref={containerRef}
        className="panel global-search"
        role="dialog"
        aria-modal="true"
        aria-label={t("globalSearch.title")}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === "Escape") close();
          else if (containerRef.current) trapTabKey(event, containerRef.current);
        }}
      >
        <input
          ref={inputRef}
          type="text"
          className="command-palette-input"
          placeholder={t("globalSearch.placeholder")}
          value={query}
          onChange={(event) => setQuery(event.currentTarget.value)}
          aria-label={t("globalSearch.title")}
        />
        <div className="global-search-results">
          {!needle && <p className="command-palette-empty">{t("globalSearch.prompt")}</p>}
          {needle && !hasAnyResults && !loading && <p className="command-palette-empty">{t("globalSearch.noResults")}</p>}

          {needle && (
            <ul className="global-search-list">
              <li>
                <button
                  type="button"
                  className="command-palette-item"
                  onClick={() => {
                    close();
                    navigate(`/provenance?id=${encodeURIComponent(query.trim())}`);
                  }}
                >
                  {t("globalSearch.openLineageFor", { query: query.trim() })}
                </button>
              </li>
            </ul>
          )}

          {sources.length > 0 && (
            <section>
              <div className="type-label global-search-group-label">{t("globalSearch.groupSources")}</div>
              <ul className="global-search-list">
                {sources.map((source) => (
                  <li key={source.id}>
                    <button
                      type="button"
                      className="command-palette-item"
                      onClick={() => {
                        close();
                        navigate("/corpus");
                        select({ kind: "source", id: source.id });
                      }}
                    >
                      {source.title} <span className="global-search-meta">{source.source_type}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {chunks.length > 0 && (
            <section>
              <div className="type-label global-search-group-label">{t("globalSearch.groupChunks")}</div>
              <ul className="global-search-list">
                {chunks.map((chunk) => (
                  <li key={chunk.chunk_id}>
                    <button
                      type="button"
                      className="command-palette-item"
                      onClick={() => {
                        close();
                        navigate("/retrieval");
                      }}
                    >
                      {chunk.source_title} <span className="global-search-meta">{chunk.content.slice(0, 80)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {filteredCanon.length > 0 && (
            <section>
              <div className="type-label global-search-group-label">{t("globalSearch.groupCanon")}</div>
              <ul className="global-search-list">
                {filteredCanon.map((entry) => (
                  <li key={entry.cycle_id}>
                    <button
                      type="button"
                      className="command-palette-item"
                      onClick={() => {
                        close();
                        navigate("/canon");
                      }}
                    >
                      #{entry.cycle_id} -- {entry.verdict} <span className="global-search-meta">{entry.query}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {filteredContradictions.length > 0 && (
            <section>
              <div className="type-label global-search-group-label">{t("globalSearch.groupContradictions")}</div>
              <ul className="global-search-list">
                {filteredContradictions.map((c, i) => (
                  <li key={`${c.source_id}-${c.target_id}-${i}`}>
                    <button
                      type="button"
                      className="command-palette-item"
                      onClick={() => {
                        close();
                        navigate("/contradictions");
                      }}
                    >
                      {c.source_id} vs {c.target_id} <span className="global-search-meta">{c.reason.slice(0, 60)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {filteredModels.length > 0 && (
            <section>
              <div className="type-label global-search-group-label">{t("globalSearch.groupModels")}</div>
              <ul className="global-search-list">
                {filteredModels.map((model) => (
                  <li key={model.id}>
                    <button
                      type="button"
                      className="command-palette-item"
                      onClick={() => {
                        close();
                        navigate("/models");
                      }}
                    >
                      {model.name}
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {filteredEvents.length > 0 && (
            <section>
              <div className="type-label global-search-group-label">{t("globalSearch.groupEvents")}</div>
              <ul className="global-search-list">
                {filteredEvents.map((event) => (
                  <li key={event.event_id}>
                    <span className="command-palette-item global-search-event">
                      {event.event_type} <span className="global-search-meta">{event.timestamp}</span>
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
