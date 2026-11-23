export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown) {
    super(`API request failed with status ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

let accessToken: string | null = null;
let refreshInFlight: Promise<string | null> | null = null;
const REFRESH_REQUEST_ID_KEY = 'factoryos.refresh.request.v1';
const REFRESH_DUPLICATE_WINDOW_MS = 30_000;
let lastCrossTabAccessToken: { token: string; expiresAt: number } | null = null;
const authChannel = typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel('factoryos-auth');
const pendingAccessTokenRequests = new Map<string, (value: { token: string; expiresAt: number } | null) => void>();

authChannel?.addEventListener('message', (event: MessageEvent) => {
  const data = event.data as { type?: string; token?: string; expiresAt?: number; requestId?: string };
  if (data?.type === 'access-token' && typeof data.token === 'string' && typeof data.expiresAt === 'number') {
    lastCrossTabAccessToken = { token: data.token, expiresAt: data.expiresAt };
    accessToken = data.token;
  } else if (data?.type === 'access-token-request' && typeof data.requestId === 'string'
      && lastCrossTabAccessToken && lastCrossTabAccessToken.expiresAt > Date.now() + 5_000) {
    authChannel?.postMessage({
      type: 'access-token-response', requestId: data.requestId,
      token: lastCrossTabAccessToken.token, expiresAt: lastCrossTabAccessToken.expiresAt,
    });
  } else if (data?.type === 'access-token-response' && typeof data.requestId === 'string'
      && typeof data.token === 'string' && typeof data.expiresAt === 'number') {
    const resolve = pendingAccessTokenRequests.get(data.requestId);
    if (resolve) {
      const value = data.expiresAt > Date.now() + 5_000
        ? { token: data.token, expiresAt: data.expiresAt }
        : null;
      resolve(value);
      pendingAccessTokenRequests.delete(data.requestId);
    }
  } else if (data?.type === 'signed-out') {
    lastCrossTabAccessToken = null;
    accessToken = null;
  }
});

function createRequestUuid(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function refreshRequestId(): string {
  const now = Date.now();
  try {
    const stored = localStorage.getItem(REFRESH_REQUEST_ID_KEY);
    if (stored) {
      const value = JSON.parse(stored) as { id?: string; createdAt?: number };
      if (value.id && typeof value.createdAt === 'number'
          && now >= value.createdAt && now - value.createdAt < REFRESH_DUPLICATE_WINDOW_MS) {
        return value.id;
      }
    }
    const id = createRequestUuid();
    localStorage.setItem(REFRESH_REQUEST_ID_KEY, JSON.stringify({ id, createdAt: now }));
    return id;
  } catch {
    return createRequestUuid();
  }
}

function clearRefreshRequestId(): void {
  try { localStorage.removeItem(REFRESH_REQUEST_ID_KEY); } catch { /* private browsing may block storage */ }
}

function publishAccessToken(token: string, expiresIn: number): void {
  const expiresAt = Date.now() + expiresIn * 1000;
  lastCrossTabAccessToken = { token, expiresAt };
  authChannel?.postMessage({ type: 'access-token', token, expiresAt });
}

function requestAccessTokenFromAnotherTab(): Promise<{ token: string; expiresAt: number } | null> {
  if (!authChannel) return Promise.resolve(null);
  const requestId = createRequestUuid();
  return new Promise((resolve) => {
    const timeout = window.setTimeout(() => {
      pendingAccessTokenRequests.delete(requestId);
      resolve(null);
    }, 100);
    pendingAccessTokenRequests.set(requestId, (value) => {
      window.clearTimeout(timeout);
      resolve(value);
    });
    authChannel.postMessage({ type: 'access-token-request', requestId });
  });
}

export function setAccessToken(token: string | null) {
  accessToken = token;
}

async function parseResponse<T>(response: Response): Promise<T> {
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload as T;
}

async function refreshAccessToken(): Promise<string | null> {
  if (!refreshInFlight) {
    const performRefresh = async (): Promise<string | null> => {
      if (lastCrossTabAccessToken && lastCrossTabAccessToken.expiresAt > Date.now() + 5_000) {
        accessToken = lastCrossTabAccessToken.token;
        return accessToken;
      }
      const requestId = refreshRequestId();
      try {
        const response = await fetch('/api/auth/refresh', {
          method: 'POST', credentials: 'include', headers: { 'X-Refresh-Request-ID': requestId },
        });
        const data = await parseResponse<{ access_token: string; expires_in: number }>(response);
        accessToken = data.access_token;
        publishAccessToken(data.access_token, data.expires_in);
        return accessToken;
      } catch (error) {
        accessToken = null;
        lastCrossTabAccessToken = null;
        if (!(error instanceof ApiError) || error.status < 500) {
          clearRefreshRequestId();
          window.dispatchEvent(new CustomEvent('factoryos:unauthorized'));
        }
        return null;
      }
    };
    const runUnderCrossTabLock = async (): Promise<string | null> => {
      if (navigator.locks) {
        return navigator.locks.request('factoryos-auth-refresh', { mode: 'exclusive' }, async () => {
          if (!lastCrossTabAccessToken || lastCrossTabAccessToken.expiresAt <= Date.now() + 5_000) {
            const sharedToken = await requestAccessTokenFromAnotherTab();
            if (sharedToken) {
              lastCrossTabAccessToken = sharedToken;
              accessToken = sharedToken.token;
            }
          }
          return performRefresh();
        });
      }
      return performRefresh();
    };
    refreshInFlight = runUnderCrossTabLock().finally(() => { refreshInFlight = null; });
  }
  return refreshInFlight;
}

async function request<T>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const headers = new Headers(init.headers);
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`);
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const response = await fetch(`/api${normalizedPath}`, { ...init, headers, credentials: 'include' });
  if (response.status === 401 && retry && !normalizedPath.startsWith('/auth/')) {
    const token = await refreshAccessToken();
    if (token) return request<T>(path, init, false);
  }
  return parseResponse<T>(response);
}

export function apiGet<T>(path: string): Promise<T> {
  return request<T>(path);
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });
}

export function apiPut<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: 'PUT', body: JSON.stringify(body) });
}

export function apiPatch<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: 'PATCH', body: JSON.stringify(body) });
}

export function apiDelete<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'DELETE' });
}

export async function loginRequest(email: string, password: string, company_id?: string) {
  const response = await fetch('/api/auth/login', {
    method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password, ...(company_id ? { company_id } : {}) }),
  });
  const data = await parseResponse<{ access_token: string; expires_in: number }>(response);
  clearRefreshRequestId();
  lastCrossTabAccessToken = null;
  accessToken = data.access_token;
  publishAccessToken(data.access_token, data.expires_in);
  return data;
}

export async function restoreAccessToken(): Promise<boolean> {
  return Boolean(await refreshAccessToken());
}

export async function logoutRequest() {
  try {
    await request('/auth/logout', { method: 'POST' }, false);
  } finally {
    accessToken = null;
    lastCrossTabAccessToken = null;
    clearRefreshRequestId();
    authChannel?.postMessage({ type: 'signed-out' });
  }
}
