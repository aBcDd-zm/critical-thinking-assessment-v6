import { ApiError, apiRequest } from "./http";
import type {
  AdminSessionDetail,
  DashboardOverview,
  ExpertScore,
  FinalizeResponse,
  PagedSessions,
  ReviewStatus,
} from "@/types/contracts";

export interface SessionFilters {
  phase?: string;
  review_status?: string;
  manual_review_recommended?: string;
  q?: string;
}

export const getDashboardOverview = () => apiRequest<DashboardOverview>("/admin/dashboard/overview");

export async function listAdminSessions(filters: SessionFilters): Promise<PagedSessions> {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => value && params.set(key, value));
  const result = await apiRequest<PagedSessions | AdminSessionDetail[]>(`/admin/sessions?${params}`);
  return Array.isArray(result) ? { items: result, total: result.length } : result;
}

export const getAdminSession = (uuid: string) =>
  apiRequest<AdminSessionDetail>(`/admin/sessions/${encodeURIComponent(uuid)}`);

export const finalizeAdminSession = (uuid: string) =>
  apiRequest<FinalizeResponse>(`/admin/sessions/${encodeURIComponent(uuid)}/finalize`, {
    method: "POST",
  });

export async function updateReview(
  uuid: string,
  payload: { review_status: ReviewStatus; review_notes: string; reviewer: string },
): Promise<Record<string, unknown>> {
  const path = `/admin/sessions/${encodeURIComponent(uuid)}/review`;
  const wirePayload = {
    ...payload,
    status: payload.review_status,
    notes: payload.review_notes,
  };
  try {
    return await apiRequest<Record<string, unknown>>(path, { method: "PUT", body: JSON.stringify(wirePayload) });
  } catch (cause) {
    if (!(cause instanceof ApiError) || ![404, 405].includes(cause.status)) throw cause;
    return apiRequest<Record<string, unknown>>(path, { method: "PATCH", body: JSON.stringify(wirePayload) });
  }
}

export async function saveExpertScores(
  uuid: string,
  payload: { scores: ExpertScore[]; reviewer: string },
): Promise<Record<string, unknown>> {
  const path = `/admin/sessions/${encodeURIComponent(uuid)}/expert-scores`;
  const wirePayload = {
    ...payload,
    scores: payload.scores.filter((item): item is ExpertScore & { score: number } => item.score !== null),
  };
  try {
    return await apiRequest<Record<string, unknown>>(path, { method: "PUT", body: JSON.stringify(wirePayload) });
  } catch (cause) {
    if (!(cause instanceof ApiError) || ![404, 405].includes(cause.status)) throw cause;
    return apiRequest<Record<string, unknown>>(path, { method: "POST", body: JSON.stringify(wirePayload) });
  }
}

export const importExpertScores = (file: File) => {
  const body = new FormData();
  body.append("file", file);
  return apiRequest<{ imported: number; errors?: string[] }>("/admin/expert-scores:import", {
    method: "POST",
    body,
  });
};

export async function getAnonymousExport(): Promise<Blob> {
  try {
    return await apiRequest<Blob>("/admin/exports/anonymous", {}, "blob");
  } catch (cause) {
    if (!(cause instanceof ApiError) || cause.status !== 404) throw cause;
    return apiRequest<Blob>("/admin/exports/anonymous.zip", {}, "blob");
  }
}
