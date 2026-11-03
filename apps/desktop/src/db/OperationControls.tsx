import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { ControlAction, ControlResponse } from "./api";
import { dbApi } from "./api";
import { ConfirmDialog, ErrorBox } from "./kit";

/**
 * Pause / resume / cancel, with their real semantics stated before and after:
 * each is a journaled request the operation applies at its next safe point
 * (never inside a DDL statement). The backend refuses what does not apply to
 * the operation's current state; the buttons only reflect it.
 */
export function OperationControls({ operationId, status, available }: { operationId: string; status: string; available: string[] }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [last, setLast] = useState<ControlResponse | null>(null);
  const control = useMutation({
    mutationFn: (action: ControlAction) => dbApi.control(operationId, action),
    onSuccess: (response) => {
      setLast(response);
      void client.invalidateQueries({ queryKey: ["db"] });
    },
  });
  if (!available.length && status !== "CANCELLING") {
    return null;
  }
  return (
    <div className="ops-controls" role="group" aria-label={t("ops.controls.label")}>
      <div className="ops-controls-buttons">
        {available.includes("pause") && (
          <button type="button" className="ops-btn" disabled={control.isPending} onClick={() => control.mutate("pause")}
                  aria-describedby="ops-control-semantics">
            <span aria-hidden="true">❚❚ </span>{t("ops.controls.pause")}
          </button>
        )}
        {available.includes("resume") && (
          <button type="button" className="ops-btn ops-btn--primary" disabled={control.isPending} onClick={() => control.mutate("resume")}>
            <span aria-hidden="true">▶ </span>{t("ops.controls.resume")}
          </button>
        )}
        {available.includes("cancel") && (
          <button type="button" className="ops-btn ops-btn--danger" disabled={control.isPending} onClick={() => setConfirmCancel(true)}>
            <span aria-hidden="true">⨯ </span>{t("ops.controls.cancel")}
          </button>
        )}
      </div>
      <p id="ops-control-semantics" className="ops-help">
        {status === "CANCELLING" ? t("ops.controls.cancelling") : status === "PAUSED" ? t("ops.controls.pausedNote") : t("ops.controls.safePoint")}
      </p>
      {last && <p className="ops-note" role="status">{t("ops.controls.requested", { action: t(`ops.controls.${last.action}`) })} — {last.semantics}</p>}
      <ErrorBox error={control.error} />
      <ConfirmDialog open={confirmCancel} tone="danger" title={t("ops.controls.cancelTitle")} confirmLabel={t("ops.controls.cancelConfirm")}
                     onCancel={() => setConfirmCancel(false)} onConfirm={() => { setConfirmCancel(false); control.mutate("cancel"); }}>
        <p>{t("ops.controls.cancelBody")}</p>
      </ConfirmDialog>
    </div>
  );
}
