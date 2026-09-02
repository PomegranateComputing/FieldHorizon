import { useTranslation } from "react-i18next";

import { useConnection } from "./ConnectionContext";
import type { StartupStep } from "./types";
import "./startup.css";

function StepMarker({ status }: { status: StartupStep["status"] }) {
  if (status === "ok") return <span className="startup-step-marker startup-step-marker--ok">✓</span>;
  if (status === "degraded") return <span className="startup-step-marker startup-step-marker--degraded">!</span>;
  if (status === "error") return <span className="startup-step-marker startup-step-marker--error">✕</span>;
  return <span className="startup-step-marker startup-step-marker--pending">…</span>;
}

/**
 * FABLE Sec.10.1: a brief, functional startup screen showing the real steps,
 * with immediately actionable errors. Only shown before the first successful
 * connection -- once connected, a lost connection later shows a small banner
 * over the still-visible shell (ConnectionBanner), not this full screen again.
 */
export function StartupScreen() {
  const { t } = useTranslation();
  const { steps, retry, canManageBackend } = useConnection();

  const erroredStep = steps.find((step) => step.status === "error");

  return (
    <div className="startup-screen">
      <h1 className="type-literary">{t("startup.title")}</h1>
      <p className="type-label">{t("startup.subtitle")}</p>

      <ul className="startup-steps type-mono">
        {steps.map((step) => (
          <li key={step.id} className={`startup-step startup-step--${step.status}`}>
            <StepMarker status={step.status} />
            <span>{t(`startup.steps.${step.id}`)}</span>
            {step.detail && <span className="startup-step-detail">{step.detail}</span>}
          </li>
        ))}
      </ul>

      {erroredStep && (
        <div className="startup-error">
          <p>{t(erroredStep.remediationKey ?? "startup.errorHint")}</p>
          <button type="button" onClick={retry}>
            {t("startup.retry")}
          </button>
        </div>
      )}

      {erroredStep && canManageBackend && <ManagedBackendLaunchForm />}
    </div>
  );
}

function ManagedBackendLaunchForm() {
  const { t } = useTranslation();
  const { launchManagedBackend } = useConnection();
  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void launchManagedBackend({
      repoRoot: String(form.get("repoRoot") ?? ""),
      pythonBin: String(form.get("pythonBin") ?? ""),
      port: 8777,
      tokenFile: ".fh_token",
    });
  };

  return (
    <form className="startup-launch-form" onSubmit={handleSubmit}>
      <p className="type-label">{t("startup.launchManagedBackend")}</p>
      <label>
        {t("startup.repoRoot")}
        <input name="repoRoot" type="text" required />
      </label>
      <label>
        {t("startup.pythonBin")}
        <input name="pythonBin" type="text" required />
      </label>
      <button type="submit">{t("startup.launch")}</button>
    </form>
  );
}
