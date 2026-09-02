import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { getSchoolMembers, getSourceChunks, getSourceDetail } from "../api/client";
import { useInspectorSelection } from "./InspectorContext";

/**
 * FABLE Sec.9's right inspector: metadata, properties, node detail, scores,
 * source, tags, relations, secondary actions -- contextual to whatever's
 * selected in the center workspace (Phase UI-4 item 2: CORPUS is the first
 * screen to select something). "Provenance" is deliberately not shown here:
 * GET /provenance/{id} dispatches numeric ids as *cycle* ids
 * (provenance.py:build_provenance_report_for_target), so a source's own
 * provenance has no real route to call yet -- omitted rather than wired to
 * the wrong report.
 */
function SourceInspectorContent({ id }: { id: number }) {
  const { t } = useTranslation();
  const detailQuery = useQuery({ queryKey: ["source-detail", id], queryFn: () => getSourceDetail(id) });
  const chunksQuery = useQuery({ queryKey: ["source-chunks", id], queryFn: () => getSourceChunks(id, 20) });

  if (detailQuery.status === "pending") {
    return <p>{t("command.loading")}</p>;
  }
  if (detailQuery.status === "error") {
    return <p>{t("command.unreachable")}</p>;
  }

  const source = detailQuery.data.source;
  return (
    <div className="inspector-content type-mono">
      <h3 className="type-literary inspector-content-title">{source.title}</h3>
      <dl className="inspector-fields">
        <dt>id</dt>
        <dd>{source.id}</dd>
        <dt>{t("inspector.sourceType")}</dt>
        <dd>{source.source_type}</dd>
        <dt>{t("inspector.path")}</dt>
        <dd className="inspector-field-wrap">{source.path}</dd>
        <dt>{t("inspector.manifestId")}</dt>
        <dd>{source.manifest_id ?? "--"}</dd>
        <dt>{t("inspector.language")}</dt>
        <dd>{source.language}</dd>
        <dt>{t("inspector.weight")}</dt>
        <dd>{source.weight}</dd>
        <dt>{t("inspector.createdAt")}</dt>
        <dd>{source.created_at}</dd>
        <dt>{t("inspector.chunkCount")}</dt>
        <dd>{source.chunk_count}</dd>
      </dl>

      <div className="type-label inspector-section-label">{t("inspector.chunks")}</div>
      {chunksQuery.status === "success" && (
        <ul className="inspector-chunk-list">
          {chunksQuery.data.chunks.map((chunk) => (
            <li key={chunk.id}>
              <div className="type-label">
                #{chunk.chunk_index} ({chunk.char_start}-{chunk.char_end})
              </div>
              <div className="inspector-chunk-content">{chunk.content.slice(0, 160)}</div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * A school's own detail: representative members (school_members_detail
 * already sorts closest-to-centroid first, so the top few genuinely are
 * the most representative, not an arbitrary sample) plus the full
 * membership list with real distance-to-centroid values.
 */
function SchoolInspectorContent({ id }: { id: number }) {
  const { t } = useTranslation();
  const membersQuery = useQuery({ queryKey: ["school-members", id], queryFn: () => getSchoolMembers(id) });

  if (membersQuery.status === "pending") {
    return <p>{t("command.loading")}</p>;
  }
  if (membersQuery.status === "error") {
    return <p>{t("command.unreachable")}</p>;
  }

  const members = membersQuery.data.members;
  return (
    <div className="inspector-content type-mono">
      <h3 className="type-literary inspector-content-title">{t("schools.membership")}</h3>
      {members.length === 0 && <p className="inspector-empty">{t("schools.noMembers")}</p>}
      <ul className="inspector-chunk-list">
        {members.map((member) => (
          <li key={member.cycle_id}>
            <div className="type-label">
              #{member.cycle_id} -- {member.verdict} -- {t("schools.distanceToCenter")} {member.distance.toFixed(3)}
            </div>
            <div className="inspector-chunk-content">{member.fragment.slice(0, 160) || member.query}</div>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function Inspector() {
  const { t } = useTranslation();
  const { selection } = useInspectorSelection();

  return (
    <aside className="panel inspector" aria-label="Inspector">
      <div className="type-label">{t("inspector.title")}</div>
      {selection?.kind === "source" ? (
        <SourceInspectorContent id={selection.id} />
      ) : selection?.kind === "school" ? (
        <SchoolInspectorContent id={selection.id} />
      ) : (
        <p className="inspector-empty">{t("inspector.empty")}</p>
      )}
    </aside>
  );
}
