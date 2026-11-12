import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
  type InfiniteData,
  type QueryKey,
} from "@tanstack/react-query";

import { listSessionFiles, uploadSessionFile } from "./api";
import {
  SESSION_FILE_PAGE_SIZE,
  type SessionFile,
  type SessionFilePage,
} from "./model";
import { sessionFileKeys } from "./queryKeys";

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
