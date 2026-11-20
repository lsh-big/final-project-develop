const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '');
export const publicDemo = import.meta.env.VITE_PUBLIC_DEMO === '1';
const demoWorkPath = '/api/v1/ai-mail/demo-work';
const demoSessionKey = `autoflow:demo-mail:${API_BASE}`;
const remoteApi = API_BASE && new URL(API_BASE, window.location.href).origin !== window.location.origin;

export const apiUrl = (path) => `${API_BASE}${path}`;

export function apiFetch(path, options) {
  if (!remoteApi || (path !== demoWorkPath && !path.startsWith(`${demoWorkPath}/`))) {
    return fetch(apiUrl(path), options);
  }
  const headers = new Headers(options?.headers);
  const token = sessionStorage.getItem(demoSessionKey);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  return fetch(apiUrl(path), { ...options, headers, credentials: 'omit' });
}

async function checkedResponse(path, options) {
  const response = await apiFetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    const message = typeof detail === 'string' ? detail : detail?.message;
    throw Object.assign(new Error(message || `API 요청 실패 (HTTP ${response.status})`), {
      recordId: detail?.id,
      code: detail?.code,
    });
  }
  return response;
}

export async function request(path, options = {}) {
  const response = await checkedResponse(path, options);
  if (response.status === 204) return null;
  const data = await response.json();
  if (remoteApi && path === demoWorkPath && data.demo_session_token && !options?.signal?.aborted) {
    sessionStorage.setItem(demoSessionKey, data.demo_session_token);
    delete data.demo_session_token;
  }
  return data;
}

export async function downloadWorkbook(path) {
  const response = await checkedResponse(path);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = 'purchase-history.xlsx';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
