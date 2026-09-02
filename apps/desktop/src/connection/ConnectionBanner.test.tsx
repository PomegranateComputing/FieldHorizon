import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConnectionBanner } from "./ConnectionBanner";

const { useConnectionMock } = vi.hoisted(() => ({ useConnectionMock: vi.fn() }));
vi.mock("./ConnectionContext", () => ({ useConnection: useConnectionMock }));

describe("ConnectionBanner (FABLE Sec.17: connection loss / reconnecting / degraded)", () => {
  it("renders nothing while connected", () => {
    useConnectionMock.mockReturnValue({ phase: "connected", attempt: 0, retry: vi.fn() });
    const { container } = render(<ConnectionBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing during the initial connecting phase (that's StartupScreen's job)", () => {
    useConnectionMock.mockReturnValue({ phase: "connecting", attempt: 0, retry: vi.fn() });
    const { container } = render(<ConnectionBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the reconnecting banner with the real attempt count", () => {
    useConnectionMock.mockReturnValue({ phase: "reconnecting", attempt: 3, retry: vi.fn() });
    render(<ConnectionBanner />);
    expect(screen.getByText(/3/)).toBeInTheDocument();
  });

  it("shows a degraded banner", () => {
    useConnectionMock.mockReturnValue({ phase: "degraded", attempt: 0, retry: vi.fn() });
    render(<ConnectionBanner />);
    expect(screen.getByText(/backend/i)).toBeInTheDocument();
  });

  it("shows a disconnected banner with a working retry button", async () => {
    const retry = vi.fn();
    useConnectionMock.mockReturnValue({ phase: "disconnected", attempt: 8, retry });
    const user = userEvent.setup();
    render(<ConnectionBanner />);

    const retryButton = screen.getByRole("button");
    await user.click(retryButton);
    expect(retry).toHaveBeenCalledOnce();
  });
});
