<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-header">
        <div class="login-icon">
          <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            <path d="M8 9h8" opacity="0.5"/><path d="M8 13h5" opacity="0.3"/>
          </svg>
        </div>
        <h2>AI 知识库助手</h2>
        <p class="login-sub">登录以继续使用</p>
      </div>

      <!-- Tab switch -->
      <div class="tab-row">
        <button
          :class="['tab', { active: mode === 'login' }]"
          @click="mode = 'login'"
        >登录</button>
        <button
          :class="['tab', { active: mode === 'register' }]"
          @click="mode = 'register'"
        >注册</button>
      </div>

      <form class="login-form" @submit.prevent="handleSubmit">
        <div class="field">
          <label>用户名</label>
          <input
            v-model="username"
            type="text"
            placeholder="请输入用户名"
            autocomplete="username"
            required
          />
        </div>
        <div class="field">
          <label>密码</label>
          <input
            v-model="password"
            type="password"
            placeholder="请输入密码"
            autocomplete="current-password"
            required
          />
        </div>

        <p v-if="errorMsg" class="error">{{ errorMsg }}</p>

        <button class="btn-submit" type="submit" :disabled="loading">
          {{ loading ? "处理中..." : mode === "login" ? "登录" : "注册" }}
        </button>
      </form>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '../stores/auth.js'

const router = useRouter()
const { login, register } = useAuth()

const mode = ref('login')
const username = ref('')
const password = ref('')
const loading = ref(false)
const errorMsg = ref('')

async function handleSubmit() {
  errorMsg.value = ''
  loading.value = true
  try {
    if (mode.value === 'login') {
      await login(username.value, password.value)
    } else {
      await register(username.value, password.value)
    }
    router.push('/chat')
  } catch (e) {
    errorMsg.value = e.message || '操作失败，请重试'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: #f0f2f5;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", sans-serif;
}

.login-card {
  width: 380px;
  max-width: 90vw;
  background: #fff;
  border-radius: 16px;
  padding: 40px 32px 32px;
  box-shadow: 0 2px 16px rgba(0, 0, 0, 0.08);
}

.login-header {
  text-align: center;
  margin-bottom: 28px;
}

.login-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 64px;
  height: 64px;
  border-radius: 16px;
  background: #eef2ff;
  color: #4d6bfe;
  margin-bottom: 16px;
}

.login-header h2 {
  font-size: 20px;
  font-weight: 600;
  color: #1a1a2e;
  margin: 0 0 4px;
}

.login-sub {
  font-size: 13px;
  color: #8c8c9e;
  margin: 0;
}

/* ---- Tabs ---- */
.tab-row {
  display: flex;
  border: 1px solid #e0e0e8;
  border-radius: 10px;
  overflow: hidden;
  margin-bottom: 24px;
}

.tab {
  flex: 1;
  padding: 8px 0;
  border: none;
  background: #fff;
  color: #8c8c9e;
  font-size: 14px;
  cursor: pointer;
  transition: all 0.15s;
}

.tab.active {
  background: #4d6bfe;
  color: #fff;
  font-weight: 500;
}

/* ---- Form ---- */
.field {
  margin-bottom: 16px;
}

.field label {
  display: block;
  font-size: 13px;
  font-weight: 500;
  color: #3a3a4e;
  margin-bottom: 6px;
}

.field input {
  width: 100%;
  height: 42px;
  border: 1px solid #dcdce4;
  border-radius: 10px;
  padding: 0 14px;
  font-size: 14px;
  font-family: inherit;
  color: #1a1a2e;
  outline: none;
  transition: border-color 0.15s;
  box-sizing: border-box;
}

.field input:focus {
  border-color: #4d6bfe;
  box-shadow: 0 0 0 3px rgba(77, 107, 254, 0.1);
}

.field input::placeholder {
  color: #bbb;
}

/* ---- Error ---- */
.error {
  color: #e53e3e;
  font-size: 13px;
  margin: -4px 0 12px;
}

/* ---- Button ---- */
.btn-submit {
  width: 100%;
  height: 44px;
  border: none;
  border-radius: 10px;
  background: #4d6bfe;
  color: #fff;
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s;
}

.btn-submit:hover:not(:disabled) {
  background: #3d5bef;
}

.btn-submit:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
