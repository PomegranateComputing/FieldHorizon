import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { ApiError, getCycleDetail, getLineage, getProvenance } from "../api/client";
import { downloadJson, downloadMarkdown } from "../shell/exportFile";
import { fetchCycleBundle, renderCycleBundleMarkdown } from "./cycleBundle";
import "./ProvenancePage.css";

/**
 * A 404 here means the id genuinely doesn't exist -- retrying it
 * (TanStack Query's default: 3 attempts with backoff) only delays
 * "Introuvable." by several seconds for no benefit. Transient errors
 * still get the default retry behavior.
 */
function retryUnlessNotFound(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status === 404) return false;
  return failureCount < 3;
}

export function ProvenancePage() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const [targetId, setTargetId] = useState(searchParams.get("id") ?? "");
  const [activeId, setActiveId] = useState(searchParams.get("id") ?? "");
  const [bundleState, setBundleState] = useState<"idle" | "building" | "error">("idle");

  const lineageQuery = useQuery({
    queryKey: ["lineage", activeId],
    queryFn: () => getLineage(activeId),
    enabled: activeId.length > 0,
    retry: retryUnlessNotFound,
  });
  const provenanceQuery = useQuery({
    queryKey: ["provenance", activeId],
    queryFn: () => getProvenance(activeId),
    enabled: activeId.length > 0,
    retry: retryUnlessNotFound,
  });

  // The reproducible cycle bundle (FABLE Sec.18) only makes sense for a
  // numeric cycle id -- an axiom-id lineage lookup has no single cycle row
  // to assemble config/evidence/scores/events around. Checked by actually
  // resolving GET /cycles/{id}, not just the numeric shape of the string,
  // so a numeric-looking id that isn't a real cycle doesn't offer a button
  // that would just fail.
  const isNumericId = /^\d+$/.test(activeId);
  const cycleDetailQuery = useQuery({
    queryKey: ["cycle-detail-for-bundle", activeId],
    queryFn: () => getCycleDetail(Number(activeId)),
    enabled: isNumericId,
    retry: retryUnlessNotFound,
  });

  async function handleExportBundle(format: "json" | "markdown") {
    setBundleState("building");
    try {
      const bundle = await fetchCycleBundle(Number(activeId));
      if (format === "json") {
        downloadJson(bundle, `field-horizon-cycle-${activeId}-bundle.json`);
      } else {
        downloadMarkdown(renderCycleBundleMarkdown(bundle), `field-horizon-cycle-${activeId}-bundle.md`);
      }
      setBundleState("idle");
    } catch {
      setBundleState("error");
    }
  }

  return (
    <div className="section-page provenance-page">
      <h1>PROVENANCE</h1>

      <form
        className="provenance-form"
        onSubmit={(event) => {
          event.preventDefault();
          setActiveId(targetId.trim());
        }}
      >
        <input
          type="text"
          placeholder={t("provenance.targetIdPlaceholder")}
          value={targetId}
          onChange={(event) => setTargetId(event.currentTarget.value)}
        />
        <button type="submit" disabled={!targetId.trim()}>
          {t("provenance.lookup")}
        </button>
        {lineageQuery.data && provenanceQuery.data && (
          <button
            type="button"
            onClick={() =>
              downloadJson(
                { lineage: lineageQuery.data.text, provenance: provenanceQuery.data },
                `field-horizon-provenance-${activeId}.json`,
              )
            }
          >
            {t("provenance.export")}
          </button>
        )}
        {cycleDetailQuery.data && (
          <>
            <button type="button" onClick={() => void handleExportBundle("json")} disabled={bundleState === "building"}>
              {bundleState === "building" ? t("provenance.buildingBundle") : t("provenance.exportBundleJson")}
            </button>
            <button type="button" onClick={() => void handleExportBundle("markdown")} disabled={bundleState === "building"}>
              {t("provenance.exportBundleMarkdown")}
            </button>
          </>
        )}
      </form>
      {bundleState === "error" && <p className="provenance-error">{t("provenance.bundleFailed")}</p>}

      {!activeId && <p>{t("provenance.none")}</p>}

      {lineageQuery.isError && <p className="provenance-error">{t("provenance.notFound")}</p>}

      {lineageQuery.data && (
        <section className="provenance-panel">
          <div className="type-label">{t("provenance.lineage")}</div>
          <pre className="provenance-lineage-tree type-mono">{lineageQuery.data.text}</pre>
        </section>
      )}

      {provenanceQuery.data && (
        <section className="provenance-panel">
          <div className="type-label">{t("provenance.supplyChainReport")}</div>
          <dl className="provenance-fields type-mono">
            <dt>{t("provenance.objectType")}</dt>
            <dd>{provenanceQuery.data.object_type}</dd>
            <dt>{t("provenance.objectId")}</dt>
            <dd>{provenanceQuery.data.object_id}</dd>
            <dt>{t("provenance.supportingEvidence")}</dt>
            <dd>{provenanceQuery.data.supporting_evidence_count}</dd>
            <dt>{t("provenance.opposingEvidence")}</dt>
            <dd>{provenanceQuery.data.opposing_evidence_count}</dd>
            <dt>{t("provenance.circularAncestry")}</dt>
            <dd className={provenanceQuery.data.circular_ancestry ? "provenance-warning" : ""}>
              {provenanceQuery.data.circular_ancestry ? t("cycles.yes") : t("cycles.no")}
            </dd>
            <dt>{t("provenance.syntheticDependencyRatio")}</dt>
            <dd>{provenanceQuery.data.synthetic_dependency_ratio.toFixed(3)}</dd>
            <dt>{t("provenance.completenessScore")}</dt>
            <dd>{provenanceQuery.data.completeness_score.toFixed(3)}</dd>
          </dl>

          {provenanceQuery.data.missing_links.length > 0 && (
            <>
              <div className="type-label provenance-section-label">{t("provenance.missingLinks")}</div>
              <ul className="type-mono">
                {provenanceQuery.data.missing_links.map((link) => (
                  <li key={link}>{link}</li>
                ))}
              </ul>
            </>
          )}

          {provenanceQuery.data.weakest_link && (
            <>
              <div className="type-label provenance-section-label">{t("provenance.weakestLink")}</div>
              <p className="type-mono">
                {provenanceQuery.data.weakest_link.source_type}:{provenanceQuery.data.weakest_link.source_id} --[
                {provenanceQuery.data.weakest_link.relation_type}]--&gt; {provenanceQuery.data.weakest_link.target_type}:
                {provenanceQuery.data.weakest_link.target_id} ({t("provenance.confidence")}{" "}
                {provenanceQuery.data.weakest_link.confidence.toFixed(2)})
              </p>
            </>
          )}

          {provenanceQuery.data.model_registry_ids.length > 0 && (
            <>
              <div className="type-label provenance-section-label">{t("provenance.modelRegistryIds")}</div>
              <p className="type-mono">{provenanceQuery.data.model_registry_ids.join(", ")}</p>
            </>
          )}
        </section>
      )}
    </div>
  );
}
