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

## 这 4 个装置要读服务端源码 —— 克隆后同样能跑

```
_cite_align_test.mjs      _cite_test.mjs
_debug_panel_test.mjs     _gate_mutate.mjs
```

它们查的是**服务端里的具体实现**（状态帧怎么发、开关字段有没有一路透传下去），所以必须读服务端源文件。
路径由 `_device_paths.mjs` 统一解析：先找 `../kb_bench/`（本项目的原始开发布局，服务端在 `kb_bench\`），
找不到就退到 `../server/`（本仓库布局）。因此**克隆本仓库后这 4 个可以直接跑**，不需要任何环境变量；
要指向别处也可以用 `DEVICE_KB_DIR` / `DEVICE_SERVER_DIR` 覆盖。

## 抓包夹具：随仓库发布的是【合成】夹具

`_cite_align_test.mjs` / `_cite_test.mjs` 还需要一份 SSE 抓包：把真实响应体喂给真解析器，
再与后端权威解析逐字对齐。**真实抓包含用户内容**（提问、回答、用户所在学校名与上传的文档名），
因此不随仓库发布；仓库里给的是 `_fixtures/` 下的合成夹具：

```
_fixtures/synthetic_sse_body.txt       合成 SSE 响应体（55 帧，内容全为虚构）
_fixtures/synthetic_sse_expect.json    与它对应的权威解析（text / sources_payload）
_fixtures/make_synthetic.mjs           重新生成上面两份：node _fixtures/make_synthetic.mjs
```

合成夹具的**帧结构与字段结构与真实抓包完全一致**（连「换行要用帧内多行 `data` 编码」这种细节都照做），
所以它照样能验出解析器的边界问题。装置会打印实际用的夹具路径 —— 看到 `_fixtures/` 就说明跑的是合成夹具，
不要把它当成真实抓包的证据。

其余装置（`_autoscroll_*`、`_cite_align_mutate`、`_kbsearch_*`、`_noretr_render_test`、`_rt_mutate`、`_websearch_test`、`_cdp_page_probe`）只依赖 `src/views/ChatView.vue`，在克隆里可直接运行。