<script setup>
/**
 * Agent 交互页（§39.62）——把「决定调工具 → 工具返回 → 作答」逐步显示出来。
 *
 * 为什么不用 EventSource：它不能带 Authorization 头（本接口需要 JWT），
 * 所以用 fetch + ReadableStream 自己解 SSE 帧（event:/data: 两行 + 空行）。
 *
 * 为什么不做「思考过程」面板：实测该模型 5 种参数写法都不返回 reasoning_content
 * （§39.62 探测记录）。这里显示的是**真实发生的工具调用链**，比模型独白更可验证。
 */
import { ref, computed, nextTick, onMounted } from "vue"
import { useRouter } from "vue-router"
// 顶部居中模式切换器（对话 / 助手）——与 ChatView 共用同一个组件
import ModeSwitch from "../components/ModeSwitch.vue"
// 会话状态提到模块级单例（§39.71）：切「对话 ⇄ 助手」时本组件会被卸载重建，
// 放在组件内的 ref 会随实例一起消失，对话记录就丢了。
// 回答正文按 Markdown 渲染：与 chat 页共用同一份 marked 配置（含代码高亮与引用角标），
// 抽到 utils/markdown.js 是为了两页只有一处真源，改渲染规则不必改两个文件。
// stripCitations：agent 页没有来源面板，留着【1】只让答案更生硬（§39.79）。
import { renderMarkdown, stripCitations } from "../utils/markdown.js"
// 来源清单的显示规则（去根标题 / 折叠摘要）放在 utils/sources.js ——
// 纯函数抽出去之后，public/ 下的装置能 import 真函数断言，不用靠人眼验。
import { cleanSectionPath, sourcesHint } from "../utils/sources.js"
import { items, history, existsInfo, running, elapsedMs, resetSession } from "../stores/agentSession.js"

const BASE_URL = "http://127.0.0.1:8000"
const router = useRouter()

const query = ref("")
// 会话状态（时间线 / 多轮上下文 / 存在提示 / 是否运行中 / 耗时）已提到模块级单例（§39.71）：
// 切「对话 ⇄ 助手」时本组件会卸载重建，本地 ref 会一起消失 ⇒ 对话记录丢失。
const listEl = ref(null)

// 「我的文件」精简列表（B2）：只读列表 + 删除按钮，解决「agent 让你删旧的，却无处可删」的断头路。
// 刻意不搬上传（上传留在 chat 页）—— agent 页的定位是「下任务」，不是「管文件」。
const docs = ref([])
const docsError = ref(null)
const deletingId = ref(null)
// 默认收起：列表占掉正文高度，而「看文件」是次要动作（想删才展开）
const docsOpen = ref(false)
// 本轮工具返回的结构化来源（§39.80）：sources 事件比 answer 先到，
// 所以先暂存在这里，等答案落地时再挂到那条回答上（非响应式，只做中转）。
let pendingSources = []

async function loadDocs() {
  try {
    const res = await fetch(`${BASE_URL}/documents`, {
      headers: { Authorization: `Bearer ${token()}` },
    })
    if (res.status === 401) {
      localStorage.removeItem("auth_token")
      router.push("/login")
      return
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    docs.value = Array.isArray(data) ? data : (data.documents || [])
    docsError.value = null
  } catch (e) {
    docsError.value = `加载文件列表失败：${e && e.message ? e.message : e}`
  }
}

/** 删除一份文档：破坏性操作，必须二次确认，并把后端返回的真实删除数量回报出来。 */
async function removeDoc(d) {
  if (running.value || deletingId.value) return
  const ok = window.confirm(
    `确定删除「${d.title || d.filename}」吗？

` +
      `将同时删除它的 ${d.chunk_count ?? d.chunks ?? "?"} 个知识块与向量索引，删除后无法再被检索到。`
  )
  if (!ok) return
  deletingId.value = d.id
  try {
    const res = await fetch(`${BASE_URL}/documents/${d.id}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token()}` },
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const r = await res.json()
    docsError.value = null
    items.value.push({
      role: "assistant",
      content: `已删除「${r.filename || d.filename}」：知识块 ${r.chunks_deleted} 个、向量 ${r.vectors_deleted} 条。`,
      at: now(),
    })
    await loadDocs()
  } catch (e) {
    docsError.value = `删除失败：${e && e.message ? e.message : e}`
  } finally {
    deletingId.value = null
  }
}

onMounted(loadDocs)

const canSend = computed(() => query.value.trim().length > 0 && !running.value)

function now() {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false })
}

