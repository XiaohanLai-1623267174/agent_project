import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    watch: {
      // ★ P1-1 期间实测到的坑（2026-10-12）：某些编辑器/AI 工具写文件用的是
      //   "**原子写**" —— 先在**源文件同目录**建一个临时目录+临时文件，再 rename。
      //   Vite 的 watcher 会去 watch 那个瞬时存在的临时文件，Windows 上拿到 EBUSY，
      //   而这是**未捕获的 'error' 事件** ⇒ **整个 dev server 进程退出**（不是 HMR 失败）。
      //   实测报错：EBUSY: resource busy or locked, watch
      //   '...\src\views\.ChatView.vue.25364.3b3cc25f-....tmpdir\ChatView.vue.tmp'
      //   ⇒ 把这类瞬时文件排除掉：dev server 不再因"别人写文件的方式"而崩。
      ignored: ['**/.*.tmpdir/**', '**/*.tmp', '**/~$*'],
    },
  },
})