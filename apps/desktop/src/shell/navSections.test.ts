import { describe, expect, it } from "vitest";

import { resolveNavVisibility } from "./navSections";
import type { NavSection } from "./navSections";

const gatedSection: NavSection = { id: "contradictions", label: "CONTRADICTIONS", path: "/contradictions", capabilityKey: "contradictions" };
const ungatedSection: NavSection = { id: "command", label: "COMMAND", path: "/command" };

describe("resolveNavVisibility", () => {
  it("is visible when the section has no capabilityKey at all", () => {
    expect(resolveNavVisibility(ungatedSection, null)).toBe("visible");
    expect(resolveNavVisibility(ungatedSection, { contradictions: { status: "absent" } })).toBe("visible");
  });

  it("is visible while capabilities haven't loaded yet -- never hidden by default", () => {
    expect(resolveNavVisibility(gatedSection, null)).toBe("visible");
  });

  it("is visible when the real capability status is present", () => {
    expect(resolveNavVisibility(gatedSection, { contradictions: { status: "present" } })).toBe("visible");
  });

  it("is marked experimental when the real capability status is partial", () => {
    expect(resolveNavVisibility(gatedSection, { contradictions: { status: "partial" } })).toBe("experimental");
  });

  it("is hidden when the real capability status is absent", () => {
    expect(resolveNavVisibility(gatedSection, { contradictions: { status: "absent" } })).toBe("hidden");
  });

  it("is visible when the section's capabilityKey isn't present in the response at all", () => {
    expect(resolveNavVisibility(gatedSection, { some_other_key: { status: "present" } })).toBe("visible");
  });
});
