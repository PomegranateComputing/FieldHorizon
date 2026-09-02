import { createContext, useContext, useState } from "react";

/**
 * FABLE Sec.9's right inspector is contextual to whatever's selected in the
 * center workspace -- one shared selection, not a per-screen ad hoc prop
 * chain, since later screens (RETRIEVAL, CYCLES, PROVENANCE) will set it too.
 * A plain discriminated-union id (not JSX) so Inspector.tsx owns how each
 * kind fetches and renders its own detail -- keeps data-fetching in one
 * place instead of scattered across every screen that can select something.
 */
export type InspectorSelection = { kind: "source"; id: number } | { kind: "school"; id: number } | null;

interface InspectorContextValue {
  selection: InspectorSelection;
  select: (selection: InspectorSelection) => void;
}

const InspectorContext = createContext<InspectorContextValue | null>(null);

export function InspectorProvider({ children }: { children: React.ReactNode }) {
  const [selection, select] = useState<InspectorSelection>(null);
  return <InspectorContext.Provider value={{ selection, select }}>{children}</InspectorContext.Provider>;
}

export function useInspectorSelection(): InspectorContextValue {
  const value = useContext(InspectorContext);
  if (!value) {
    throw new Error("useInspectorSelection must be used within an InspectorProvider");
  }
  return value;
}
