import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { TokenGate } from "./TokenGate";
import { getStoredToken } from "./tokenStorage";

describe("TokenGate", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("shows the paste-token form when no token is stored, not the children", () => {
    render(
      <TokenGate>
        <div>protected content</div>
      </TokenGate>,
    );

    expect(screen.queryByText("protected content")).not.toBeInTheDocument();
    expect(screen.getByLabelText(/api token|jeton api/i)).toBeInTheDocument();
  });

  it("never bundles a token: the paste form starts empty, not pre-filled", () => {
    render(
      <TokenGate>
        <div>protected content</div>
      </TokenGate>,
    );
    expect(screen.getByLabelText(/api token|jeton api/i)).toHaveValue("");
  });

  it("stores the token and renders children once a token is submitted", async () => {
    const user = userEvent.setup();
    render(
      <TokenGate>
        <div>protected content</div>
      </TokenGate>,
    );

    await user.type(screen.getByLabelText(/api token|jeton api/i), "my-real-token");
    await user.click(screen.getByRole("button", { name: /connect|connecter/i }));

    expect(screen.getByText("protected content")).toBeInTheDocument();
    expect(getStoredToken()).toBe("my-real-token");
  });

  it("renders children immediately when a token is already stored", () => {
    window.sessionStorage.setItem("field-horizon-api-token", "already-there");

    render(
      <TokenGate>
        <div>protected content</div>
      </TokenGate>,
    );

    expect(screen.getByText("protected content")).toBeInTheDocument();
  });
});