async function scrollBottom() {
  await nextTick()
  if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
}

function token() {
  return localStorage.getItem("auth_token")
}

/** 解析一条 SSE 帧：accumulate 里按空行切分。 */
function framesFrom(buf) {
  const out = []
  let idx
  while ((idx = buf.indexOf("\n\n")) >= 0) {
    const raw = buf.slice(0, idx)
    buf = buf.slice(idx + 2)
    let event = "message"
    let data = ""
    for (const line of raw.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim()
      else if (line.startsWith("data:")) data += line.slice(5).trim()
    }
    if (data) out.push({ event, data })
  }
  return { frames: out, rest: buf }
}

function applyFrame(f) {
  let obj = {}
  try {
    obj = JSON.parse(f.data)
  } catch {
    obj = { raw: f.data }
  }
  if (f.event === "tool_call") {
    items.value.push({
      role: "tool_call", name: obj.name, args: obj.args,
      step: obj.step, max: obj.max,      // 「受预算约束」要在界面上看得见，而不是只在日志里
      at: now(),
    })
  } else if (f.event === "tool_result") {
    items.value.push({ role: "tool_result", name: obj.name, content: obj.content, ok: obj.ok, at: now() })
    // 把「同名已存在」的结构化信息提出来，交给模板渲染成按钮 —— 纯文本问句容易被用户漏看
    try {
      const parsed = JSON.parse(String(obj.content || ""))
      if (parsed && parsed.exists === true) {
        existsInfo.value = { filename: parsed.filename || "", existing: parsed.existing || [] }
      }
    } catch {
      /* content 不是 JSON 就不管（有些工具返回纯文本） */
    }
  } else if (f.event === "token") {
    // 逐 token：追加到**同一个**气泡上；没有正在流的气泡才新建。
    // 用「末尾是不是 streaming 状态」来定位，而不是另开一个变量存索引 ——
    // 这样 token_reset / answer 都能用同一条判断，不会出现索引与数组错位。
    const piece = obj.text || ""
    if (!piece) return
    const last = items.value[items.value.length - 1]
    if (last && last.role === "assistant" && last.streaming) last.content += piece
    else items.value.push({ role: "assistant", content: piece, streaming: true, at: now() })
  } else if (f.event === "token_reset") {
    // 后端发现这一轮其实是「工具轮」⇒ 刚流出的那段是模型的「前言」（真·模型输出）。
    // 不删掉，而是转成一行浅色「思考」（§39.78）：它暴露模型打算做什么，有信息量；
    // 但必须与「回答」在视觉上明确区分 —— 前言是模型的自述，不等于它真的做了什么，
    // 真正发生了什么以下面的工具卡片为准（卡片来自工具真实返回值，不是模型说的）。
    const last = items.value[items.value.length - 1]
    if (last && last.role === "assistant" && last.streaming) {
      items.value.pop()
      const preamble = String(last.content || "").trim()
      if (preamble) items.value.push({ role: "thinking", content: preamble, at: last.at })
    }
  } else if (f.event === "sources") {
    // 工具侧的**结构化来源**（§39.80）：来自 search_knowledge 的真实返回值
    // （文件名 / 小节路径 / 截图片段），不是模型自述。先暂存，answer 到达时挂上去。
    pendingSources = Array.isArray(obj.items) ? obj.items : []
  } else if (f.event === "answer") {
    const text = String(obj.content || "")
    const last = items.value[items.value.length - 1]
    if (last && last.role === "assistant" && last.streaming) {
      // 终态权威值：直接覆盖逐 token 拼出来的内容（幂等，避免同一段答案出现两次）
      last.content = text
      last.streaming = false
      if (pendingSources.length) {
        last.sources = pendingSources
        last.sourcesOpen = false   // 默认折叠：来源往往比答案长（§39.81）
      }
    } else if (text) {
      items.value.push({
        role: "assistant",
        content: text,
        at: now(),
        sources: pendingSources,
        sourcesOpen: false,
      })
    }
    pendingSources = []
    if (text) history.value.push({ role: "assistant", content: text })
  } else if (f.event === "done") {
    // 一轮结束后刷新文件列表：入库会新增行、删除会减少行，用户不用手动刷新页面
    loadDocs()
  } else if (f.event === "error") {
    items.value.push({
      role: "error",
      kind: obj.kind || "unknown",
      message: obj.message || "未知错误",
      retryable: !!obj.retryable,
      at: now(),
    })
  }
}

