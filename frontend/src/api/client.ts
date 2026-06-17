import { useAuthStore } from '../stores/authStore'

const API_BASE = '/api/v1'

interface ApiError {
  code: number
  message: string
  detail?: string
  data: null
}

class ApiRequestError extends Error {
  code: number
  detail?: string

  constructor(err: ApiError, status: number) {
    super(err.message)
    this.name = 'ApiRequestError'
    this.code = err.code || status
    this.detail = err.detail
  }
}

function getHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  }
  const apiKey = useAuthStore.getState().apiKey
  if (apiKey) {
    headers['X-API-Key'] = apiKey
  }
  return headers
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  options?: { rawResponse?: boolean }
): Promise<T> {
  const url = `${API_BASE}${path}`
  const fetchOptions: RequestInit = {
    method,
    headers: getHeaders(),
  }

  if (body !== undefined && method !== 'GET') {
    fetchOptions.body = JSON.stringify(body)
  }

  const res = await fetch(url, fetchOptions)

  // Handle auth errors
  if (res.status === 401) {
    useAuthStore.getState().clearApiKey()
    throw new ApiRequestError(
      { code: 40101, message: 'Authentication failed', detail: 'Invalid or missing API key', data: null },
      401
    )
  }

  if (!res.ok) {
    let errBody: ApiError = { code: res.status, message: res.statusText, data: null }
    try {
      errBody = await res.json()
    } catch {
      // Use default error
    }
    throw new ApiRequestError(errBody, res.status)
  }

  if (options?.rawResponse) {
    return res as unknown as T
  }

  return res.json()
}

export const api = {
  get: <T>(path: string, params?: Record<string, string>) => {
    const searchParams = params ? '?' + new URLSearchParams(params).toString() : ''
    return request<T>('GET', path + searchParams)
  },
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body),
  delete: <T>(path: string) => request<T>('DELETE', path),
  getRaw: (path: string, body?: unknown) =>
    request<Response>('POST', path, body, { rawResponse: true }),
}

export { ApiRequestError }
