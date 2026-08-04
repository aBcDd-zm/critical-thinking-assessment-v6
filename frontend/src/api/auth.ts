import { ApiError, apiRequest } from "./http";
import type { AdminUser } from "@/types/contracts";

export interface LoginResponse {
  user: AdminUser;
}

export const loginAdmin = (username: string, password: string) =>
  apiRequest<LoginResponse>("/admin/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });

export const getCurrentAdmin = async (): Promise<AdminUser> => {
  const result = await apiRequest<LoginResponse>("/admin/auth/me");
  return result.user;
};

export async function logoutAdmin(): Promise<void> {
  try {
    await apiRequest<void>("/admin/auth/logout", { method: "POST" });
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 401) throw error;
  }
}
