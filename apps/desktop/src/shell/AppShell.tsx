import { useEffect, useState } from "react";
import { Outlet } from "react-router-dom";

import { ConnectionBanner } from "../connection/ConnectionBanner";
import { CommandPalette } from "./CommandPalette";
import { Console } from "./Console";
import { ConsoleVisibilityProvider } from "./ConsoleVisibilityContext";
import { EventStreamProvider } from "./EventStreamContext";
import { GlobalSearch } from "./GlobalSearch";
import { Inspector } from "./Inspector";
import { InspectorProvider } from "./InspectorContext";
import "./shell.css";
import { SideNav } from "./SideNav";
import { TopBar } from "./TopBar";

/** FABLE Sec.9's global layout: top bar, side nav, center workspace, right inspector, bottom console. */
export function AppShell() {
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);

  // FABLE Sec.11/12: Ctrl/Cmd+K for the command palette, Ctrl/Cmd+Shift+K for
  // global search -- global, not gated on focus, matching every app that
  // uses this shortcut family (VSCode, Slack, Linear...).
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "k") return;
      event.preventDefault();
      if (event.shiftKey) {
        setSearchOpen((v) => !v);
        setPaletteOpen(false);
      } else {
        setPaletteOpen((v) => !v);
        setSearchOpen(false);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <EventStreamProvider>
      <InspectorProvider>
        <ConsoleVisibilityProvider>
          <div className="app-shell">
            <TopBar onOpenPalette={() => setPaletteOpen(true)} onOpenSearch={() => setSearchOpen(true)} />
            <ConnectionBanner />
            <div className="app-shell-body">
              <SideNav />
              <main className="app-shell-center panel">
                <Outlet />
              </main>
              <Inspector />
            </div>
            <Console />
          </div>
          <CommandPalette
            open={paletteOpen}
            onClose={() => setPaletteOpen(false)}
            onOpenSearch={() => {
              setPaletteOpen(false);
              setSearchOpen(true);
            }}
          />
          <GlobalSearch open={searchOpen} onClose={() => setSearchOpen(false)} />
        </ConsoleVisibilityProvider>
      </InspectorProvider>
    </EventStreamProvider>
  );
}