/** 一键回复：把预设文案填进输入框并直接发送（配合 exists 提示用）。 */
async function quick(text) {
  if (running.value) return
  query.value = text
  await send()
}

async function send() {
  const q = query.value.trim()
  if (!q || running.value) return
  const t0 = performance.now()
  running.value = true
  const payloadHistory = history.value.slice()   // 先取快照：后端会在末尾追加本次 query
  existsInfo.value = null
  items.value.push({ role: "user", content: q, at: now() })
  history.value.push({ role: "user", content: q })
  // 立刻清空输入框：消息已经进了时间线，留着原文会让人以为「没发出去」而重复发送
  // （q 是上面 trim 后的快照，所以清空不会影响本次请求体）
  query.value = ""
  await scrollBottom()

  try {
    const res = await fetch(`${BASE_URL}/agent/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token()}`,
      },
      body: JSON.stringify({ query: q, history: payloadHistory }),
    })
    if (res.status === 401) {
      localStorage.removeItem("auth_token")
      router.push("/login")
      return
    }
    if (!res.ok) {
      // 非 SSE 的错误（如 400 参数错误）：按 JSON 解析，保持与 /agent 的契约一致
      const err = await res.json().catch(() => ({}))
      const detail = err.detail
      items.value.push({
        role: "error",
        kind: (detail && detail.error && detail.error.kind) || "http_" + res.status,
        message:
          (detail && detail.error && detail.error.message) ||
          (typeof detail === "string" ? detail : `HTTP ${res.status}`),
        retryable: !!(detail && detail.error && detail.error.retryable),
        at: now(),
      })
      return
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder("utf-8")
    let buf = ""
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const { frames, rest } = framesFrom(buf)
      buf = rest
      for (const f of frames) {
        applyFrame(f)
        await scrollBottom()
      }
    }
  } catch (e) {
    items.value.push({
      role: "error",
      kind: "network",
      message: `请求失败：${e && e.message ? e.message : e}`,
      retryable: true,
      at: now(),
    })
  } finally {
    elapsedMs.value = Math.round(performance.now() - t0)
    running.value = false
    // 流被中断（网络断、预算硬停）时，最后一条可能还挂着 streaming 标记 ⇒ 收尾掉，
    // 否则光标会一直在闪，看起来像"模型还在写"，其实早停了。
    const lastItem = items.value[items.value.length - 1]
    if (lastItem && lastItem.role === "assistant" && lastItem.streaming) lastItem.streaming = false
    await scrollBottom()
  }
}

function reset() {
  // 清空时间线**同时**清多轮上下文：只清时间线会让模型接着上一轮话题回答（§39.71 修正）
  resetSession()
}

</script>

