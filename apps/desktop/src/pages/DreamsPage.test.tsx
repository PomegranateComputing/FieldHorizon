import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { DreamRunsResponse, ProposalSummaryModel, ProposalsResponse } from "../api/types";
import { DreamsPage } from "./DreamsPage";

const {
  getDreamRunsMock,
  getProposalsMock,
  getProposalMock,
  postApplyProposalMock,
  postRejectProposalMock,
} = vi.hoisted(() => ({
  getDreamRunsMock: vi.fn(),
  getProposalsMock: vi.fn(),
  getProposalMock: vi.fn(),
  postApplyProposalMock: vi.fn(),
  postRejectProposalMock: vi.fn(),
}));

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    getDreamRuns: getDreamRunsMock,
    getProposals: getProposalsMock,
    getProposal: getProposalMock,
    postApplyProposal: postApplyProposalMock,
    postRejectProposal: postRejectProposalMock,
  };
});

const PROPOSAL: ProposalSummaryModel = { number: 1, name: "the_veiled_machine", motif: "the veiled machine", count: 6 };

function renderDreamsPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DreamsPage />
    </QueryClientProvider>,
  );
}

describe("DreamsPage apply-confirm dialog accessibility (Phase UI-6 item 3)", () => {
  it("exposes role=dialog/aria-modal, focuses a control on open, and returns focus to the Apply button on Escape", async () => {
    getDreamRunsMock.mockResolvedValue({ schema_version: 1, runs: [] } satisfies DreamRunsResponse);
    getProposalsMock.mockResolvedValue({ schema_version: 1, proposals: [PROPOSAL] } satisfies ProposalsResponse);

    const user = userEvent.setup();
    renderDreamsPage();

    const applyButton = await screen.findByRole("button", { name: /apply|appliquer/i });
    await user.click(applyButton);

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("aria-labelledby");

    await waitFor(() => {
      expect(dialog.contains(document.activeElement)).toBe(true);
    });

    await user.keyboard("{Escape}");

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(document.activeElement).toBe(applyButton);
  });
});
