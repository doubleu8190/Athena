import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";
import { BrowserRouter } from "react-router-dom";

import { AuthProvider } from "../features/auth/AuthProvider";
import { ErrorBoundary } from "../shared/ui/ErrorBoundary";
import { AppRoutes } from "./AppRoutes";
import { queryClient as defaultQueryClient } from "./queryClient";

interface AppProvidersProps extends PropsWithChildren {
  client?: QueryClient;
}

export function AppProviders({
  children,
  client = defaultQueryClient,
}: AppProvidersProps) {
  return (
    <QueryClientProvider client={client}>
      <AuthProvider>{children}</AuthProvider>
    </QueryClientProvider>
  );
}

export function App() {
  return (
    <ErrorBoundary>
      <AppProviders>
        <BrowserRouter>
          <AppRoutes />
        </BrowserRouter>
      </AppProviders>
    </ErrorBoundary>
  );
}
