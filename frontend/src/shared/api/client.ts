import createClient, {
  type Client,
  type ClientPathsWithMethod,
  type MaybeOptionalInit,
  type MethodResponse,
} from "openapi-fetch";

import type { components, paths } from "./schema";

const API_PREFIX = "/api/v1";

export type ApiSchemas = components["schemas"];

export const AUTH_UNAUTHORIZED_EVENT = "athena:auth-unauthorized";
export const ADMIN_FORBIDDEN_EVENT = "athena:admin-forbidden";

export interface AdminForbiddenEventDetail {
  path: string;
}

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

export interface ApiErrorOptions extends ErrorOptions {
  field?: string;
  memberPath?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly field?: string;
  readonly memberPath?: string;

  constructor(
    status: number,
    detail: string,
    options: ApiErrorOptions = {},
  ) {
    super(`API ${status}: ${detail}`, options);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.field = options.field;
    this.memberPath = options.memberPath;
  }
}

export interface ParsedErrorDetail {
  code: string;
  field?: string;
  memberPath?: string;
}

type ApiMultipartOptions = Omit<RequestInit, "body">;

interface PerformedRequest {
  identityEpoch: number;
  response: Response;
}

/** 去除 /api/v1 前缀，确保 ADMIN_FORBIDDEN_EVENT 的 detail.path 与 AuthProvider 路径分级口径一致。 */
export function stripApiPrefix(rawPath: string): string {
  let path = rawPath;
  if (/^https?:\/\//i.test(path)) {
    try {
      const parsed = new URL(path);
      path = `${parsed.pathname}${parsed.search}${parsed.hash}`;
    } catch {
      // 非合法绝对 URL 时按原始路径继续归一化。
    }
  }
  if (path === API_PREFIX) {
    return "/";
  }
  if (
    path.startsWith(`${API_PREFIX}/`) ||
    path.startsWith(`${API_PREFIX}?`)
  ) {
    return path.slice(API_PREFIX.length);
  }
  return path;
}

export function publishUnauthorized(): void {
  if (typeof window !== "undefined") {
    // 所有 API 与 SSE 共用同一未认证事件，由 AuthProvider 统一清理受保护缓存。
    window.dispatchEvent(new CustomEvent(AUTH_UNAUTHORIZED_EVENT));
  }
}

export function publishAdminForbidden(path: string): void {
  if (typeof window !== "undefined") {
    // 附带去掉 /api/v1 前缀的触发路径，供 AuthProvider 区分超管专属端点、技能集合端点与资源级端点。
    window.dispatchEvent(
      new CustomEvent<AdminForbiddenEventDetail>(ADMIN_FORBIDDEN_EVENT, {
        detail: { path: stripApiPrefix(path) },
      }),
    );
  }
}

/** 从已解析的 JSON 错误体提取稳定错误码及可选字段/技能包成员路径。 */
export function parseErrorDetailPayload(payload: unknown): ParsedErrorDetail {
  if (typeof payload === "object" && payload !== null && "detail" in payload) {
    if (typeof payload.detail === "string") {
      return { code: payload.detail };
    }
    // 技能包路径错误与 Markdown 表单字段错误均采用结构化 detail；
    // 稳定错误码保留在 code，可选 field / member_path 独立透传，不污染权限判断。
    if (
      typeof payload.detail === "object" &&
      payload.detail !== null &&
      "code" in payload.detail &&
      typeof payload.detail.code === "string"
    ) {
      return {
        code: payload.detail.code,
        field:
          "field" in payload.detail && typeof payload.detail.field === "string"
            ? payload.detail.field
            : undefined,
        memberPath:
          "member_path" in payload.detail &&
          typeof payload.detail.member_path === "string"
            ? payload.detail.member_path
            : undefined,
      };
    }
  }
  return { code: "http_error" };
}

function isJsonContentType(response: Response): boolean {
  return (response.headers.get("content-type") ?? "")
    .toLowerCase()
    .includes("application/json");
}

function assertJsonContentType(response: Response): void {
  if (!isJsonContentType(response)) {
    throw new ApiError(response.status, "invalid_response");
  }
}

export async function readErrorDetail(
  response: Response,
): Promise<ParsedErrorDetail> {
  if (isJsonContentType(response)) {
    try {
      const payload: unknown = await response.json();
      return parseErrorDetailPayload(payload);
    } catch {
      // 服务端异常时可能声明 JSON 但返回截断内容，统一降级为稳定错误码。
    }
  }
  return { code: "http_error" };
}

function buildApiError(
  status: number,
  parsedDetail: ParsedErrorDetail,
  cause?: unknown,
): ApiError {
  const options: ApiErrorOptions = {};
  if (parsedDetail.field !== undefined) {
    options.field = parsedDetail.field;
  }
  if (parsedDetail.memberPath !== undefined) {
    options.memberPath = parsedDetail.memberPath;
  }
  if (cause !== undefined) {
    options.cause = cause;
  }
  return new ApiError(status, parsedDetail.code, options);
}

