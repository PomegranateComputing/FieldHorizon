import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { getRecentCycles } from "../api/client";
import { useConnection } from "../connection/ConnectionContext";
import { useCapabilities } from "../hooks/useCapabilities";
import { SUPPORTED_LANGUAGES } from "../i18n";
import type { SupportedLanguage } from "../i18n";
import { getPlatform } from "../platform";
import { useConsoleVisibility } from "./ConsoleVisibilityContext";
import { trapTabKey } from "./focusTrap";
import { NAV_SECTIONS, resolveNavVisibility } from "./navSections";
import "./CommandPalette.css";

interface Command {
  id: string;
  label: string;
  run: () => void | Promise<void>;
  destructive?: boolean;
}

/**
 * FABLE Sec.11: keyboard-accessible, searchable, consistent shortcuts,
 * confirmation on destructive ones. Every command here does something real
 * today -- "Export current view" (one of FABLE's own examples) is
 * deliberately absent until Phase UI-6 item 6 (Exports) actually exists;
 * adding it now would be a command that opens nothing.
 */
export function CommandPalette({ open, onClose, onOpenSearch }: { open: boolean; onClose: () => void; onOpenSearch: () => void }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const capabilities = useCapabilities();
  const { canManageBackend, canRestartManagedBackend, restartManagedBackend } = useConnection();
  const { toggle: toggleConsole } = useConsoleVisibility();
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const capabilityMap = capabilities.status === "loaded" ? capabilities.data.capabilities : null;

  const commands = useMemo((): Command[] => {
    const list: Command[] = [];

    for (const section of NAV_SECTIONS) {
      if (resolveNavVisibility(section, capabilityMap) === "hidden") continue;
      list.push({ id: `nav-${section.id}`, label: t("commandPalette.open", { section: section.label }), run: () => navigate(section.path) });
    }

    for (const lang of SUPPORTED_LANGUAGES as readonly SupportedLanguage[]) {
      if (lang === i18n.language) continue;
      list.push({
        id: `lang-${lang}`,
        label: t("commandPalette.switchLanguage", { language: lang.toUpperCase() }),
        run: () => void i18n.changeLanguage(lang),
      });
    }

    list.push({ id: "toggle-console", label: t("commandPalette.toggleConsole"), run: toggleConsole });

    list.push({ id: "search-sources", label: t("commandPalette.searchSources"), run: onOpenSearch });

    list.push({
      id: "open-latest-cycle",
      label: t("commandPalette.openLatestCycle"),
      run: async () => {
        const recent = await getRecentCycles(1);
        if (recent.entries.length > 0) navigate(`/provenance?id=${recent.entries[0].cycle_id}`);
      },
    });

    if (canManageBackend && canRestartManagedBackend) {
      list.push({
        id: "restart-backend",
        label: t("commandPalette.restartBackend"),
        destructive: true,
        run: async () => {
          const platform = await getPlatform();
          const confirmed = await platform.confirm(t("commandPalette.restartBackendConfirmBody"), {
            title: t("commandPalette.restartBackendConfirmTitle"),
            kind: "warning",
          });
          if (confirmed) await restartManagedBackend();
        },
      });
    }

    return list;
  }, [capabilityMap, i18n, navigate, onOpenSearch, canManageBackend, canRestartManagedBackend, restartManagedBackend, toggleConsole, t]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return commands;
    return commands.filter((c) => c.label.toLowerCase().includes(needle));
  }, [commands, query]);

  useEffect(() => {
    if (open) {
      setQuery("");
      setActiveIndex(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  async function runCommand(command: Command) {
    onClose();
    await command.run();
  }

  if (!open) return null;

  return (
    <div className="palette-backdrop" onClick={onClose}>
      <div
        ref={containerRef}
        className="panel command-palette"
        role="dialog"
        aria-modal="true"
        aria-label={t("commandPalette.title")}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            onClose();
          } else if (event.key === "ArrowDown") {
            event.preventDefault();
            setActiveIndex((i) => Math.min(i + 1, filtered.length - 1));
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            setActiveIndex((i) => Math.max(i - 1, 0));
          } else if (event.key === "Enter") {
            event.preventDefault();
            const command = filtered[activeIndex];
            if (command) void runCommand(command);
          } else if (containerRef.current) {
            trapTabKey(event, containerRef.current);
          }
        }}
      >
        <input
          ref={inputRef}
          type="text"
          className="command-palette-input"
          placeholder={t("commandPalette.placeholder")}
          value={query}
          onChange={(event) => setQuery(event.currentTarget.value)}
          aria-label={t("commandPalette.title")}
        />
        <ul className="command-palette-list" role="listbox">
          {filtered.length === 0 && <li className="command-palette-empty">{t("commandPalette.noCommands")}</li>}
          {filtered.map((command, index) => (
            <li key={command.id} role="option" aria-selected={index === activeIndex}>
              <button
                type="button"
                className={`command-palette-item${index === activeIndex ? " command-palette-item--active" : ""}${command.destructive ? " command-palette-item--destructive" : ""}`}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => void runCommand(command)}
              >
                {command.label}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
