import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { GlobalSearch } from "./GlobalSearch";

const { getSourcesMock, postRetrieveMock, getRegistryListMock, getContradictionsMock, getActiveCanonMock, navigateMock, selectMock } =
  vi.hoisted(() => ({
    getSourcesMock: vi.fn(),
    postRetrieveMock: vi.fn(),
    getRegistryListMock: vi.fn(),
    getContradictionsMock: vi.fn(),
    getActiveCanonMock: vi.fn(),
    navigateMock: vi.fn(),
    selectMock: vi.fn(),
  }));

vi.mock("../api/client", () => ({
  getSources: getSourcesMock,
  postRetrieve: postRetrieveMock,
  getRegistryList: getRegistryListMock,
  getContradictions: getContradictionsMock,
  getActiveCanon: getActiveCanonMock,
}));
vi.mock("./EventStreamContext", () => ({ useEventStreamContext: () => ({ events: [], connected: true }) }));
vi.mock("./InspectorContext", () => ({ useInspectorSelection: () => ({ select: selectMock }) }));
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigateMock };
});

function renderSearch() {
  return render(
    <MemoryRouter>
      <GlobalSearch open onClose={vi.fn()} />
    </MemoryRouter>,
  );
}

describe("GlobalSearch (FABLE Sec.12: results grouped by type, real backend endpoints only)", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
    getRegistryListMock.mockResolvedValue({ schema_version: 1, entries: [] });
    getContradictionsMock.mockResolvedValue({ schema_version: 1, contradictions: [] });
    getActiveCanonMock.mockResolvedValue({ schema_version: 1, entries: [] });
    getSourcesMock.mockResolvedValue({ schema_version: 1, sources: [], total: 0 });
    postRetrieveMock.mockResolvedValue({ schema_version: 1, candidates: [] });
  });

  it("debounces typing into a single real /sources and /retrieve call, grouped under Sources / Chunks", async () => {
    getSourcesMock.mockResolvedValue({
      schema_version: 1,
      sources: [{ id: 1, title: "The Veiled Machine", source_type: "book", manifest_id: null, weight: 1 }],
      total: 1,
    });
    postRetrieveMock.mockResolvedValue({
      schema_version: 1,
      candidates: [
        {
          chunk_id: 7,
          canonical_ref: "ref-7",
          content: "an excerpt about the veiled machine",
          source_title: "The Veiled Machine",
          source_type: "book",
          score: 0.9,
          components: {},
        },
      ],
    });
    const user = userEvent.setup({ delay: null });

    renderSearch();
    await user.type(screen.getByRole("textbox"), "veiled");

    await waitFor(() => expect(getSourcesMock).toHaveBeenCalled(), { timeout: 2000 });
    expect(postRetrieveMock).toHaveBeenCalledWith(
      expect.objectContaining({ query: "veiled", explain: false, plan: false }),
    );

    expect(await screen.findByText("Sources")).toBeInTheDocument();
    expect(screen.getAllByText("The Veiled Machine").length).toBeGreaterThan(0);
    expect(screen.getByText("Chunks / results")).toBeInTheDocument();

    // Debounced: one keystroke sequence should not fire a request per character.
    expect(getSourcesMock.mock.calls.length).toBeLessThan(6);
  });

  it("selecting a source result navigates to CORPUS and selects it in the Inspector", async () => {
    getSourcesMock.mockResolvedValue({
      schema_version: 1,
      sources: [{ id: 42, title: "Test Manifesto", source_type: "manifesto", manifest_id: null, weight: 1 }],
      total: 1,
    });
    const user = userEvent.setup({ delay: null });

    renderSearch();
    await user.type(screen.getByRole("textbox"), "mein");
    const result = await screen.findByRole("button", { name: /test manifesto/i });
    await user.click(result);

    expect(navigateMock).toHaveBeenCalledWith("/corpus");
    expect(selectMock).toHaveBeenCalledWith({ kind: "source", id: 42 });
  });

  it("always offers a direct lineage/provenance jump for the typed query", async () => {
    const user = userEvent.setup({ delay: null });
    renderSearch();
    await user.type(screen.getByRole("textbox"), "0007");

    const jump = await screen.findByRole("button", { name: /open lineage.*0007/i });
    await user.click(jump);
    expect(navigateMock).toHaveBeenCalledWith("/provenance?id=0007");
  });
});
