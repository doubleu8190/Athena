import { QueryClient } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { StrictMode } from "react";
import {
  createMemoryRouter,
  RouterProvider,
  useLocation,
} from "react-router-dom";

import { AppRoutes } from "../app/AppRoutes";
import { AppProviders } from "../app/App";

export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        gcTime: Infinity,
        retry: false,
      },
      mutations: {
        retry: false,
      },
    },
  });
}

interface RenderAppOptions {
  initialIndex?: number;
  seedQueryClient?: (client: QueryClient) => void;
  strictMode?: boolean;
}

export function renderApp(
  initialEntry: string | string[],
  options: RenderAppOptions = {},
) {
  const client = createTestQueryClient();
  options.seedQueryClient?.(client);
  let currentLocation: {
    pathname: string;
    search: string;
  } | null = null;

  function LocationCapture() {
    const location = useLocation();
    currentLocation = {
      pathname: location.pathname,
      search: location.search,
    };
    return null;
  }

  function RouterRoot() {
    return (
      <>
        <LocationCapture />
        <AppRoutes />
      </>
    );
  }

  const router = createMemoryRouter(
    [
      {
        element: <RouterRoot />,
        path: "*",
      },
    ],
    {
      initialEntries:
        typeof initialEntry === "string" ? [initialEntry] : initialEntry,
      initialIndex: options.initialIndex,
    },
  );
  const appTree = (
    <AppProviders client={client}>
      <RouterProvider router={router} />
    </AppProviders>
  );
  const view = render(
    options.strictMode ? <StrictMode>{appTree}</StrictMode> : appTree,
  );

  return {
    ...view,
    client,
    getLocation: () => {
      if (!currentLocation) {
        throw new Error("Router location is not available");
      }
      return currentLocation;
    },
    navigateTo: (to: string) => router.navigate(to),
    goBack: () => router.navigate(-1),
    goForward: () => router.navigate(1),
    router,
  };
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    status,
  });
}
