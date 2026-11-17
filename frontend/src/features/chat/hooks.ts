import {
  useInfiniteQuery,
  useQuery,
  type InfiniteData,
  type QueryKey,
} from "@tanstack/react-query";

import {
  getRun,
  getRunToolCall,
  listMessages,
  listRuns,
  listRunToolCalls,
} from "./api";
import type {
  ConversationMessagePage,
  RunToolCallListResponse,
} from "./model";
import { chatKeys } from "./queryKeys";

export function useSessionMessages(sessionId: string, enabled = true) {
  return useInfiniteQuery<
    ConversationMessagePage,
    Error,
    InfiniteData<ConversationMessagePage>,
    QueryKey,
    number | undefined
  >({
    enabled,
    getNextPageParam: (lastPage) =>
      lastPage.next_before_sequence ?? undefined,
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) => listMessages(sessionId, pageParam, 20),
    queryKey: chatKeys.messages(sessionId),
  });
}

export function useLatestRun(sessionId: string, enabled = true) {
  return useQuery({
    enabled,
    queryFn: () => listRuns(sessionId, 1),
    queryKey: chatKeys.latestRun(sessionId),
  });
}

export function useRunDetails(
  sessionId: string,
  runId: string | null,
  enabled = true,
) {
  return useQuery({
    enabled: Boolean(runId) && enabled,
    queryFn: () => getRun(sessionId, runId ?? ""),
    queryKey: chatKeys.run(sessionId, runId ?? ""),
  });
}

export function useRunToolCalls(
  sessionId: string,
  runId: string | null,
  enabled = true,
) {
  return useInfiniteQuery<
    RunToolCallListResponse,
    Error,
    InfiniteData<RunToolCallListResponse>,
    QueryKey,
    number
  >({
    enabled: Boolean(runId) && enabled,
    getNextPageParam: (lastPage) =>
      lastPage.next_after_ordinal ?? undefined,
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      listRunToolCalls(sessionId, runId ?? "", pageParam, 100),
    queryKey: chatKeys.toolCalls(sessionId, runId ?? ""),
  });
}

export function useToolCallDetail(
  sessionId: string,
  runId: string,
  callId: string,
  enabled: boolean,
) {
  return useQuery({
    enabled,
    queryFn: () => getRunToolCall(sessionId, runId, callId),
    queryKey: chatKeys.toolCall(sessionId, runId, callId),
  });
}

