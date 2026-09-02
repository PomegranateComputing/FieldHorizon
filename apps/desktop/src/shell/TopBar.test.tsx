import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { makeCapabilitiesResponse, makeHealthResponse, makeSystemInfoResponse } from "../test/fixtures";
import { TopBar } from "./TopBar";

const { useHealthMock, useCapabilitiesMock, useSystemInfoMock, useEventStreamContextMock } = vi.hoisted(() => ({
  useHealthMock: vi.fn(),
  useCapabilitiesMock: vi.fn(),
  useSystemInfoMock: vi.fn(),
  useEventStreamContextMock: vi.fn(),
}));
vi.mock("../hooks/useHealth", () => ({ useHealth: useHealthMock }));
vi.mock("../hooks/useCapabilities", () => ({ useCapabilities: useCapabilitiesMock }));
vi.mock("../hooks/useSystemInfo", () => ({ useSystemInfo: useSystemInfoMock }));
vi.mock("./EventStreamContext", () => ({ useEventStreamContext: useEventStreamContextMock }));

describe("TopBar i18n switch", () => {
  beforeEach(() => {
    useHealthMock.mockReturnValue({ status: "loaded", data: makeHealthResponse() });
    useCapabilitiesMock.mockReturnValue({ status: "loaded", data: makeCapabilitiesResponse() });
    useSystemInfoMock.mockReturnValue({ status: "loaded", data: makeSystemInfoResponse() });
    useEventStreamContextMock.mockReturnValue({ events: [], connected: true });
  });

  afterEach(async () => {
    // i18next is a module-level singleton -- reset so other test files don't inherit a language switch made here.
    await i18n.changeLanguage("fr");
  });

  it("renders French labels by default (FABLE Sec.7: French is the recommended initial language)", async () => {
    await i18n.changeLanguage("fr");
    render(<TopBar onOpenPalette={() => {}} onOpenSearch={() => {}} />);
    expect(screen.getByText(/bdd/i)).toBeInTheDocument();
    expect(screen.getByText(/modèle/i)).toBeInTheDocument();
  });

  it("switches every translated label to English when EN is clicked", async () => {
    await i18n.changeLanguage("fr");
    const user = userEvent.setup();
    render(<TopBar onOpenPalette={() => {}} onOpenSearch={() => {}} />);

    await user.click(screen.getByRole("button", { name: "EN" }));

    expect(screen.getByText(/^db:/i)).toBeInTheDocument();
    expect(screen.getByText(/^model:/i)).toBeInTheDocument();
    expect(screen.queryByText(/bdd/i)).not.toBeInTheDocument();
  });

  it("switches back to French when FR is clicked after switching to English", async () => {
    await i18n.changeLanguage("en");
    const user = userEvent.setup();
    render(<TopBar onOpenPalette={() => {}} onOpenSearch={() => {}} />);

    await user.click(screen.getByRole("button", { name: "FR" }));

    expect(screen.getByText(/bdd/i)).toBeInTheDocument();
  });
});
