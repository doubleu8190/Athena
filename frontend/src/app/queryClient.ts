import { QueryClient } from "@tanstack/react-query";

export const ADMIN_QUERY_ROOT = ["admin"] as const;

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: false,
      staleTime: 30_000,
    },
    mutations: {
      retry: false,
    },
  },
});