<template>
  <div class="agent-wrap">
    <header class="agent-head">
      <!-- 顶部居中：助手 ⇄ 对话 的双向切换（原来那个单向「回聊天」按钮已被它取代） -->
      <ModeSwitch />
      <div class="agent-title">
        <strong>知识库 Agent</strong>
        <span class="agent-sub">发指令 → 看它调工具 → 拿结果</span>
      </div>
      <div class="agent-actions">
        <span v-if="elapsedMs" class="agent-elapsed">耗时 {{ elapsedMs }} ms</span>
        <button class="agent-btn" :disabled="running" @click="reset">清空</button>
      </div>
    </header>

    <main ref="listEl" class="agent-list">
      <!-- 示例文案只用通用说法：原来这里写着本机绝对路径 + 真实上传文档名，
           既暴露作者目录，也把用户内容带进了发布物（导出闸 ④ 会挡下，E4 也不允许）。
           换成「文件名 + 一句问法」反而更通用：这两个例子正好演示 agent 的两条主能力。 -->
      <p v-if="!items.length" class="agent-empty">
        试试：「把 简历.pdf 导入知识库」或「奖学金评定办法里国家励志奖学金的申请条件是什么」
      </p>

      <div v-for="(it, i) in items" :key="i" class="agent-row" :class="'is-' + it.role">
        <!-- 用户 -->
        <template v-if="it.role === 'user'">
          <div class="agent-user">{{ it.content }}</div>
          <span class="agent-at">{{ it.at }}</span>
        </template>

        <!-- 工具调用 -->
        <template v-else-if="it.role === 'tool_call'">
          <div class="agent-card call">
            <div class="agent-card-head">
              <span class="agent-badge">调用工具</span>
              <code class="agent-tool">{{ it.name }}</code>
              <span v-if="it.step && it.max" class="agent-step">第 {{ it.step }}/{{ it.max }} 步</span>
              <span class="agent-at">{{ it.at }}</span>
            </div>
            <pre class="agent-args">{{ JSON.stringify(it.args, null, 2) }}</pre>
          </div>
        </template>

        <!-- 工具结果 -->
        <template v-else-if="it.role === 'tool_result'">
          <div class="agent-card result">
            <div class="agent-card-head">
              <span class="agent-badge ok">工具返回</span>
              <code class="agent-tool">{{ it.name }}</code>
              <span class="agent-at">{{ it.at }}</span>
            </div>
            <pre class="agent-args">{{ it.content }}</pre>
          </div>
        </template>

        <!-- 模型「前言」：调工具前它的自述（§39.78） -->
        <!-- 为什么单独一行而不是并进回答：它只是模型打算做什么，不保证真做了；
             真正做了什么以下面的工具卡片（真实返回值）为准。视觉上也要弱化，不能抢答案的注意力。 -->
        <template v-else-if="it.role === 'thinking'">
          <div class="agent-thinking">
            <span class="agent-thinking-mark">思考</span>
            <span class="agent-thinking-text">{{ it.content }}</span>
          </div>
        </template>

        <!-- 助手答案 -->
        <template v-else-if="it.role === 'assistant'">
          <div class="agent-answer">
            <div class="agent-card-head">
              <span class="agent-badge answer">回答</span>
              <!-- 逐 token 流式时显示状态：让「正在写」与「已写完」在视觉上可区分（§39.74） -->
              <span v-if="it.streaming" class="agent-streaming">生成中…</span>
              <span class="agent-at">{{ it.at }}</span>
            </div>
            <!-- 和 chat 页一样按 Markdown 渲染（marked + 代码高亮），共用 utils/markdown.js -->
            <div class="markdown-body agent-answer-body" v-html="renderMarkdown(stripCitations(it.content))"></div>
            <!-- 来源清单（§39.80）：数据来自 search_knowledge 的真实返回值，不是模型自述，
                 所以「有依据」这件事在 agent 页终于可以核对；编号沿用模型看到的 n，
                 但正文里的【n】仍被 stripCitations 去掉（点不开的编号是噪音）。 -->
            <div v-if="it.sources && it.sources.length" class="agent-sources">
              <!-- 默认折叠（§39.81）：来源常比答案还长，展开会把回答淹掉。
                   折叠态仍留一行摘要，避免「完全看不出依据在哪」。 -->
              <button
                type="button"
                class="agent-sources-toggle"
                :aria-expanded="it.sourcesOpen ? 'true' : 'false'"
                @click="it.sourcesOpen = !it.sourcesOpen"
              >
                <span class="agent-sources-caret">{{ it.sourcesOpen ? "▾" : "▸" }}</span>
                <span>来源（{{ it.sources.length }}）</span>
                <span v-if="!it.sourcesOpen" class="agent-sources-hint">{{ sourcesHint(it.sources) }}</span>
              </button>
              <ol v-if="it.sourcesOpen" class="agent-sources-list">
                <li
                  v-for="s in it.sources"
                  :key="(s.n || 0) + '-' + (s.document_id || '') + '-' + (s.section_path || '')"
                >
                  <span v-if="s.n" class="agent-source-n">[{{ s.n }}]</span>
                  <span class="agent-source-file">{{ s.filename || "未知文件" }}</span>
                  <span v-if="cleanSectionPath(s.section_path)" class="agent-source-sec">· {{ cleanSectionPath(s.section_path) }}</span>
                  <div v-if="s.snippet" class="agent-source-snippet">{{ s.snippet }}</div>
                </li>
              </ol>
            </div>
          </div>
        </template>

        <!-- 错误 -->
        <template v-else-if="it.role === 'error'">
          <div class="agent-card err">
            <div class="agent-card-head">
              <span class="agent-badge bad">出错</span>
              <code class="agent-tool">{{ it.kind }}</code>
              <span class="agent-at">{{ it.at }}</span>
            </div>
            <p class="agent-err-msg">{{ it.message }}</p>
            <p class="agent-err-hint">{{ it.retryable ? "可以重试（稍后再发一次）" : "不可重试（需要补额度或修配置）" }}</p>
          </div>
        </template>
      </div>

      <!-- 同名已存在：给可点按钮。纯文本问句用户容易漏看，也没有明确出口 -->
      <div v-if="existsInfo" class="agent-quick">
        <span class="agent-quick-label">
          「{{ existsInfo.filename }}」已存在 {{ existsInfo.existing.length }} 份：
          <code v-for="d in existsInfo.existing" :key="d.document_id" class="agent-quick-doc">
            #{{ d.document_id }} {{ d.title }}({{ d.chunks }})
          </code>
        </span>
        <button class="agent-quick-btn" @click="quick('再导一次')">再导一次</button>
        <button class="agent-quick-btn ghost" @click="quick('先不导了')">先不导了</button>
      </div>

      <p v-if="running" class="agent-running">正在执行…</p>
    </main>

    <!-- 我的文件（B2 精简版）：只列 + 删。补的是「agent 让你删旧的，却无处可删」这条断头路。 -->
    <section class="agent-files">
      <button class="agent-files-head" @click="docsOpen = !docsOpen">
        <b>我的文件（{{ docs.length }}）</b>
        <span class="agent-files-caret">{{ docsOpen ? "收起" : "展开" }}</span>
      </button>
      <ul v-if="docsOpen" class="agent-files-list">
        <li v-for="d in docs" :key="d.id" class="agent-file">
          <code class="agent-file-id">#{{ d.id }}</code>
          <span class="agent-file-title">{{ d.title || d.filename }}</span>
          <span class="agent-file-meta">{{ d.chunk_count ?? d.chunks ?? "?" }} 切片</span>
          <button class="agent-file-del" :disabled="!!deletingId" @click="removeDoc(d)">
            {{ deletingId === d.id ? "删除中…" : "删除" }}
          </button>
        </li>
        <li v-if="!docs.length" class="agent-file-empty">
          库里还没有文件。这里刻意没有上传入口 —— 直接跟 agent 说「把 XX 导入」，它会自己去常见目录里找。
        </li>
      </ul>
      <p v-if="docsError" class="agent-files-err">{{ docsError }}</p>
    </section>

    <footer class="agent-compose">
      <textarea
        v-model="query"
        class="agent-input"
        rows="2"
        placeholder="输入指令，例如：把某个 PDF 导入知识库"
        :disabled="running"
        @keydown.enter.exact.prevent="send"
      />
      <button class="agent-send" :disabled="!canSend" @click="send">
        {{ running ? "执行中…" : "发送" }}
      </button>
    </footer>
  </div>
