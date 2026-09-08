<script setup lang="ts">
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useAdminAuth } from "@/composables/useAdminAuth";

const username = ref("");
const password = ref("");
const error = ref("");
const loading = ref(false);
const route = useRoute();
const router = useRouter();
const auth = useAdminAuth();

async function submit() {
  error.value = "";
  loading.value = true;
  try {
    await auth.login(username.value.trim(), password.value);
    const redirect = typeof route.query.redirect === "string" && route.query.redirect.startsWith("/admin")
      ? route.query.redirect
      : "/admin/dashboard";
    await router.replace(redirect);
  } catch {
    error.value = "账号或密码不正确，请检查后重试。";
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <main class="admin-login-page">
    <section class="admin-login-card" aria-labelledby="admin-login-title">
      <RouterLink class="console-brand login-brand" to="/assessment"><span>思衡</span><small>复核台</small></RouterLink>
      <div><span class="eyebrow">ADMINISTRATOR ACCESS</span><h1 id="admin-login-title">进入复核运营后台</h1><p>仅供获授权人员查看会话、证据和研究复核记录。</p></div>
      <form @submit.prevent="submit">
        <label><span>管理员账号</span><input v-model="username" autocomplete="username" required :disabled="loading" /></label>
        <label><span>密码</span><input v-model="password" type="password" autocomplete="current-password" required :disabled="loading" /></label>
        <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
        <button class="primary-button" type="submit" :disabled="loading">{{ loading ? "正在验证…" : "登录后台" }}</button>
      </form>
      <p class="login-boundary">本后台不提供受测者账号、评分排名或公开数据浏览。</p>
    </section>
  </main>
</template>