/**
 * 底层请求原语（供 OpenAPI、multipart 与 download 共用）：
 * 统一动态查找 globalThis.fetch、转换网络异常为 ApiError(0, "network_error")，
 * 并在响应头到达时执行二段 authIdentityEpoch 校验。
 */
async function fetchWithIdentityEpoch(
  url: string,
  init: RequestInit,
  identityEpoch: number,
): Promise<Response> {
  let response: Response;
  try {
    // 动态读取 globalThis.fetch，确保单测 vi.stubGlobal("fetch", ...) 实时生效。
    response = await globalThis.fetch(url, init);
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw error;
    }
    assertAuthIdentityCurrent(identityEpoch);
    throw new ApiError(0, "network_error", { cause: error });
  }
  // 二段身份校验：响应头刚到达时立刻复核，避免过期身份进入响应体读取。
  assertAuthIdentityCurrent(identityEpoch);
  return response;
}

/**
 * 统一错误响应终结原语（供 OpenAPI、multipart 与 download 共用）：
 * 执行三段 authIdentityEpoch 校验后再按状态码派发 401/403 全局事件并抛出结构化 ApiError，
 * 确保过期请求绝不向新登录身份误发权限事件。
 */
function throwApiResponseError(
  status: number,
  path: string,
  parsedDetail: ParsedErrorDetail,
  identityEpoch: number,
  cause?: unknown,
): never {
  assertAuthIdentityCurrent(identityEpoch);
  if (status === 401) {
    publishUnauthorized();
  }
  if (status === 403 && parsedDetail.code === "admin_required") {
    publishAdminForbidden(path);
  }
  throw buildApiError(status, parsedDetail, cause);
}

/**
 * 轻量请求载体：供 openapi-fetch 构造相对路径请求而不依赖 Node/jsdom 的绝对 URL Request 构造限制，
 * 同时让底层 fetch 收到 (url, init) 双参数调用，与浏览器原生 fetch 及 Vitest fetch 桩完全一致。
 */
class OpenApiFetchRequest {
  readonly url: string;
  readonly init: RequestInit;
  readonly method: string;
  readonly headers: Headers;
  readonly cache?: RequestCache;
  readonly credentials?: RequestCredentials;
  readonly signal?: AbortSignal | null;
  readonly body?: BodyInit | null;
  readonly redirect?: RequestRedirect;

  constructor(input: RequestInfo | URL, init: RequestInit = {}) {
    this.url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    this.init = init;
    this.method = (init.method ?? "GET").toUpperCase();
    this.headers = new Headers(init.headers);
    this.cache = init.cache;
    this.credentials = init.credentials;
    this.signal = init.signal;
    this.body = init.body;
    this.redirect = init.redirect;
  }
}

interface OpenApiRequestMeta {
  identityEpoch: number;
  requestUrl: string;
  response: Response | null;
}

function extractRequestParts(rawRequest: Request): {
  init: RequestInit;
  url: string;
} {
  if (rawRequest instanceof OpenApiFetchRequest) {
    return { init: rawRequest.init, url: rawRequest.url };
  }
  return {
    init: {
      body: rawRequest.body,
      cache: rawRequest.cache,
      credentials: rawRequest.credentials,
      headers: rawRequest.headers,
      method: rawRequest.method,
      redirect: rawRequest.redirect,
      signal: rawRequest.signal,
    },
    url: rawRequest.url,
  };
}

function createTrackedFetch(
  meta: OpenApiRequestMeta,
): (request: Request) => Promise<Response> {
  return async (rawRequest: Request): Promise<Response> => {
    const { init, url } = extractRequestParts(rawRequest);
    meta.requestUrl = url;
    const response = await fetchWithIdentityEpoch(url, init, meta.identityEpoch);
    meta.response = response;

    if (response.ok && response.status !== 204) {
      assertJsonContentType(response);
    }
    return response;
  };
}

// 仅限本模块内部经 apiGet/apiPost/apiPut/apiPatch/apiDelete 调用，
// 确保所有请求统一经过三段身份代次校验与 401/403 事件派发。
const openapiClient: Client<paths> = createClient<paths>({
  // 生成的 OpenAPI paths 已携带 /api/v1 前缀，此处必须置空防止重复拼接。
  baseUrl: "",
  credentials: "same-origin",
  headers: { Accept: "application/json" },
  Request: OpenApiFetchRequest as unknown as typeof Request,
  fetch: (rawRequest: Request) => {
    const { init, url } = extractRequestParts(rawRequest);
    return globalThis.fetch(url, init);
  },
});

type OpenApiClient = Client<paths>;
type OpenApiMethod = "get" | "post" | "put" | "patch" | "delete";
type OpenApiClientMethodKey = Uppercase<OpenApiMethod>;
type OpenApiPath<Method extends OpenApiMethod> = ClientPathsWithMethod<
  OpenApiClient,
  Method
>;
type OpenApiInit<
  Method extends OpenApiMethod,
  Path extends OpenApiPath<Method>,
> = MaybeOptionalInit<paths[Path], Method>;

type OpenApiInitArgs<
  Method extends OpenApiMethod,
  Path extends OpenApiPath<Method>,
> = undefined extends OpenApiInit<Method, Path>
  ? [init?: OpenApiInit<Method, Path>]
  : [init: OpenApiInit<Method, Path>];

