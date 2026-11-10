export function newChatHref(projectId?: string | null): string {
  return projectId
    ? `/chat/new?project=${encodeURIComponent(projectId)}`
    : "/chat";
}

