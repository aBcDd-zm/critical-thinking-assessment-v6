<script setup lang="ts">
import { computed } from "vue";
import { RouterLink, RouterView, useRouter } from "vue-router";
import { useAdminAuth } from "@/composables/useAdminAuth";

const router = useRouter();
const auth = useAdminAuth();
const adminName = computed(() => auth.user.value?.display_name || auth.user.value?.username || "管理员");

async function logout() {
  await auth.logout();
  await router.replace({ name: "admin-login" });
}
</script>

<template>
  <div class="admin-console">
    <aside class="console-sidebar" aria-label="后台导航">
      <RouterLink class="console-brand" to="/admin/dashboard">
        <span>思衡</span>
        <small>复核台</small>
      </RouterLink>
      <nav class="console-nav">
        <RouterLink to="/admin/dashboard"><b>01</b><span>数据概览</span></RouterLink>
        <RouterLink to="/admin/sessions"><b>02</b><span>会话复核</span></RouterLink>
      </nav>
      <div class="console-sidebar-footer">
        <RouterLink class="console-user-link" to="/assessment">打开用户端 ↗</RouterLink>
        <div class="console-account"><span>{{ adminName }}</span><button type="button" @click="logout">退出登录</button></div>
      </div>
    </aside>
    <div class="console-workspace">
      <header class="console-topbar">
        <div><span class="eyebrow">自然访谈与证据复核</span><h1>复核运营后台</h1></div>
        <div class="console-account topbar-account"><span class="status-dot" />{{ adminName }}<button type="button" @click="logout">退出</button></div>
      </header>
      <RouterView />
    </div>
  </div>
</template>