type OpenApiSuccessData<
  Method extends OpenApiMethod,
  Path extends OpenApiPath<Method>,
> = [MethodResponse<OpenApiClient, Method, Path>] extends [never]
  ? void
  : MethodResponse<OpenApiClient, Method, Path>;

interface RawOpenApiResult {
  data?: unknown;
  error?: unknown;
  response: Response;
}

/**
 * 统一解包 openapi-fetch 响应，复用底层身份代次校验、401/403 全局事件派发与 ApiError 构造。
 */
async function executeOpenApiRequest<T>(
  invoke: (
    trackedFetch: (request: Request) => Promise<Response>,
  ) => Promise<RawOpenApiResult>,
): Promise<T> {
  // 一段身份校验：在发起请求前锁定当前身份代次。
  const meta: OpenApiRequestMeta = {
    identityEpoch: getAuthIdentityEpoch(),
    requestUrl: "",
    response: null,
  };
  const trackedFetch = createTrackedFetch(meta);

  let result: RawOpenApiResult;
  try {
    result = await invoke(trackedFetch);
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    if (error instanceof Error && error.name === "AbortError") {
      throw error;
    }
    assertAuthIdentityCurrent(meta.identityEpoch);
    if (meta.response) {
      if (!meta.response.ok) {
        throwApiResponseError(
          meta.response.status,
          meta.requestUrl,
          { code: "http_error" },
          meta.identityEpoch,
          error,
        );
      }
      throw new ApiError(meta.response.status, "invalid_response", {
        cause: error,
      });
    }
    throw new ApiError(0, "network_error", { cause: error });
  }

  const { data, error, response } = result;
  if (!response.ok) {
    const parsedDetail = isJsonContentType(response)
      ? parseErrorDetailPayload(error)
      : { code: "http_error" };
    throwApiResponseError(
      response.status,
      meta.requestUrl,
      parsedDetail,
      meta.identityEpoch,
    );
  }

  // 三段身份校验：JSON 响应体读取完成后复核，防止身份切换后把旧用户数据交给 Query/Mutation。
  assertAuthIdentityCurrent(meta.identityEpoch);

  if (response.status === 204) {
    return undefined as T;
  }

  if (data === undefined) {
    throw new ApiError(response.status, "invalid_response");
  }

  return data as T;
}

/** 泛型工厂：统一收敛 GET/POST/PUT/PATCH/DELETE 的 OpenAPI 类型推导与请求调度。 */
function createOpenApiInvoker<Method extends OpenApiMethod>(
  clientMethod: Uppercase<Method> & OpenApiClientMethodKey,
) {
  return <Path extends OpenApiPath<Method>>(
    path: Path,
    ...args: OpenApiInitArgs<Method, Path>
  ): Promise<OpenApiSuccessData<Method, Path>> => {
    const init = args[0] as Record<string, unknown> | undefined;
    const invokeMethod = openapiClient[clientMethod] as (
      url: Path,
      options?: Record<string, unknown>,
    ) => Promise<RawOpenApiResult>;
    return executeOpenApiRequest<OpenApiSuccessData<Method, Path>>(
      (trackedFetch) =>
        invokeMethod(path, { ...(init ?? {}), fetch: trackedFetch }),
    );
  };
}

export const apiGet = createOpenApiInvoker<"get">("GET");
export const apiPost = createOpenApiInvoker<"post">("POST");
export const apiPut = createOpenApiInvoker<"put">("PUT");
export const apiPatch = createOpenApiInvoker<"patch">("PATCH");
export const apiDelete = createOpenApiInvoker<"delete">("DELETE");

async function performRawRequest(
  path: string,
  requestInit: RequestInit,
): Promise<PerformedRequest> {
  const identityEpoch = getAuthIdentityEpoch();
  const headers = new Headers(requestInit.headers);
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }

  const response = await fetchWithIdentityEpoch(
    `${API_PREFIX}${path}`,
    {
      ...requestInit,
      credentials: "same-origin",
      headers,
    },
    identityEpoch,
  );

  if (!response.ok) {
    const parsedDetail = await readErrorDetail(response);
    throwApiResponseError(response.status, path, parsedDetail, identityEpoch);
  }
  return { identityEpoch, response };
}

export async function apiMultipart<T>(
  path: string,
  body: FormData,
  options: ApiMultipartOptions = {},
): Promise<T> {
  // 浏览器必须自行生成 multipart boundary，不能复用 JSON 请求的 Content-Type。
  const { identityEpoch, response } = await performRawRequest(path, {
    ...options,
    body,
  });

  if (response.status === 204) {
    return undefined as T;
  }
  assertJsonContentType(response);

  let payload: T;
  try {
    payload = (await response.json()) as T;
  } catch (error) {
    assertAuthIdentityCurrent(identityEpoch);
    throw new ApiError(response.status, "invalid_response", { cause: error });
  }
  // 三段身份校验：响应体读取完成后再次确认身份未切换。
  assertAuthIdentityCurrent(identityEpoch);
  return payload;
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
  const { identityEpoch, response } = await performRawRequest(path, {
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
