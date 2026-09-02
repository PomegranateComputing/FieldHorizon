import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { makeCapabilitiesResponse } from "../test/fixtures";
import { CommandPalette } from "./CommandPalette";
import { ConsoleVisibilityProvider } from "./ConsoleVisibilityContext";

const { useCapabilitiesMock, useConnectionMock, getRecentCyclesMock, navigateMock } = vi.hoisted(() => ({
  useCapabilitiesMock: vi.fn(),
  useConnectionMock: vi.fn(),
  getRecentCyclesMock: vi.fn(),
  navigateMock: vi.fn(),
}));

vi.mock("../hooks/useCapabilities", () => ({ useCapabilities: useCapabilitiesMock }));
vi.mock("../connection/ConnectionContext", () => ({ useConnection: useConnectionMock }));
vi.mock("../api/client", () => ({ getRecentCycles: getRecentCyclesMock }));
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigateMock };
});

function renderPalette(onOpenSearch = vi.fn()) {
  return render(
    <MemoryRouter>
      <ConsoleVisibilityProvider>
        <CommandPalette open onClose={vi.fn()} onOpenSearch={onOpenSearch} />
      </ConsoleVisibilityProvider>
    </MemoryRouter>,
  );
}

describe("CommandPalette (FABLE Sec.11)", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("lists every visible nav section as a searchable 'Open X' command and navigates on click", async () => {
    useCapabilitiesMock.mockReturnValue({ status: "loaded", data: makeCapabilitiesResponse() });
    useConnectionMock.mockReturnValue({ canManageBackend: false, canRestartManagedBackend: false, restartManagedBackend: vi.fn() });
    const user = userEvent.setup();

    renderPalette();

    const input = screen.getByRole("textbox");
    await user.type(input, "corpus");

    const item = await screen.findByRole("button", { name: /open corpus/i });
    await user.click(item);
    expect(navigateMock).toHaveBeenCalledWith("/corpus");
  });

  it("hides nav sections whose capability is absent, matching SideNav's own gating", async () => {
    useCapabilitiesMock.mockReturnValue({
      status: "loaded",
      data: makeCapabilitiesResponse({ weather: { status: "absent", detail: "" } }),
    });
    useConnectionMock.mockReturnValue({ canManageBackend: false, canRestartManagedBackend: false, restartManagedBackend: vi.fn() });
    const user = userEvent.setup();

    renderPalette();
    await user.type(screen.getByRole("textbox"), "weather");

    expect(screen.queryByRole("button", { name: /open weather/i })).not.toBeInTheDocument();
  });

  it("only offers 'Restart managed backend' once a managed backend has actually been launched", async () => {
    useCapabilitiesMock.mockReturnValue({ status: "loaded", data: makeCapabilitiesResponse() });
    useConnectionMock.mockReturnValue({ canManageBackend: true, canRestartManagedBackend: false, restartManagedBackend: vi.fn() });
    const user = userEvent.setup();

    renderPalette();
    await user.type(screen.getByRole("textbox"), "restart");

    expect(screen.queryByRole("button", { name: /restart/i })).not.toBeInTheDocument();
  });

  it("hands off to global search via the 'Search sources...' command", async () => {
    useCapabilitiesMock.mockReturnValue({ status: "loaded", data: makeCapabilitiesResponse() });
    useConnectionMock.mockReturnValue({ canManageBackend: false, canRestartManagedBackend: false, restartManagedBackend: vi.fn() });
    const onOpenSearch = vi.fn();
    const user = userEvent.setup();

    renderPalette(onOpenSearch);
    await user.type(screen.getByRole("textbox"), "search sources");
    await user.click(await screen.findByRole("button", { name: /search sources/i }));

    expect(onOpenSearch).toHaveBeenCalledOnce();
  });
});
