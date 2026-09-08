import { createRouter, createWebHistory } from "vue-router";
import ConsentView from "@/views/ConsentView.vue";
import InterviewView from "@/views/InterviewView.vue";
import ReportView from "@/views/ReportView.vue";
import AdminLayout from "@/views/AdminLayout.vue";
import AdminLoginView from "@/views/AdminLoginView.vue";
import AdminDashboardView from "@/views/AdminDashboardView.vue";
import AdminSessionsView from "@/views/AdminSessionsView.vue";
import AdminSessionDetailView from "@/views/AdminSessionDetailView.vue";
import { useAdminAuth } from "@/composables/useAdminAuth";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/assessment" },
    { path: "/assessment", component: ConsentView, meta: { title: "开始测评" } },
    { path: "/assessment/session/:sessionUuid", component: InterviewView, meta: { title: "AI 访谈" } },
    { path: "/assessment/report/:sessionUuid", component: ReportView, meta: { title: "测评报告" } },
    { path: "/admin/login", name: "admin-login", component: AdminLoginView, meta: { title: "管理员登录", publicAdmin: true } },
    {
      path: "/admin",
      component: AdminLayout,
      meta: { requiresAdmin: true },
      children: [
        { path: "", redirect: { name: "admin-dashboard" } },
        { path: "dashboard", name: "admin-dashboard", component: AdminDashboardView, meta: { title: "复核概览" } },
        { path: "sessions", name: "admin-sessions", component: AdminSessionsView, meta: { title: "会话复核" } },
        { path: "sessions/:sessionUuid", name: "admin-session-detail", component: AdminSessionDetailView, meta: { title: "会话复核" } },
      ],
    },
    { path: "/:pathMatch(.*)*", redirect: "/assessment" },
  ],
  scrollBehavior: () => ({ top: 0 }),
});

router.beforeEach(async (to) => {
  const auth = useAdminAuth();
  const requiresAdmin = to.matched.some((record) => record.meta.requiresAdmin);
  if (requiresAdmin && !(await auth.restore())) {
    return { name: "admin-login", query: { redirect: to.fullPath } };
  }
  if (to.name === "admin-login" && (await auth.restore())) {
    const redirect = typeof to.query.redirect === "string" && to.query.redirect.startsWith("/admin")
      ? to.query.redirect
      : "/admin/dashboard";
    return redirect;
  }
  return true;
});

router.afterEach((to) => {
  document.title = `${String(to.meta.title ?? "思衡")} · 思衡`;
});

if (typeof window !== "undefined") {
  window.addEventListener("cta-v6:admin-auth-expired", () => {
    const auth = useAdminAuth();
    auth.clear();
    if (router.currentRoute.value.path.startsWith("/admin") && router.currentRoute.value.name !== "admin-login") {
      void router.replace({ name: "admin-login", query: { redirect: router.currentRoute.value.fullPath } });
    }
  });
}

export default router;
