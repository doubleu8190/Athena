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

export interface UpdateSessionVariables {
  input: SessionUpdateInput;
  sessionId: string;
}

/** 动态更新任意会话的标题或项目归属，供侧栏内联重命名与项目移动弹窗共用。 */
export function useUpdateSessionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ input, sessionId }: UpdateSessionVariables) =>
      updateSession(sessionId, input),
    onSuccess: async (session: Session, variables) => {
      queryClient.setQueryData(
        sessionKeys.detail(variables.sessionId),
        session,
      );
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

/** 动态删除任意会话，供侧栏直接操作多条会话行而无需预先绑定单个 sessionId。 */
export function useDeleteAnySessionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: string) => deleteSession(sessionId),
    onSuccess: async (_data, sessionId) => {
      queryClient.removeQueries({
        queryKey: sessionKeys.detail(sessionId),
      });
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

