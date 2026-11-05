import { createContext, useContext, useState } from "react";

/** Lifted out of Console.tsx itself so the command palette's "Toggle console" (UI specification Sec.11) can reach the same state Console renders from, without a prop chain through AppShell. */
interface ConsoleVisibilityContextValue {
  visible: boolean;
  toggle: () => void;
}

const ConsoleVisibilityContext = createContext<ConsoleVisibilityContextValue | null>(null);

const CONSOLE_VISIBLE_KEY = "field-horizon-console-visible";

/**
 * Folded by default since DB-02 (the operations workstation needs the height at
 * 1366x768); the operator's own choice is remembered across sessions.
 */
export function ConsoleVisibilityProvider({ children }: { children: React.ReactNode }) {
  const [visible, setVisible] = useState(() => window.localStorage.getItem(CONSOLE_VISIBLE_KEY) === "true");
  const toggle = () =>
    setVisible((v) => {
      window.localStorage.setItem(CONSOLE_VISIBLE_KEY, String(!v));
      return !v;
    });
  return (
    <ConsoleVisibilityContext.Provider value={{ visible, toggle }}>
      {children}
    </ConsoleVisibilityContext.Provider>
  );
}

export function useConsoleVisibility(): ConsoleVisibilityContextValue {
  const value = useContext(ConsoleVisibilityContext);
  if (!value) {
    throw new Error("useConsoleVisibility must be used within a ConsoleVisibilityProvider");
  }
  return value;
}
