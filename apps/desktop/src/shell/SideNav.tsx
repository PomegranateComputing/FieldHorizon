import { useState } from "react";
import { NavLink } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { useCapabilities } from "../hooks/useCapabilities";
import { DB_SECTIONS, NAV_SECTIONS, resolveNavVisibility } from "./navSections";

const ADVANCED_OPEN_KEY = "field-horizon-nav-advanced-open";

/**
 * Two groups: the DB operations workstation first (DB-02's primary screens,
 * translated), then the corpus engine's screens under an "advanced" disclosure
 * -- a native <details>, so it opens with Enter/Space and stays in the DOM.
 */
export function SideNav() {
  const { t } = useTranslation();
  const capabilities = useCapabilities();
  const capabilityMap = capabilities.status === "loaded" ? capabilities.data.capabilities : null;
  const [advancedOpen, setAdvancedOpen] = useState(() => window.localStorage.getItem(ADVANCED_OPEN_KEY) !== "false");

  return (
    <nav className="panel side-nav" aria-label={t("ops.nav.groupOps")}>
      <div className="side-nav-group-title" id="nav-group-ops">{t("ops.nav.groupOps")}</div>
      <ul className="side-nav-list" aria-labelledby="nav-group-ops">
        {DB_SECTIONS.map((section) => (
          <li key={section.id}>
            <NavLink to={section.path} end={section.end}
                     className={({ isActive }) => `side-nav-item side-nav-item--ops${isActive ? " side-nav-item--active" : ""}`}>
              {section.labelKey ? t(section.labelKey) : section.label}
            </NavLink>
          </li>
        ))}
      </ul>
      <details className="side-nav-advanced" open={advancedOpen}
               onToggle={(e) => {
                 const open = (e.currentTarget as HTMLDetailsElement).open;
                 setAdvancedOpen(open);
                 window.localStorage.setItem(ADVANCED_OPEN_KEY, String(open));
               }}>
        <summary className="side-nav-group-title">{t("ops.nav.groupAdvanced")}</summary>
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
      </details>
    </nav>
  );
}
