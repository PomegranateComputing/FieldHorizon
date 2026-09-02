import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { getSystemInfo, postRetrieve } from "../api/client";
import type { CanonCandidateModel, RetrieveCandidateModel, RetrieveResponse, RetrievalWeightsModel } from "../api/types";
import "./RetrievalPage.css";

const WEIGHT_KEYS: (keyof RetrievalWeightsModel)[] = [
  "vector_similarity",
  "bm25",
  "domain_prior",
  "source_weight",
  "severity",
];

const PRESETS_STORAGE_KEY = "field-horizon-retrieval-presets";

interface Preset {
  name: string;
  weights: RetrievalWeightsModel;
}

function loadPresets(): Preset[] {
  try {
    const raw = window.localStorage.getItem(PRESETS_STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Preset[]) : [];
  } catch {
    return [];
  }
}

function savePresets(presets: Preset[]): void {
  window.localStorage.setItem(PRESETS_STORAGE_KEY, JSON.stringify(presets));
}

function WeightSliders({
  weights,
  onChange,
}: {
  weights: RetrievalWeightsModel;
  onChange: (weights: RetrievalWeightsModel) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="retrieval-weights">
      {WEIGHT_KEYS.map((key) => (
        <label key={key} className="retrieval-weight-row">
          <span className="type-label">{t(`retrieval.weight.${key}`)}</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.01}
            value={weights[key]}
            onChange={(event) => onChange({ ...weights, [key]: Number(event.currentTarget.value) })}
          />
          <span className="type-mono retrieval-weight-value">{weights[key].toFixed(2)}</span>
        </label>
      ))}
    </div>
  );
}