</template>

<style scoped>
.agent-wrap {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: #f6f7f9;
  color: #1f2937;
}
.agent-head {
  /* 供 .mode-switch 绝对居中（不参与 flex 排版，避免挤压标题与右侧按钮） */
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 18px;
  background: #fff;
  border-bottom: 1px solid #e5e7eb;
}
.agent-title strong {
  font-size: 15px;
}
.agent-sub {
  margin-left: 8px;
  font-size: 12px;
  color: #6b7280;
}
.agent-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.agent-elapsed {
  font-size: 12px;
  color: #6b7280;
}
.agent-btn {
  padding: 4px 10px;
  font-size: 12px;
  background: #fff;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  cursor: pointer;
}
.agent-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.agent-list {
  flex: 1;
  overflow-y: auto;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.agent-empty {
  color: #9ca3af;
  font-size: 13px;
}
.agent-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.agent-row.is-user {
  align-items: flex-end;
}
.agent-user {
  max-width: 70%;
  padding: 8px 12px;
  background: #2563eb;
  color: #fff;
  border-radius: 10px;
  font-size: 14px;
  white-space: pre-wrap;
  word-break: break-word;
}
.agent-at {
  font-size: 11px;
  color: #9ca3af;
}
.agent-card {
  max-width: 88%;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 8px 10px;
}
.agent-card.call {
  border-left: 3px solid #6366f1;
}
.agent-card.result {
  border-left: 3px solid #10b981;
}
.agent-card.err {
  border-left: 3px solid #ef4444;
  background: #fef2f2;
}
.agent-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.agent-badge {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  background: #eef2ff;
  color: #4338ca;
}
.agent-badge.ok {
  background: #ecfdf5;
  color: #047857;
}
.agent-badge.answer {
  background: #eff6ff;
  color: #1d4ed8;
}
.agent-badge.bad {
  background: #fee2e2;
  color: #b91c1c;
}
.agent-tool {
  font-size: 12px;
  color: #374151;
}
.agent-args {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: #374151;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 220px;
  overflow: auto;
}
.agent-answer {
  max-width: 88%;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 8px 12px;
}
.agent-answer-body {
  margin: 0;
  font-size: 14px;
  line-height: 1.6;
  /* 去掉 pre-wrap：内容现在由 marked 生成 HTML（.markdown-body），
     留着 pre-wrap 会把 HTML 源码里的换行也渲染成空行 */
  word-break: break-word;
  font-family: inherit;
}
.agent-err-msg {
  margin: 0 0 4px;
  font-size: 13px;
}
.agent-err-hint {
  margin: 0;
  font-size: 12px;
  color: #6b7280;
}
.agent-running {
  font-size: 12px;
  color: #6b7280;
}
.agent-compose {
  display: flex;
  gap: 10px;
  padding: 12px 18px;
  background: #fff;
  border-top: 1px solid #e5e7eb;
}
.agent-input {
  flex: 1;
  resize: none;
  padding: 8px 10px;
  font-size: 14px;
  font-family: inherit;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  outline: none;
}
.agent-input:focus {
  border-color: #2563eb;
}
.agent-send {
  padding: 0 18px;
  font-size: 14px;
  color: #fff;
  background: #2563eb;
  border: none;
  border-radius: 8px;
  cursor: pointer;
}
.agent-send:disabled {
  background: #9ca3af;
  cursor: not-allowed;
}
.agent-quick {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin: 10px 0 4px;
  padding: 10px 12px;
  border: 1px solid #f0c36d;
  border-radius: 10px;
  background: #fffaf0;
  font-size: 13px;
}
.agent-quick-label { color: #8a6d3b; }
.agent-quick-doc {
  margin-right: 6px;
  padding: 1px 6px;
  border-radius: 6px;
  background: #fdf3dc;
  font-size: 12px;
}
.agent-quick-btn {
  padding: 5px 12px;
  border: 1px solid #e0a53a;
  border-radius: 8px;
  background: #fff;
  color: #8a6d3b;
  cursor: pointer;
  font-size: 13px;
}
.agent-quick-btn:hover { background: #fff4d9; }
.agent-quick-btn.ghost { border-color: #d8d8d8; color: #888; }
.agent-files {
  border-top: 1px solid #e6e6e6;
  background: #fafafa;
  font-size: 13px;
  max-height: 168px;
  overflow-y: auto;
}
.agent-files-head {
  display: flex;
  justify-content: space-between;
  width: 100%;
  padding: 8px 16px;
  border: none;
  background: transparent;
  color: #444;
  cursor: pointer;
  font-size: 13px;
}
.agent-files-caret { color: #999; font-size: 12px; }
.agent-files-list { margin: 0; padding: 0 16px 8px; list-style: none; }
.agent-file {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 0;
  border-top: 1px dashed #eee;
}
.agent-file-id { color: #888; font-size: 12px; }
.agent-file-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.agent-file-meta { color: #999; font-size: 12px; }
.agent-file-del {
  padding: 2px 8px;
  border: 1px solid #ddd;
  border-radius: 6px;
  background: #fff;
  color: #c0392b;
  cursor: pointer;
  font-size: 12px;
}
.agent-file-del:hover:not(:disabled) { border-color: #c0392b; }
.agent-file-del:disabled { color: #bbb; cursor: not-allowed; }
.agent-file-empty { padding: 6px 0; color: #999; font-size: 12px; line-height: 1.5; }
.agent-files-err { margin: 0 16px 8px; color: #c0392b; font-size: 12px; }
.agent-step {
  padding: 1px 6px;
  border-radius: 999px;
  background: #eef4ff;
  color: #3b6fd4;
  font-size: 11px;
}
.agent-streaming {
  padding: 1px 6px;
  border-radius: 999px;
  background: #fff7e6;
  color: #b45309;
  font-size: 11px;
  animation: agent-pulse 1.2s ease-in-out infinite;
}
@keyframes agent-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
}
/* 模型前言（思考行，§39.78）：刻意弱化 —— 小字浅灰、无边框、不占卡片，
   因为它是模型的自述（「我打算去找简历」），不等于它真做了；
   真正做了什么由下面的工具卡片（真实返回值）说话。 */
.agent-thinking {
  max-width: 88%;
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 1px 2px 1px 6px;
  color: #6b7280;
  font-size: 12.5px;
  line-height: 1.5;
}
.agent-thinking-mark {
  flex: none;
  padding: 0 6px;
  border-radius: 999px;
  background: #f1f5f9;
  color: #475569;
  font-size: 11px;
}
.agent-thinking-text {
  font-style: italic;
  word-break: break-word;
}
/* 来源清单（§39.80）：放在回答卡片内部、正文之后，视觉上从属，
   但要能看清「哪份文件 · 哪一节」—— 这是 agent 页唯一的可核对依据。 */
.agent-sources {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed #e5e7eb;
}
.agent-sources-toggle {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  padding: 3px 8px 3px 4px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #6b7280;
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
}
.agent-sources-toggle:hover {
  background: #f3f4f6;
  color: #374151;
}
.agent-sources-caret {
  width: 10px;
  display: inline-block;
  color: #9ca3af;
}
.agent-sources-hint {
  color: #9ca3af;
}
.agent-sources-head {
  font-size: 12px;
  color: #6b7280;
  margin-bottom: 4px;
}
.agent-sources-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12.5px;
  color: #374151;
}
.agent-sources-list li {
  margin-bottom: 4px;
}
.agent-source-n {
  color: #6b7280;
  margin-right: 2px;
}
.agent-source-file {
  font-weight: 600;
}
.agent-source-sec {
  color: #6b7280;
  margin-left: 2px;
}
.agent-source-snippet {
  color: #6b7280;
  font-size: 12px;
  line-height: 1.45;
  margin-top: 1px;
  word-break: break-word;
}
</style>