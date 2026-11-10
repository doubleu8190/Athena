import {
  createContext,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type PropsWithChildren,
} from "react";
import { useLocation, useNavigationType } from "react-router-dom";

import {
  isProjectNotFound,
  projectErrorMessage,
} from "../projects/errorMessages";
import { useProject } from "../projects/hooks";

interface DraftChatState {
  contextNotice: string | null;
  contextReady: boolean;
  draft: string;
  projectId: string | null;
}

interface DraftChatEntry extends DraftChatState {
  projectContext: "manual" | "pending" | "ready";
}

interface DraftChatStore {
  activeKey: string | null;
  entries: Record<string, DraftChatEntry>;
}

export interface SessionSubmissionHandoff {
  draft: string;
  error: string | null;
  requestId: string;
  reuseRequestId: boolean;
}

interface DraftChatContextValue extends DraftChatState {
  chooseProject: (projectId: string | null) => void;
  clearSessionHandoff: (sessionId: string) => void;
  getSessionHandoff: (
    sessionId: string,
  ) => SessionSubmissionHandoff | null;
  setDraft: (draft: string) => void;
  setSessionHandoff: (
    sessionId: string,
    handoff: SessionSubmissionHandoff,
  ) => void;
}

const DraftChatContext = createContext<DraftChatContextValue | null>(null);

function requestedProjectIdFromLocation(
  pathname: string,
  search: string,
): string | null {
  if (pathname !== "/chat/new") {
    return null;
  }
  return new URLSearchParams(search).get("project");
}

function isDraftRoute(pathname: string): boolean {
  return pathname === "/chat" || pathname === "/chat/new";
}

function createEntry(requestedProjectId: string | null): DraftChatEntry {
  return {
    contextNotice: null,
    contextReady: !requestedProjectId,
    draft: "",
    projectContext: requestedProjectId ? "pending" : "ready",
    projectId: null,
  };
}

export function DraftChatProvider({ children }: PropsWithChildren) {
  const location = useLocation();
  const navigationType = useNavigationType();
  const requestedProjectId = requestedProjectIdFromLocation(
    location.pathname,
    location.search,
  );
  const requestedProject = useProject(requestedProjectId);
  const [store, setStore] = useState<DraftChatStore>(() => {
    if (!isDraftRoute(location.pathname)) {
      return { activeKey: null, entries: {} };
    }
    return {
      activeKey: location.key,
      entries: { [location.key]: createEntry(requestedProjectId) },
    };
  });
  const sessionHandoffsRef = useRef<Record<string, SessionSubmissionHandoff>>(
    {},
  );

  useLayoutEffect(() => {
    if (!isDraftRoute(location.pathname)) {
      return;
    }

    setStore((current) => {
      if (current.activeKey === location.key) {
        return current;
      }
      // 新历史项按显式新建清空；POP 返回时恢复该历史项原先的草稿快照。
      const savedEntry =
        navigationType === "POP" ? current.entries[location.key] : undefined;
      const entry = savedEntry ?? createEntry(requestedProjectId);
      return {
        activeKey: location.key,
        entries: { ...current.entries, [location.key]: entry },
      };
    });
  }, [
    location.key,
    location.pathname,
    navigationType,
    requestedProjectId,
  ]);

  useLayoutEffect(() => {
    if (
      !isDraftRoute(location.pathname) ||
      !requestedProjectId
    ) {
      return;
    }

    setStore((current) => {
      const entry = current.entries[location.key];
      if (
        current.activeKey !== location.key ||
        !entry ||
        entry.projectContext !== "pending"
      ) {
        return current;
      }

      let nextEntry: DraftChatEntry;
      if (requestedProject.data) {
        nextEntry = {
          ...entry,
          contextNotice: null,
          contextReady: true,
          projectContext: "ready",
          projectId: requestedProject.data.id,
        };
      } else if (requestedProject.isError) {
        // 跨用户项目与不存在项目使用同一回退文案，不暴露资源是否存在。
        nextEntry = {
          ...entry,
          contextNotice: isProjectNotFound(requestedProject.error)
            ? "该项目不可访问，已切换为未归属项目。"
            : projectErrorMessage(requestedProject.error),
          contextReady: true,
          projectContext: "ready",
          projectId: null,
        };
      } else {
        nextEntry = {
          ...entry,
          contextNotice: null,
          contextReady: false,
          projectId: null,
        };
      }

      return {
        ...current,
        entries: { ...current.entries, [location.key]: nextEntry },
      };
    });
  }, [
    location.key,
    location.pathname,
    requestedProject.data,
    requestedProject.error,
    requestedProject.isError,
    requestedProjectId,
  ]);

  const chooseProject = useCallback((projectId: string | null) => {
    setStore((current) => {
      if (!current.activeKey) {
        return current;
      }
      const entry = current.entries[current.activeKey];
      if (!entry) {
        return current;
      }
      return {
        ...current,
        entries: {
          ...current.entries,
          [current.activeKey]: {
            ...entry,
            contextNotice: null,
            contextReady: true,
            projectContext: "manual",
            projectId,
          },
        },
      };
    });
  }, []);

  const setDraft = useCallback((draft: string) => {
    setStore((current) => {
      if (!current.activeKey) {
        return current;
      }
      const entry = current.entries[current.activeKey];
      if (!entry) {
        return current;
      }
      return {
        ...current,
        entries: {
          ...current.entries,
          [current.activeKey]: {
            ...entry,
            draft,
          },
        },
      };
    });
  }, []);

  const setSessionHandoff = useCallback(
    (sessionId: string, handoff: SessionSubmissionHandoff) => {
      sessionHandoffsRef.current[sessionId] = handoff;
    },
    [],
  );

  const getSessionHandoff = useCallback(
    (sessionId: string) => sessionHandoffsRef.current[sessionId] ?? null,
    [],
  );

  const clearSessionHandoff = useCallback((sessionId: string) => {
    delete sessionHandoffsRef.current[sessionId];
  }, []);

  const activeEntry =
    (store.activeKey ? store.entries[store.activeKey] : null) ??
    createEntry(requestedProjectId);
  const {
    contextNotice,
    contextReady,
    draft,
    projectId,
  } = activeEntry;
  const value = useMemo(
    () => ({
      chooseProject,
      clearSessionHandoff,
      contextNotice,
      contextReady,
      draft,
      getSessionHandoff,
      projectId,
      setDraft,
      setSessionHandoff,
    }),
    [
      chooseProject,
      clearSessionHandoff,
      contextNotice,
      contextReady,
      draft,
      getSessionHandoff,
      projectId,
      setDraft,
      setSessionHandoff,
    ],
  );

  return (
    <DraftChatContext.Provider value={value}>
      {children}
    </DraftChatContext.Provider>
  );
}

export function useDraftChat(): DraftChatContextValue {
  const value = useContext(DraftChatContext);
  if (!value) {
    throw new Error("useDraftChat must be used within DraftChatProvider");
  }
  return value;
}
