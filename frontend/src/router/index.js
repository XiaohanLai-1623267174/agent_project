import { createRouter, createWebHistory } from "vue-router"

const routes = [
  {
    path: "/login",
    name: "Login",
    component: () => import("../views/Login.vue"),
  },
  {
    path: "/chat",
    name: "Chat",
    component: () => import("../views/ChatView.vue"),
    meta: { requiresAuth: true },
  },
  {
    path: "/agent",
    name: "Agent",
    component: () => import("../views/AgentView.vue"),
    // 与 /chat 一样要求登录：/agent/stream 也走 Depends(get_current_user)
    meta: { requiresAuth: true },
  },
  {
    path: "/:pathMatch(.*)*",
    redirect: "/chat",
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to, from, next) => {
  const token = localStorage.getItem("auth_token")
  if (to.meta.requiresAuth && !token) {
    next("/login")
  } else if (to.path === "/login" && token) {
    next("/chat")
  } else {
    next()
  }
})

export default router
