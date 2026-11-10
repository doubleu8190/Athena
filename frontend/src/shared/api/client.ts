const API_PREFIX = "/api/v1";

export const AUTH_UNAUTHORIZED_EVENT = "athena:auth-unauthorized";
export const ADMIN_FORBIDDEN_EVENT = "athena:admin-forbidden";

let authIdentityEpoch = 0;

export function advanceAuthIdentityEpoch(): void {
  authIdentityEpoch += 1;
}

export function getAuthIdentityEpoch(): number {
  return authIdentityEpoch;
}

export function isAuthIdentityCurrent(identityEpoch: number): boolean {
  return identityEpoch === authIdentityEpoch;
}

export function authIdentityChangedError(): DOMException {
  return new DOMException(
    "Authentication identity changed while the request was in flight",
    "AbortError",
  );
}

function assertAuthIdentityCurrent(identityEpoch: number): void {
  if (!isAuthIdentityCurrent(identityEpoch)) {
    throw authIdentityChangedError();
  }
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string, options?: ErrorOptions) {
    super(`API ${status}: ${detail}`, options);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

interface ApiRequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
}

interface ApiMultipartOptions extends Omit<RequestInit, "body"> {}

interface PerformedRequest {
  identityEpoch: number;
  response: Response;
}

function publishUnauthorized(): void {
  if (typeof window !== "undefined") {
    // 所有 API 共用同一事件，后续业务模块只需清理受保护缓存，不各自处理 401。
    window.dispatchEvent(new CustomEvent(AUTH_UNAUTHORIZED_EVENT));
  }
}

function publishAdminForbidden(): void {
  if (typeof window !== "undefined") {
    // 管理接口的最终权限复核失败时，由应用根节点统一清空管理缓存并重新核验身份。
    window.dispatchEvent(new CustomEvent(ADMIN_FORBIDDEN_EVENT));
  }
}

async function readErrorDetail(response: Response): Promise<string> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.toLowerCase().includes("application/json")) {
    try {
      const payload: unknown = await response.json();
      if (
        typeof payload === "object" &&
        payload !== null &&
        "detail" in payload &&
        typeof payload.detail === "string"
      ) {
        return payload.detail;
      }
    } catch {
      // 服务端异常时可能声明 JSON 但返回截断内容，统一降级为稳定错误码。
    }
  }
  return "http_error";
}

async function performRequest(
  path: string,
  requestInit: RequestInit,
): Promise<PerformedRequest> {
  const requestIdentityEpoch = getAuthIdentityEpoch();
  const headers = new Headers(requestInit.headers);
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(`${API_PREFIX}${path}`, {
      ...requestInit,
      credentials: "same-origin",
      headers,
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw error;
    }
    assertAuthIdentityCurrent(requestIdentityEpoch);
    throw new ApiError(0, "network_error", { cause: error });
  }

  // 响应头到达只代表请求仍在同一身份下，错误体读取前仍需持续校验。
  assertAuthIdentityCurrent(requestIdentityEpoch);

  if (!response.ok) {
    const detail = await readErrorDetail(response);
    // 错误体读取可能跨过登录/退出边界，过期请求不得发布权限事件。
    assertAuthIdentityCurrent(requestIdentityEpoch);
    if (response.status === 401) {
      publishUnauthorized();
    }
    if (response.status === 403 && detail === "admin_required") {
      publishAdminForbidden();
    }
    throw new ApiError(response.status, detail);
  }
  return { identityEpoch: requestIdentityEpoch, response };
}

async function parseJsonResponse<T>({
  identityEpoch,
  response,
}: PerformedRequest): Promise<T> {
  assertAuthIdentityCurrent(identityEpoch);
  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().includes("application/json")) {
    throw new ApiError(response.status, "invalid_response");
  }

  let payload: T;
  try {
    payload = (await response.json()) as T;
  } catch (error) {
    assertAuthIdentityCurrent(identityEpoch);
    throw new ApiError(response.status, "invalid_response", { cause: error });
  }
  // 响应体读取完成后再次校验，避免身份切换后把旧用户数据交给 Query/Mutation。
  assertAuthIdentityCurrent(identityEpoch);
  return payload;
}

export async function apiRequest<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const { body, headers: suppliedHeaders, ...requestInit } = options;
  const headers = new Headers(suppliedHeaders);
  if (body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  const request = await performRequest(path, {
    ...requestInit,
    body: body === undefined ? undefined : JSON.stringify(body),
    headers,
  });
  return parseJsonResponse<T>(request);
}

export async function apiMultipart<T>(
  path: string,
  body: FormData,
  options: ApiMultipartOptions = {},
): Promise<T> {
  // 浏览器必须自行生成 multipart boundary，不能复用 JSON 请求的 Content-Type。
  const request = await performRequest(path, {
    ...options,
    body,
  });
  return parseJsonResponse<T>(request);
}

export interface DownloadedFile {
  blob: Blob;
  filename: string;
}

function safeDecodeFilename(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

export function filenameFromContentDisposition(
  value: string | null,
): string | null {
  if (!value) {
    return null;
  }
  const encoded = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(value);
  if (encoded?.[1]) {
    return safeDecodeFilename(encoded[1].trim().replace(/^"|"$/g, ""));
  }
  const plain = /filename\s*=\s*(?:"([^"]+)"|([^;]+))/i.exec(value);
  const candidate = plain?.[1] ?? plain?.[2];
  return candidate ? safeDecodeFilename(candidate.trim()) : null;
}

export async function apiDownload(
  path: string,
  options: Omit<RequestInit, "body"> = {},
): Promise<DownloadedFile> {
  const headers = new Headers(options.headers);
  if (!headers.has("Accept")) {
    headers.set("Accept", "*/*");
  }
  const { identityEpoch, response } = await performRequest(path, {
    ...options,
    headers,
  });
  let blob: Blob;
  try {
    blob = await response.blob();
  } catch (error) {
    assertAuthIdentityCurrent(identityEpoch);
    throw error;
  }
  // Blob 可能较大且读取耗时，返回前必须确认仍属于当前身份。
  assertAuthIdentityCurrent(identityEpoch);
  return {
    blob,
    filename:
      filenameFromContentDisposition(
        response.headers.get("content-disposition"),
      ) ?? "download",
  };
}
