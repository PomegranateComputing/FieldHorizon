import { Suspense, lazy } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { ConnectionProvider, useConnection } from "./connection/ConnectionContext";
import { StartupScreen } from "./connection/StartupScreen";
import { CommandPage } from "./pages/CommandPage";
import { CorpusPage } from "./pages/CorpusPage";
import { CyclesPage } from "./pages/CyclesPage";
import { ContradictionsPage } from "./pages/ContradictionsPage";
import { CouncilsPage } from "./pages/CouncilsPage";
import { DreamsPage } from "./pages/DreamsPage";
import { EvaluationPage } from "./pages/EvaluationPage";
import { ModelsPage } from "./pages/ModelsPage";
import { PlannerPage } from "./pages/PlannerPage";
import { ProvenancePage } from "./pages/ProvenancePage";
import { RetrievalPage } from "./pages/RetrievalPage";
import { SchoolsPage } from "./pages/SchoolsPage";
import { SectionPage } from "./pages/SectionPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SystemPage } from "./pages/SystemPage";
import { AppShell } from "./shell/AppShell";
import { NAV_SECTIONS } from "./shell/navSections";

// Chart/graph-heavy screens pull in echarts or React Flow -- lazy-loaded
// so every other screen's first paint doesn't pay for a charting library
// it never uses (FABLE Sec.15).
const WeatherPage = lazy(() => import("./pages/WeatherPage").then((m) => ({ default: m.WeatherPage })));
const CanonPage = lazy(() => import("./pages/CanonPage").then((m) => ({ default: m.CanonPage })));
const SemanticsPage = lazy(() => import("./pages/SemanticsPage").then((m) => ({ default: m.SemanticsPage })));

function ScreenFallback() {
  const { t } = useTranslation();
  return <p className="section-page">{t("command.loading")}</p>;
}

/**
 * The built assets are identical for both delivery targets (Tauri and the
 * /ui browser mount), but the correct router basename differs between them
 * -- Tauri serves dist/index.html at its own root, the browser build is
 * nested under /ui (Phase UI-3 item 2's static mount). Checked at runtime
 * from the actual URL rather than a build-time flag, since one build serves
 * both.
 */
const basename = window.location.pathname.startsWith("/ui") ? "/ui" : "/";

/** Filled in one screen at a time (Phase UI-4/UI-5); anything absent here still gets SectionPage's honest "not built yet" stub. */
const SCREEN_COMPONENTS: Partial<Record<string, React.ComponentType>> = {
  canon: CanonPage,
  command: CommandPage,
  corpus: CorpusPage,
  retrieval: RetrievalPage,
  cycles: CyclesPage,
  councils: CouncilsPage,
  contradictions: ContradictionsPage,
  dreams: DreamsPage,
  evaluation: EvaluationPage,
  models: ModelsPage,
  planner: PlannerPage,
  provenance: ProvenancePage,
  schools: SchoolsPage,
  semantics: SemanticsPage,
  settings: SettingsPage,
  system: SystemPage,
  weather: WeatherPage,
};

function Gate() {
  const { phase } = useConnection();

  // Only the *first* connection attempt blocks with the full startup screen
  // (FABLE Sec.10.1). Once connected at least once, a later drop shows the
  // shell with stale data plus ConnectionBanner (Phase UI-3 item 5) instead
  // of taking the whole screen over again -- FABLE Sec.17's "no blank pages."
  if (phase === "connecting") {
    return <StartupScreen />;
  }

  return (
    <BrowserRouter basename={basename}>
      <Routes>
        <Route path="/" element={<AppShell />}>
          <Route index element={<Navigate to="/command" replace />} />
          {NAV_SECTIONS.map((section) => {
            const Screen = SCREEN_COMPONENTS[section.id];
            return (
              <Route
                key={section.id}
                path={section.path}
                element={Screen ? <Suspense fallback={<ScreenFallback />}><Screen /></Suspense> : <SectionPage title={section.label} />}
              />
            );
          })}
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

function App() {
  return (
    <ConnectionProvider>
      <Gate />
    </ConnectionProvider>
  );
}

export default App;
