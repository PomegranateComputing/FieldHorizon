import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import { TokenGate } from "./auth/TokenGate";
import "./i18n";
import "./styles/index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    // Server state, not client state (FABLE Sec.8.6): a modest stale time
    // avoids refetching on every focus/mount for data that's cheap to poll
    // but doesn't change every second, without masking real updates for long.
    queries: { staleTime: 5_000, refetchOnWindowFocus: true },
  },
});

const root = (
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <TokenGate>
        <App />
      </TokenGate>
    </QueryClientProvider>
  </React.StrictMode>
);

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(root);