function ComponentBreakdown({ components }: { components: RetrieveCandidateModel["components"] }) {
  const entries = Object.entries(components ?? {});
  if (entries.length === 0) return null;
  return (
    <table className="retrieval-components type-mono">
      <tbody>
        {entries.map(([name, component]) => (
          <tr key={name}>
            <td>{name}</td>
            <td>raw {component.raw.toFixed(3)}</td>
            <td>w {component.weight.toFixed(2)}</td>
            <td>= {component.contribution.toFixed(3)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CandidateRow({ candidate, rank }: { candidate: RetrieveCandidateModel; rank: number }) {
  return (
    <li className={`retrieval-candidate${candidate.exclusion_reason ? " retrieval-candidate--excluded" : ""}`}>
      <div className="retrieval-candidate-header type-mono">
        <span className="type-label">#{rank}</span>
        <span>{candidate.source_title}</span>
        <span>{candidate.canonical_ref}</span>
        <span>score {candidate.score.toFixed(3)}</span>
      </div>
      {candidate.exclusion_reason && <div className="retrieval-exclusion-reason">{candidate.exclusion_reason}</div>}
      <p className="retrieval-candidate-content">{candidate.content.slice(0, 240)}</p>
      <ComponentBreakdown components={candidate.components} />
    </li>
  );
}

function CanonCandidateRow({ candidate }: { candidate: CanonCandidateModel }) {
  return (
    <li className={`retrieval-candidate${candidate.exclusion_reason ? " retrieval-candidate--excluded" : ""}`}>
      <div className="retrieval-candidate-header type-mono">
        <span className="type-label">cycle #{candidate.cycle_id}</span>
        <span>score {candidate.score.toFixed(3)}</span>
        <span>evidence {candidate.supporting_evidence_count}</span>
      </div>
      {candidate.exclusion_reason && <div className="retrieval-exclusion-reason">{candidate.exclusion_reason}</div>}
      <p className="retrieval-candidate-content">{candidate.fragment.slice(0, 240)}</p>
    </li>
  );
}

function ResultsColumn({ result }: { result: RetrieveResponse | undefined }) {
  const { t } = useTranslation();
  if (!result) return null;

  // The generated types mark these optional (they're pydantic default_factory
  // fields, not strictly required in the JSON schema) even though the server
  // always sends them -- default to empty rather than assume the field exists.
  const strategiesSelected = result.strategies_selected ?? [];
  const constraintsApplied = result.constraints_applied ?? [];
  const canonCandidates = result.canon_candidates ?? [];
  const excludedCandidates = result.excluded_candidates ?? [];
  const excludedCanonCandidates = result.excluded_canon_candidates ?? [];

  return (
    <div className="retrieval-results">
      {result.used_planner && (
        <div className="retrieval-plan-meta type-mono">
          <div>
            {t("retrieval.strategies")}: {strategiesSelected.join(", ") || "--"}
          </div>
          <div>
            {t("retrieval.constraints")}: {constraintsApplied.join(", ") || "--"}
          </div>
        </div>
      )}
      <ol className="retrieval-candidate-list">
        {result.candidates.map((candidate, index) => (
          <CandidateRow key={candidate.chunk_id} candidate={candidate} rank={index + 1} />
        ))}
      </ol>

      {result.used_planner && canonCandidates.length > 0 && (
        <>
          <div className="type-label retrieval-section-label">{t("retrieval.canonCandidates")}</div>
          <ol className="retrieval-candidate-list">
            {canonCandidates.map((candidate) => (
              <CanonCandidateRow key={candidate.cycle_id} candidate={candidate} />
            ))}
          </ol>
        </>
      )}

      {result.used_planner && (excludedCandidates.length > 0 || excludedCanonCandidates.length > 0) && (
        <>
          <div className="type-label retrieval-section-label">{t("retrieval.excluded")}</div>
          <ol className="retrieval-candidate-list">
            {excludedCandidates.map((candidate, index) => (
              <CandidateRow key={candidate.chunk_id} candidate={candidate} rank={index + 1} />
            ))}
            {excludedCanonCandidates.map((candidate) => (
              <CanonCandidateRow key={candidate.cycle_id} candidate={candidate} />
            ))}
          </ol>
        </>
      )}
    </div>
  );
}

export function RetrievalPage() {
  const { t } = useTranslation();
  const systemInfoQuery = useQuery({ queryKey: ["system-info"], queryFn: getSystemInfo });

  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(10);
  const [plan, setPlan] = useState(false);
  const [asOf, setAsOf] = useState("");
  const [weights, setWeights] = useState<RetrievalWeightsModel | null>(null);
  const [compare, setCompare] = useState(false);
  const [compareWeights, setCompareWeights] = useState<RetrievalWeightsModel | null>(null);
  const [presets, setPresets] = useState<Preset[]>(() => loadPresets());
  const [presetName, setPresetName] = useState("");

  useEffect(() => {
    if (systemInfoQuery.data && !weights) {
      setWeights(systemInfoQuery.data.default_weights);
      setCompareWeights(systemInfoQuery.data.default_weights);
    }
  }, [systemInfoQuery.data, weights]);

  const mutation = useMutation({
    mutationFn: (w: RetrievalWeightsModel) =>
      postRetrieve({ query, limit, explain: true, plan, as_of: plan && asOf ? asOf : null, weights: w }),
  });
  const compareMutation = useMutation({
    mutationFn: (w: RetrievalWeightsModel) =>
      postRetrieve({ query, limit, explain: true, plan, as_of: plan && asOf ? asOf : null, weights: w }),
  });

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!weights || !query.trim()) return;
    mutation.mutate(weights);
    if (compare && compareWeights) {
      compareMutation.mutate(compareWeights);
    }
  }

  function handleSavePreset() {
    if (!presetName.trim() || !weights) return;
    const next = [...presets.filter((p) => p.name !== presetName), { name: presetName, weights }];
    setPresets(next);
    savePresets(next);
    setPresetName("");
  }

  function handleLoadPreset(name: string) {
    const preset = presets.find((p) => p.name === name);
    if (preset) setWeights(preset.weights);
  }

  const defaultWeights = useMemo(() => systemInfoQuery.data?.default_weights, [systemInfoQuery.data]);

  if (!weights) {
    return (
      <div className="section-page">
        <h1>RETRIEVAL</h1>
        <p>{t("command.loading")}</p>
      </div>
    );
  }

  return (
    <div className="section-page retrieval-page">
      <h1>RETRIEVAL</h1>

      <form onSubmit={handleSubmit} className="retrieval-form">
        <input
          type="text"
          className="retrieval-query-input"
          placeholder={t("retrieval.queryPlaceholder")}
          value={query}
          onChange={(event) => setQuery(event.currentTarget.value)}
        />
        <label className="type-label">
          {t("retrieval.limit")}
          <input
            type="number"
            min={1}
            max={100}
            value={limit}
            onChange={(event) => setLimit(Number(event.currentTarget.value))}
          />
        </label>
        <label className="type-label retrieval-checkbox">
          <input type="checkbox" checked={plan} onChange={(event) => setPlan(event.currentTarget.checked)} />
          {t("retrieval.usePlanner")}
        </label>
        {plan && (
          <input
            type="text"
            placeholder={t("retrieval.asOfPlaceholder")}
            value={asOf}
            onChange={(event) => setAsOf(event.currentTarget.value)}
          />
        )}
        <label className="type-label retrieval-checkbox">
          <input type="checkbox" checked={compare} onChange={(event) => setCompare(event.currentTarget.checked)} />
          {t("retrieval.compareMode")}
        </label>
        <button type="submit" disabled={!query.trim() || mutation.isPending}>
          {mutation.isPending ? t("command.loading") : t("retrieval.run")}
        </button>
      </form>

      <div className="retrieval-config-row">
        <div className="retrieval-config-column">
          <div className="type-label">{t("retrieval.weightsA")}</div>
          <WeightSliders weights={weights} onChange={setWeights} />
          {defaultWeights && (
            <button type="button" onClick={() => setWeights(defaultWeights)}>
              {t("retrieval.resetDefaults")}
            </button>
          )}
        </div>
        {compare && compareWeights && (
          <div className="retrieval-config-column">
            <div className="type-label">{t("retrieval.weightsB")}</div>
            <WeightSliders weights={compareWeights} onChange={setCompareWeights} />
          </div>
        )}
      </div>

      <div className="retrieval-presets">
        <input
          type="text"
          placeholder={t("retrieval.presetName")}
          value={presetName}
          onChange={(event) => setPresetName(event.currentTarget.value)}
        />
        <button type="button" onClick={handleSavePreset} disabled={!presetName.trim()}>
          {t("retrieval.savePreset")}
        </button>
        {presets.length > 0 && (
          <select onChange={(event) => handleLoadPreset(event.currentTarget.value)} defaultValue="">
            <option value="" disabled>
              {t("retrieval.loadPreset")}
            </option>
            {presets.map((preset) => (
              <option key={preset.name} value={preset.name}>
                {preset.name}
              </option>
            ))}
          </select>
        )}
      </div>

      {mutation.isError && <p className="retrieval-error">{String(mutation.error)}</p>}

      <div className="retrieval-results-row">
        <ResultsColumn result={mutation.data} />
        {compare && <ResultsColumn result={compareMutation.data} />}
      </div>
    </div>
  );
}
