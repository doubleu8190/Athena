import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
  type InfiniteData,
  type QueryClient,
  type QueryKey,
} from "@tanstack/react-query";

import { listSessionFiles, uploadSessionFile } from "./api";
import {
  SESSION_FILE_PAGE_SIZE,
  type SessionFile,
  type SessionFilePage,
} from "./model";
import { sessionFileKeys } from "./queryKeys";

function isSessionFileMetadataEqual(
  current: SessionFile,
  next: SessionFile,
): boolean {
  return (
    current.extraction_status === next.extraction_status &&
    current.extracted_text_bytes === next.extracted_text_bytes &&
    current.extraction_error_code === next.extraction_error_code &&
    current.filename === next.filename &&
    current.label === next.label &&
    current.mime_type === next.mime_type &&
    current.size_bytes === next.size_bytes
  );
}

/**
 * 在线预览触发后端惰性补解析后，将最新文件元数据（如 extraction_status / extracted_text_bytes）
 * 原地同步回会话文件列表缓存；先按字段级判等，无变化时直接返回原缓存引用，避免触发 MessageRow 不必要重渲染。
 */
export function syncSessionFileInCache(
  queryClient: QueryClient,
  sessionId: string,
  updatedFile: SessionFile,
): void {
  queryClient.setQueryData<InfiniteData<SessionFilePage>>(
    sessionFileKeys.list(sessionId),
    (current) => {
      if (!current) {
        return current;
      }
      let changed = false;
      const nextPages = current.pages.map((page) => {
        let pageChanged = false;
        const nextItems = page.items.map((item) => {
          if (item.id !== updatedFile.id) {
            return item;
          }
          if (isSessionFileMetadataEqual(item, updatedFile)) {
            return item;
          }
          pageChanged = true;
          changed = true;
          return { ...item, ...updatedFile };
        });
        return pageChanged ? { ...page, items: nextItems } : page;
      });
      return changed ? { ...current, pages: nextPages } : current;
    },
  );
}

export function useSessionFiles(sessionId: string, enabled = true) {
  return useInfiniteQuery<
    SessionFilePage,
    Error,
    InfiniteData<SessionFilePage>,
    QueryKey,
    string | null
  >({
    enabled,
    getNextPageParam: (lastPage) =>
      lastPage.next_after_file_id ?? undefined,
    initialPageParam: null,
    queryFn: ({ pageParam }) =>
      listSessionFiles(
        sessionId,
        pageParam,
        SESSION_FILE_PAGE_SIZE,
      ),
    queryKey: sessionFileKeys.list(sessionId),
  });
}

export function useUploadSessionFileMutation(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      file,
      requestId,
    }: {
      file: File;
      requestId: string;
    }) => uploadSessionFile(sessionId, file, requestId),
    onSuccess: async (file: SessionFile) => {
      // 上传响应只保证新文件已发布；保留翻页游标并从服务端重新读取权威顺序。
      queryClient.setQueryData<InfiniteData<SessionFilePage>>(
        sessionFileKeys.list(sessionId),
        (current) =>
          current
            ? {
                ...current,
                pages: current.pages.map((page, index) =>
                  index === 0 &&
                  !page.items.some((item) => item.id === file.id)
                    ? { ...page, items: [file, ...page.items] }
                    : page,
                ),
              }
            : current,
      );
      await queryClient.invalidateQueries({
        queryKey: sessionFileKeys.list(sessionId),
      });
    },
  });
}
