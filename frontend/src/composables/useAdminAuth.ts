import { computed, ref } from "vue";
import { getCurrentAdmin, loginAdmin, logoutAdmin } from "@/api/auth";
import type { AdminUser } from "@/types/contracts";

const user = ref<AdminUser | null>(null);
const resolved = ref(false);
let inFlight: Promise<boolean> | null = null;

export function useAdminAuth() {
  const isLoggedIn = computed(() => user.value !== null);

  async function restore(): Promise<boolean> {
    if (resolved.value) return user.value !== null;
    if (inFlight) return inFlight;
    inFlight = getCurrentAdmin()
      .then((current) => {
        user.value = current;
        return true;
      })
      .catch(() => {
        user.value = null;
        return false;
      })
      .finally(() => {
        resolved.value = true;
        inFlight = null;
      });
    return inFlight;
  }

  async function login(username: string, password: string): Promise<AdminUser> {
    const result = await loginAdmin(username, password);
    user.value = result.user;
    resolved.value = true;
    return result.user;
  }

  async function logout(): Promise<void> {
    try {
      await logoutAdmin();
    } finally {
      user.value = null;
      resolved.value = true;
    }
  }

  function clear() {
    user.value = null;
    resolved.value = true;
  }

  return { user, isLoggedIn, restore, login, logout, clear };
}
