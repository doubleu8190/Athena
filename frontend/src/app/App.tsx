import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";
import {
  createBrowserRouter,
  RouterProvider,
} from "react-router-dom";

import { AuthProvider } from "../features/auth/AuthProvider";
import { ErrorBoundary } from "../shared/ui/ErrorBoundary";
import { Toaster } from "../shared/ui/Toast";
import { TooltipProvider } from "../shared/ui/Tooltip";
import { AppRoutes } from "./AppRoutes";
import { queryClient as defaultQueryClient } from "./queryClient";

const appRouter = createBrowserRouter([
  {
    element: <AppRoutes />,
    path: "*",
  },
]);

interface AppProvidersProps extends PropsWithChildren {
  client?: QueryClient;
}

export function AppProviders({
  children,
  client = defaultQueryClient,
}: AppProvidersProps) {
  return (
    <QueryClientProvider client={client}>
      {/* 全局挂载 TooltipProvider 与 Sonner Toaster，确保运行时与测试环境一致可用。 */}
      <TooltipProvider>
        <AuthProvider>{children}</AuthProvider>
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export function App() {
  return (
    <ErrorBoundary>
      <AppProviders>
        <RouterProvider router={appRouter} />
      </AppProviders>
    </ErrorBoundary>
  );
}
