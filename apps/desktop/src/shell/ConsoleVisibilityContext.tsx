import { createContext, useContext, useState } from "react";

/** Lifted out of Console.tsx itself so the command palette's "Toggle console" (FABLE Sec.11) can reach the same state Console renders from, without a prop chain through AppShell. */
interface ConsoleVisibilityContextValue {
  visible: boolean;
  toggle: () => void;
}

const ConsoleVisibilityContext = createContext<ConsoleVisibilityContextValue | null>(null);

export function ConsoleVisibilityProvider({ children }: { children: React.ReactNode }) {
  const [visible, setVisible] = useState(true);
  return (
    <ConsoleVisibilityContext.Provider value={{ visible, toggle: () => setVisible((v) => !v) }}>
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
