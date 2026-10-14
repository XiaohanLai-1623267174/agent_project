/**
 * Auth state store (simple reactive singleton, no Pinia needed).
 */
import { reactive, computed } from "vue"
import { apiPost } from "../api/request.js"

const state = reactive({
  token: localStorage.getItem("auth_token") || "",
  username: localStorage.getItem("auth_username") || "",
})

export function useAuth() {
  const isAuthenticated = computed(() => !!state.token)

  async function login(username, password) {
    const data = await apiPost("/auth/login", { username, password })
    state.token = data.token
    state.username = data.username
    localStorage.setItem("auth_token", data.token)
    localStorage.setItem("auth_username", data.username)
  }

  async function register(username, password) {
    const data = await apiPost("/auth/register", { username, password })
    state.token = data.token
    state.username = data.username
    localStorage.setItem("auth_token", data.token)
    localStorage.setItem("auth_username", data.username)
  }

  function logout() {
    state.token = ""
    state.username = ""
    localStorage.removeItem("auth_token")
    localStorage.removeItem("auth_username")
  }

  return { state, isAuthenticated, login, register, logout }
}
