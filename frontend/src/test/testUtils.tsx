import { QueryClient } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import {
  MemoryRouter,
  useLocation,
  useNavigate,
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

export function renderApp(initialEntry: string) {
  const client = createTestQueryClient();
  let currentLocation: {
    pathname: string;
    search: string;
  } | null = null;
  let navigate: ReturnType<typeof useNavigate> | null = null;

  function LocationCapture() {
    const location = useLocation();
    currentLocation = {
      pathname: location.pathname,
      search: location.search,
    };
    return null;
  }

  function NavigationCapture() {
    navigate = useNavigate();
    return null;
  }

  const view = render(
    <AppProviders client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <LocationCapture />
        <NavigationCapture />
        <AppRoutes />
      </MemoryRouter>
    </AppProviders>,
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
    navigateTo: (to: string) => {
      if (!navigate) {
        throw new Error("Router navigation is not available");
      }
      return navigate(to);
    },
    goBack: () => {
      if (!navigate) {
        throw new Error("Router navigation is not available");
      }
      return navigate(-1);
    },
  };
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    status,
  });
}
