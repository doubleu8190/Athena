import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import {
  createSession,
  deleteSession,
  getSession,
  listSessions,
  updateSession,
} from "./api";
import type {
  Session,
  SessionCreateInput,
  SessionListQuery,
  SessionUpdateInput,
} from "./model";
import { sessionKeys } from "./queryKeys";

async function invalidateSessionCaches(
  queryClient: ReturnType<typeof useQueryClient>,
): Promise<void> {
  await queryClient.invalidateQueries({ queryKey: sessionKeys.all });
}

export function useSessionList(
  query: SessionListQuery = {},
  enabled = true,
) {
  return useQuery({
    queryKey: sessionKeys.list(query),
    queryFn: () => listSessions(query),
    enabled,
  });
}

export function useRecentSessions() {
  return useSessionList({ page: 1, pageSize: 8 });
}

export function useSession(
  sessionId: string | null | undefined,
  enabled = true,
) {
  return useQuery({
    queryKey: sessionKeys.detail(sessionId ?? ""),
    queryFn: () => getSession(sessionId ?? ""),
    enabled: Boolean(sessionId) && enabled,
  });
}

export function useCreateSessionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: SessionCreateInput) => createSession(input),
    onSuccess: async () => {
      await invalidateSessionCaches(queryClient);
    },
  });
}

export function useRenameSessionMutation(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: SessionUpdateInput) =>
      updateSession(sessionId, input),
    onSuccess: async (session: Session) => {
      queryClient.setQueryData(sessionKeys.detail(sessionId), session);
      await invalidateSessionCaches(queryClient);
    },
  });
}

export function useDeleteSessionMutation(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => deleteSession(sessionId),
    onSuccess: async () => {
      queryClient.removeQueries({
        queryKey: sessionKeys.detail(sessionId),
      });
      await invalidateSessionCaches(queryClient);
    },
  });
}

