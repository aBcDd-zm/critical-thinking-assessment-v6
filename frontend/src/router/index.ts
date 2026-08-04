import { createRouter, createWebHistory } from "vue-router";
import ConsentView from "@/views/ConsentView.vue";
import InterviewView from "@/views/InterviewView.vue";
import ReportView from "@/views/ReportView.vue";
import AdminSessionsView from "@/views/AdminSessionsView.vue";
import AdminSessionDetailView from "@/views/AdminSessionDetailView.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/assessment" },
    { path: "/assessment", component: ConsentView, meta: { title: "开始测评" } },
    { path: "/assessment/session/:sessionUuid", component: InterviewView, meta: { title: "AI 访谈" } },
    { path: "/assessment/report/:sessionUuid", component: ReportView, meta: { title: "测评报告" } },
    { path: "/admin", component: AdminSessionsView, meta: { title: "复核工作台" } },
    { path: "/admin/sessions/:sessionUuid", component: AdminSessionDetailView, meta: { title: "会话复核" } },
    { path: "/:pathMatch(.*)*", redirect: "/assessment" },
  ],
  scrollBehavior: () => ({ top: 0 }),
});

router.afterEach((to) => {
  document.title = `${String(to.meta.title ?? "思衡 V6")} · 思衡 V6`;
});

export default router;
