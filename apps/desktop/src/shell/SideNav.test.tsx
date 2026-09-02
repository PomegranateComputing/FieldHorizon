import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { makeCapabilitiesResponse } from "../test/fixtures";
import { SideNav } from "./SideNav";

const { useCapabilitiesMock } = vi.hoisted(() => ({ useCapabilitiesMock: vi.fn() }));
vi.mock("../hooks/useCapabilities", () => ({ useCapabilities: useCapabilitiesMock }));

describe("SideNav capability gating", () => {
  it("shows a section normally when its capability is present", () => {
    useCapabilitiesMock.mockReturnValue({ status: "loaded", data: makeCapabilitiesResponse() });
    render(<SideNav />, { wrapper: MemoryRouter });

    expect(screen.getByText("RETRIEVAL")).toBeInTheDocument();
  });

  it("marks a partial capability's section as experimental, without hiding it", () => {
    useCapabilitiesMock.mockReturnValue({ status: "loaded", data: makeCapabilitiesResponse() });
    render(<SideNav />, { wrapper: MemoryRouter });

    const contradictionsLink = screen.getByText("CONTRADICTIONS").closest("a");
    expect(contradictionsLink).toBeInTheDocument();
    expect(contradictionsLink).toHaveTextContent("EXP");
  });

  it("hides a section entirely when its capability is absent", () => {
    useCapabilitiesMock.mockReturnValue({
      status: "loaded",
      data: makeCapabilitiesResponse({ weather: { status: "absent", detail: "" } }),
    });
    render(<SideNav />, { wrapper: MemoryRouter });

    expect(screen.queryByText("WEATHER")).not.toBeInTheDocument();
    // Unrelated present sections are unaffected.
    expect(screen.getByText("SCHOOLS")).toBeInTheDocument();
  });

  it("shows every gated section (not hidden) while capabilities are still loading", () => {
    useCapabilitiesMock.mockReturnValue({ status: "loading" });
    render(<SideNav />, { wrapper: MemoryRouter });

    expect(screen.getByText("WEATHER")).toBeInTheDocument();
    expect(screen.getByText("CONTRADICTIONS")).toBeInTheDocument();
  });

  it("always shows ungated sections regardless of capabilities", () => {
    useCapabilitiesMock.mockReturnValue({ status: "error", error: new Error("network down") });
    render(<SideNav />, { wrapper: MemoryRouter });

    expect(screen.getByText("COMMAND")).toBeInTheDocument();
    expect(screen.getByText("SETTINGS")).toBeInTheDocument();
  });
});
