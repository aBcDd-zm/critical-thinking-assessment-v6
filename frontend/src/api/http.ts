const rawBaseUrl = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8060/api/v1";
export const API_BASE_URL = rawBaseUrl.replace(/\/$/, "");
const ADMIN_CSRF_COOKIE = "cta_v6_admin_csrf";

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly body?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function errorMessage(body: unknown, fallback: string): string {
  if (typeof body === "string" && body.trim()) return body;
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    const detail = record.detail ?? record.message;
    if (typeof detail === "string") return detail;
  }
  return fallback;
}

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const prefix = `${encodeURIComponent(name)}=`;
  const part = document.cookie.split(";").map((item) => item.trim()).find((item) => item.startsWith(prefix));
  if (!part) return null;
  try {
    return decodeURIComponent(part.slice(prefix.length));
  } catch {
    return null;
  }
}

function isUnsafeMethod(method?: string): boolean {
  return !["GET", "HEAD", "OPTIONS"].includes((method || "GET").toUpperCase());
}

function isAuthenticatedAdminPath(path: string): boolean {
  return path.startsWith("/admin/") && path !== "/admin/auth/login";
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
  responseType: "json" | "blob" = "json",
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("Accept", responseType === "json" ? "application/json" : "application/pdf, application/zip");
  if (isAuthenticatedAdminPath(path) && isUnsafeMethod(init.method)) {
    const csrfToken = readCookie(ADMIN_CSRF_COOKIE);
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers, credentials: "include" });
  if (!response.ok) {
    const contentType = response.headers.get("content-type") ?? "";
    const body = contentType.includes("json") ? await response.json().catch(() => null) : await response.text();
    const error = new ApiError(errorMessage(body, `请求失败（${response.status}）`), response.status, body);
    if (response.status === 401 && isAuthenticatedAdminPath(path) && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("cta-v6:admin-auth-expired"));
    }
    throw error;
  }
  if (responseType === "blob") return (await response.blob()) as T;
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function absoluteApiUrl(path: string): string {
  if (/^https?:\/\//.test(path)) return path;
  const runtimeOrigin = typeof window === "undefined" ? "http://127.0.0.1" : window.location.origin;
  if (path.startsWith("/api/")) return new URL(path, runtimeOrigin).toString();
  const absoluteBase = new URL(API_BASE_URL, runtimeOrigin).toString().replace(/\/$/, "");
  return `${absoluteBase}${path.startsWith("/") ? path : `/${path}`}`;
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
