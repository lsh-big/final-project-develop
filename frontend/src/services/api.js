const API_BASE = import.meta.env.VITE_API_BASE_URL || '';

export const apiUrl = (path) => `${API_BASE}${path}`;

async function checkedResponse(path, options) {
  const response = await fetch(apiUrl(path), options);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    const message = typeof detail === 'string' ? detail : detail?.message;
    throw Object.assign(new Error(message || `API 요청 실패 (HTTP ${response.status})`), {
      recordId: detail?.id,
    });
  }
  return response;
}

export async function request(path, options = {}) {
  const response = await checkedResponse(path, options);
  return response.status === 204 ? null : response.json();
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
