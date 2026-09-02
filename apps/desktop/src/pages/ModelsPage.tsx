import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { getRegistryList } from "../api/client";
import "./ModelsPage.css";

const KINDS = ["model", "prompt", "embedding"] as const;
type Kind = (typeof KINDS)[number];

export function ModelsPage() {
  const { t } = useTranslation();
  const [kind, setKind] = useState<Kind>("model");
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);

  const listQuery = useQuery({ queryKey: ["registry", kind], queryFn: () => getRegistryList(kind) });

  return (
    <div className="section-page models-page">
      <h1>MODELS</h1>
      <p className="models-intro">{t("models.intro")}</p>

      <div className="models-kind-tabs">
        {KINDS.map((k) => (
          <button
            key={k}
            type="button"
            className={k === kind ? "models-kind-tab--active" : ""}
            onClick={() => {
              setKind(k);
              setSelected(null);
            }}
          >
            {t(`models.kind.${k}`)}
          </button>
        ))}
      </div>

      {listQuery.status === "pending" && <p>{t("command.loading")}</p>}
      {listQuery.status === "error" && <p>{t("command.unreachable")}</p>}
      {listQuery.status === "success" && listQuery.data.entries.length === 0 && <p>{t("models.noneYet")}</p>}

      <div className="models-layout">
        <ul className="models-list">
          {listQuery.data?.entries.map((entry) => (
            <li key={String(entry.id ?? JSON.stringify(entry))}>
              <button type="button" onClick={() => setSelected(entry)} className="models-list-item">
                <span className="type-mono">{String(entry.id ?? entry.name ?? "")}</span>
                {"active" in entry && (
                  <span className={entry.active ? "models-active-badge" : "models-inactive-badge"}>
                    {entry.active ? t("models.active") : t("models.inactive")}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>

        {selected && (
          <div className="panel models-detail">
            <div className="type-label">{t("models.detail")}</div>
            <dl className="models-detail-fields type-mono">
              {Object.entries(selected).map(([key, value]) => (
                <div key={key} className="models-detail-row">
                  <dt>{key}</dt>
                  <dd>{String(value)}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}
      </div>
    </div>
  );
}
