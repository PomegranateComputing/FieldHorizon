import { NavLink } from "react-router-dom";

import { useCapabilities } from "../hooks/useCapabilities";
import { NAV_SECTIONS, resolveNavVisibility } from "./navSections";

export function SideNav() {
  const capabilities = useCapabilities();
  const capabilityMap = capabilities.status === "loaded" ? capabilities.data.capabilities : null;

  return (
    <nav className="panel side-nav" aria-label="Field Horizon sections">
      {NAV_SECTIONS.map((section) => {
        const visibility = resolveNavVisibility(section, capabilityMap);
        if (visibility === "hidden") {
          return null;
        }
        return (
          <NavLink
            key={section.id}
            to={section.path}
            className={({ isActive }) => `side-nav-item type-label${isActive ? " side-nav-item--active" : ""}`}
          >
            {section.label}
            {visibility === "experimental" && <span className="side-nav-experimental-badge">EXP</span>}
          </NavLink>
        );
      })}
    </nav>
  );
}
