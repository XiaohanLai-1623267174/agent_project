# 前端（Vue 3 + Vite）

聊天界面：登录、会话列表、SSE 流式回答、引用跳转，以及两个检索开关。

## 运行

```bash
npm install
npm run dev      # 用 http://localhost:5173/ 访问（Vite 监听 IPv6 [::1]，127.0.0.1 连不上）
```

后端地址默认 `http://127.0.0.1:8000`；复制 `.env.example` 为 `.env.local` 可改 `VITE_API_BASE`。

## 两个开关

| 开关 | 作用 | 默认 |
|---|---|---|
| 知识库搜索 | 强制走知识库检索 | 开 |
| 联网搜索 | 额外检索公开网页，结果作为参考块进入回答 | 关（偏好存在 `localStorage` 的 `kb_web_search`） |

## 验收装置（零依赖、零 LLM）

本项目的前端改动用一组自带断言的小装置验收，**请在 `frontend/` 目录下运行**（它们按相对路径读取 `src/views/ChatView.vue`）：

```bash
node _autoscroll_test.mjs        # 断言装置
node _autoscroll_mutate.mjs      # 反向对照：故意改坏源码，验证上面的断言确实会红
```

- `_*_test.mjs`：断言装置，全部通过时退出码为 0。
- `_*_mutate.mjs`：反向对照。它把源码里的目标字符串改成等价的坏实现，然后检查**指定的那条断言**是否真的失败。退出码 0 的含义是「所有改动都真的变红，且都被点名的断言抓住」——这一步用来排除「断言写了但根本没在检查」。
- `_cdp_page_probe.mjs`：真浏览器验收（需要外部先以 `--remote-debugging-port` 启动 Chrome）。它走 CDP 检查页面真实渲染出的按钮状态、颜色、`localStorage` 持久化与运行时异常，全程 0 次 LLM 调用。

## 注意：有 4 个装置只在原始开发目录里能跑

下面这 4 个装置会去读 `../kb_bench/`（原始仓库布局里服务端代码所在的位置），因此在单独克隆本仓库后无法运行 —— 它们检查的是服务端源码里的具体实现，属于开发期工具：

```
_cite_align_test.mjs      _cite_test.mjs
_debug_panel_test.mjs     _gate_mutate.mjs
```

其余装置（`_autoscroll_*`、`_cite_align_mutate`、`_kbsearch_*`、`_noretr_render_test`、`_rt_mutate`、`_websearch_test`、`_cdp_page_probe`）只依赖 `src/views/ChatView.vue`，在克隆里可直接运行。