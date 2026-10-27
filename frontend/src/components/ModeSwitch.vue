<script setup>
/**
 * 模式切换器（对话 / 助手）—— 顶部居中的胶囊分段控件。
 *
 * 为什么做成共享组件而不是各页抄一份：
 *   两页的 header 结构不同（chat 是 flex 左右两栏 + CSS 变量；agent 是硬编码色值），
 *   如果各写一份，样式必然漂移；而且"当前在哪一页"必须只有一处判断，否则以后加第三页就要改两处。
 *
 * 为什么用绝对定位居中，而不是塞进 flex：
 *   两页 header 都是 `justify-content: space-between` 的两栏结构，
 *   直接插入第三个 flex 子项会把左右两栏往中间挤，改变现有布局；
 *   绝对定位只叠加、不参与排版，视觉上"正好在正中间"，且窄屏时降级为静态块（见 CSS 媒体查询）。
 */
import { computed } from "vue"
import { useRoute, useRouter } from "vue-router"

const route = useRoute()
const router = useRouter()

// 用路由而非本地 state 判断当前模式：刷新页面、直接打开 /agent 都能正确高亮，
// 也不会出现"切换了但地址没变"的假状态。
const mode = computed(() => (route.path.startsWith("/agent") ? "agent" : "chat"))

function go(target) {
  if (target === mode.value) return
  router.push(target === "agent" ? "/agent" : "/chat")
}
</script>

<template>
  <div class="mode-switch" role="tablist" aria-label="模式切换">
    <button
      type="button"
      role="tab"
      class="mode-item"
      :class="{ active: mode === 'chat' }"
      :aria-selected="mode === 'chat'"
      title="单轮 / 多轮问答，带知识库来源"
      @click="go('chat')"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
      </svg>
      对话
    </button>
    <button
      type="button"
      role="tab"
      class="mode-item"
      :class="{ active: mode === 'agent' }"
      :aria-selected="mode === 'agent'"
      title="下任务：找文件 / 检索 / 入库，工具链可见"
      @click="go('agent')"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1" />
        <circle cx="12" cy="12" r="3.2" />
      </svg>
      助手
    </button>
  </div>
</template>

<style scoped>
/* 用 var(..., 回退值)：chat 页有一套 CSS 变量，agent 页是硬编码色值，
   回退值让同一份样式在两边都不违和（否则必然一边白得刺眼） */
.mode-switch {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 5;
  display: inline-flex;
  gap: 2px;
  padding: 3px;
  border-radius: 999px;
  background: var(--bg-input, #f0f2f5);
  border: 1px solid var(--border, #e2e4ec);
}

.mode-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 14px;
  border: none;
  border-radius: 999px;
  background: transparent;
  color: var(--text-secondary, #5a5a72);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.mode-item:hover:not(.active) {
  color: var(--text-primary, #1a1a2e);
}

.mode-item.active {
  background: var(--bg-secondary, #ffffff);
  color: var(--text-primary, #1a1a2e);
  font-weight: 600;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.14);
}

/* 窄屏：绝对居中会压住左右两侧按钮 ⇒ 降级为普通流式块，靠父级 flex 居中 */
@media (max-width: 860px) {
  .mode-switch {
    position: static;
    transform: none;
    margin: 0 auto;
  }
  .mode-item {
    padding: 5px 10px;
  }
}
</style>