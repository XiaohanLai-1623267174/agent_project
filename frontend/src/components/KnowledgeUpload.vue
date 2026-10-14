<template>
  <div class="kb-root">
    <!-- 隐藏的文件选择器：由输入框里的回形针按钮触发（见 ChatView 的 .btn-attach） -->
    <input ref="fileEl" class="kb-file" type="file" accept="application/pdf,.pdf"
           @change="onFileChange" />

    <!-- ===== 附件条：选好文件后出现在输入框上方（标准 AI 对话界面形态） ===== -->
    <div v-if="file || uploading" class="kb-strip">
      <div class="kb-chip" :class="{ busy: uploading }">
        <svg class="kb-chip-icon" width="15" height="15" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
             stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14,2 14,8 20,8" />
        </svg>
        <span class="kb-chip-name" :title="fileName">{{ fileName }}</span>
        <span class="kb-chip-size">{{ fileSize }}</span>

        <span v-if="uploading" class="kb-chip-progress">
          <span class="kb-spinner"></span>解析中 {{ elapsed }}s
        </span>
        <button v-else class="kb-chip-x" title="移除" @click="clearFile">✕</button>
      </div>
      <!-- 回形针只负责选文件，真正的上传由发送按钮触发（与主流 AI 对话界面一致） -->
      <p v-if="!uploading" class="kb-strip-hint">点右下角 <b>发送</b> 开始上传并解析入库</p>
      <p v-else class="kb-strip-hint">
        PDF 清洗 → 逐块元数据 → 向量化。小文件数十秒，篇幅大的可能几分钟，请勿关闭页面。
      </p>
    </div>

    <!-- ===== 结果提示 ===== -->
    <div v-if="result" class="kb-strip">
      <p class="kb-msg ok">
        ✅ 已入库「{{ result.title }}」，切成 <b>{{ result.chunk_count }}</b> 块（文档 ID {{ result.document_id }}）
      </p>
    </div>
    <div v-if="error" class="kb-strip">
      <p class="kb-msg bad">
        ❌ 入库失败：{{ error }}
        <button class="kb-msg-x" title="关闭" @click="error = ''">✕</button>
      </p>
    </div>

    <!-- ===== 我的知识库：一行轻量入口，点开是列表 ===== -->
    <p class="kb-linkline">
      <button class="kb-link" @click="openList">我的知识库（{{ docs.length }}）</button>
    </p>

    <!-- ===== 列表面板 ===== -->
    <div v-if="listOpen" class="kb-overlay" @click.self="listOpen = false">
      <section class="kb-modal" role="dialog" aria-modal="true" aria-label="我的知识库">
        <header class="kb-head">
          <div>
            <h3 class="kb-title">我的知识库</h3>
            <p class="kb-sub">这里只有你自己的文档；入库后即可在对话里检索到</p>
          </div>
          <button class="kb-close" title="关闭" @click="listOpen = false">✕</button>
        </header>

        <div class="kb-docs-head">
          <span>共 {{ docs.length }} 份</span>
          <button class="kb-refresh" :disabled="loadingDocs" @click="loadDocs">
            {{ loadingDocs ? '刷新中…' : '刷新' }}
          </button>
        </div>
        <p v-if="deleteMsg" class="kb-msg" :class="deleteMsg.ok ? 'ok' : 'bad'">{{ deleteMsg.text }}</p>
        <p v-if="docsError" class="kb-msg bad">{{ docsError }}</p>
        <p v-else-if="docs.length === 0" class="kb-empty">还没有文档，点输入框里的回形针传一份</p>
        <ul v-else class="kb-doclist">
          <li v-for="d in docs" :key="d.id" class="kb-doc">
            <span class="kb-doc-title" :title="d.filename">{{ d.title || d.filename }}</span>
            <span class="kb-doc-meta">
              <span class="kb-badge" :class="d.status">{{ statusText(d.status) }}</span>
              <span :class="{ warn: d.chunk_count === 0 }">{{ d.chunk_count }} 块</span>
              <!-- 删除：误传的文档必须能自救，否则会永久留在库里拉低检索质量 -->
              <button class="kb-doc-del" :disabled="deletingId !== null"
                      :title="`删除「${d.title || d.filename}」`" @click="removeDoc(d)">
                {{ deletingId === d.id ? '删除中…' : '删除' }}
              </button>
            </span>
          </li>
        </ul>
      </section>
    </div>
  </div>
</template>

<script setup>
/**
 * 知识库上传：回形针选文件 → 附件条 → 发送按钮触发上传 → 自动解析入库。
 *
 * 形态刻意对齐【一般 AI 对话界面】：
 *   · 回形针图标在输入框内左侧。按钮本体在 ChatView 的 .input-wrapper 里，
 *     由父组件调用这里 expose 出来的 pickFile() 打开隐藏的 file input ——
 *     这样一个组件就能同时管住"输入框里的按钮"和"输入框上方的附件条"，
 *     不必为共享状态再引入 store。
 *   · 选好文件先在输入框上方显示附件条（文件名/大小/✕），
 *     再由【发送按钮】启动上传（父组件 send() 里第一行就是 kbHasFile 判断）。
 *   · 解析中显示计时 + 明确写出要跑哪三步，并禁止再选文件。
 *
 * 为什么不用弹窗做上传：主流对话界面都不是"弹窗里上传"。
 * 弹窗只留给【我的知识库】列表这种非高频操作。
 */
import { computed, ref, onMounted, onBeforeUnmount } from 'vue'
import { apiGet, apiDelete, uploadDocument } from '../api/request.js'

const fileEl = ref(null)
const file = ref(null)
const uploading = ref(false)
const elapsed = ref(0)
const result = ref(null)
const error = ref('')

const docs = ref([])
const loadingDocs = ref(false)
const docsError = ref('')
const listOpen = ref(false)
const deletingId = ref(null)      // 正在删除的文档 id（用于禁用按钮 + 显示"删除中…"）
const deleteMsg = ref(null)       // { ok, text } —— 删除结果必须显式回报给用户

let timer = null
let hideTimer = null

const fileName = computed(() => file.value?.name || '上传中…')
const fileSize = computed(() => (file.value ? prettySize(file.value.size) : ''))

// ========== 选文件（供父组件的回形针按钮调用） ==========
function pickFile() {
  if (uploading.value) return
  fileEl.value?.click()
}

function acceptFile(f) {
  error.value = ''
  result.value = null
  if (!f) return
  // 浏览器给 .pdf 的 type 可能是空串（取决于系统注册表），所以后缀一起判
  const isPdf = f.type === 'application/pdf' || /\.pdf$/i.test(f.name)
  if (!isPdf) {
    file.value = null
    error.value = `只支持 PDF 文件，你选的是「${f.name}」`
    return
  }
  file.value = f
}

function onFileChange(e) {
  acceptFile(e.target.files?.[0])
  e.target.value = ''   // 清掉，否则再选同一个文件不触发 change
}

function clearFile() {
  if (uploading.value) return
  file.value = null
}

function prettySize(n) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

// ========== 上传（供父组件的发送按钮调用） ==========
function stopTimer() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

async function startUpload() {
  if (!file.value || uploading.value) return
  uploading.value = true
  error.value = ''
  result.value = null
  elapsed.value = 0
  stopTimer()
  timer = setInterval(() => { elapsed.value += 1 }, 1000)

  try {
    const data = await uploadDocument(file.value, '')
    file.value = null
    result.value = data
    // 成功提示自动消失，避免长期占着输入框上方
    if (hideTimer) clearTimeout(hideTimer)
    hideTimer = setTimeout(() => { result.value = null }, 8000)
    try {
      docs.value = await apiGet('/documents')
    } catch {
      /* 列表刷新失败不影响"已入库"这个结论 */
    }
  } catch (e) {
    error.value = e.message || String(e)
  } finally {
    uploading.value = false
    stopTimer()
  }
}

// ========== 我的知识库 ==========
async function openList() {
  listOpen.value = true
  await loadDocs()
}

async function loadDocs() {
  loadingDocs.value = true
  docsError.value = ''
  try {
    docs.value = await apiGet('/documents')
  } catch (e) {
    docs.value = []
    // 后端未重启时该端点还不存在（404）—— 说清楚，而不是让列表空白
    docsError.value = `读取文档列表失败：${e.message}`
  } finally {
    loadingDocs.value = false
  }
}

// ========== 删除文档 ==========
/**
 * 删除一份文档（后端会同时清掉它的向量与检索缓存）。
 *
 * 为什么必须二次确认：这是【不可撤销】的破坏性操作，删掉的是知识库里真实
 * 可检索的内容。误删一份重要文档的代价，远大于多一次点击。
 *
 * 为什么要把后端返回的条数显示出来：只回一个 200 无法让用户确认"到底删掉了
 * 什么"。后端会如实回报 chunks/vectors 条数与残留量，这里原样呈现。
 */
async function removeDoc(d) {
  if (deletingId.value !== null) return
  const name = d.title || d.filename
  const ok = window.confirm(
    `确定删除「${name}」吗？\n\n` +
    `将同时删除它的 ${d.chunk_count} 个知识块与向量索引，删除后无法再被检索到。\n` +
    `（上传的 PDF 原件会保留）`
  )
  if (!ok) return

  deletingId.value = d.id
  deleteMsg.value = null
  try {
    const r = await apiDelete(`/documents/${d.id}`)
    const warns = []
    if (r.vectors_left > 0) warns.push(`向量库仍有 ${r.vectors_left} 条残留（已不影响检索）`)
    if (r.bm25_warning) warns.push(`检索缓存失效失败：${r.bm25_warning}`)
    deleteMsg.value = {
      ok: warns.length === 0,
      text: `已删除「${r.filename || name}」：知识块 ${r.chunks_deleted} 个、向量 ${r.vectors_deleted} 条`
            + (warns.length ? `　⚠️ ${warns.join('；')}` : ''),
    }
    await loadDocs()
  } catch (e) {
    deleteMsg.value = { ok: false, text: `删除失败：${e.message}` }
  } finally {
    deletingId.value = null
  }
}

const STATUS_TEXT = { completed: '已完成', failed: '失败', processing: '处理中' }
function statusText(s) {
  return STATUS_TEXT[s] || s
}

onMounted(() => {
  loadDocs()
})

onBeforeUnmount(() => {
  stopTimer()
  if (hideTimer) clearTimeout(hideTimer)
})

// 暴露给父组件：hasFile 决定发送按钮是否可用 / 是否走上传分支
defineExpose({ pickFile, startUpload, hasFile: computed(() => !!file.value) })
</script>

<style scoped>
/* 组件放在 .input-area 里（在 .input-wrapper 之前），只负责输入框【上方】的
   附件条与提示；输入框内的回形针按钮样式在 ChatView 的 .btn-attach */
.kb-root {
  max-width: 820px;
  margin: 0 auto;
}
.kb-file { display: none; }

/* ===== 附件条 ===== */
.kb-strip { margin-bottom: 8px; }

.kb-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  max-width: 100%;
  padding: 6px 8px 6px 10px;
  border: 1px solid var(--border, #e2e4ec);
  background: var(--bg-secondary, #fff);
  border-radius: var(--radius-sm, 8px);
  font-size: 12.5px;
}
.kb-chip.busy {
  border-color: var(--accent, #4d6bfe);
  background: var(--accent-dim, rgba(77, 107, 254, 0.08));
}
.kb-chip-icon { flex-shrink: 0; color: var(--accent, #4d6bfe); }
.kb-chip-name {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-primary, #1a1a2e);
}
.kb-chip-size { flex-shrink: 0; color: var(--text-muted, #999aae); }
.kb-chip-progress {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  color: var(--accent, #4d6bfe);
  font-variant-numeric: tabular-nums;
}
.kb-chip-x {
  flex-shrink: 0;
  width: 20px;
  height: 20px;
  border: none;
  background: transparent;
  color: var(--text-muted, #999aae);
  font-size: 11px;
  cursor: pointer;
  border-radius: 4px;
}
.kb-chip-x:hover { background: var(--bg-hover, #f0f1f5); color: var(--text-secondary, #5a5a72); }

.kb-strip-hint {
  margin: 6px 2px 0;
  font-size: 11.5px;
  color: var(--text-muted, #999aae);
  line-height: 1.5;
}

/* 加载小圆环 */
.kb-spinner {
  width: 11px;
  height: 11px;
  border: 1.6px solid rgba(77, 107, 254, 0.28);
  border-top-color: var(--accent, #4d6bfe);
  border-radius: 50%;
  animation: kb-spin 0.7s linear infinite;
}
@keyframes kb-spin { to { transform: rotate(360deg); } }

/* ===== 结果提示 ===== */
.kb-msg {
  margin: 0;
  padding: 7px 10px;
  border-radius: var(--radius-sm, 8px);
  font-size: 12.5px;
  line-height: 1.55;
}
.kb-msg.ok {
  background: rgba(16, 185, 129, 0.08);
  border: 1px solid rgba(16, 185, 129, 0.26);
  color: #0f766e;
}
.kb-msg.bad {
  background: rgba(239, 68, 68, 0.07);
  border: 1px solid rgba(239, 68, 68, 0.24);
  color: #b91c1c;
  word-break: break-word;
}
.kb-msg-x {
  float: right;
  border: none;
  background: transparent;
  color: inherit;
  opacity: 0.6;
  cursor: pointer;
  font-size: 11px;
}
.kb-msg-x:hover { opacity: 1; }

/* ===== 我的知识库入口 ===== */
.kb-linkline { margin: 8px 0 0; text-align: center; }
.kb-link {
  border: none;
  background: transparent;
  color: var(--text-muted, #999aae);
  font-size: 11px;
  font-family: inherit;
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 2px;
  padding: 0;
}
.kb-link:hover { color: var(--accent, #4d6bfe); }

/* ===== 列表面板 ===== */
.kb-overlay {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(26, 26, 46, 0.32);
}
.kb-modal {
  width: 100%;
  max-width: 520px;
  max-height: 80vh;
  overflow-y: auto;
  padding: 20px 22px 22px;
  background: var(--bg-surface, #fff);
  border-radius: var(--radius-md, 14px);
  box-shadow: var(--shadow-md, 0 4px 16px rgba(0, 0, 0, 0.06));
  color: var(--text-primary, #1a1a2e);
}
.kb-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.kb-title { font-size: 15px; font-weight: 600; margin: 0 0 4px; }
.kb-sub { font-size: 12px; color: var(--text-muted, #999aae); margin: 0; }
.kb-close {
  flex-shrink: 0;
  width: 26px;
  height: 26px;
  border: none;
  background: transparent;
  color: var(--text-muted, #999aae);
  cursor: pointer;
  border-radius: 6px;
}
.kb-close:hover { background: var(--bg-hover, #f0f1f5); }

.kb-docs-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12.5px;
  color: var(--text-secondary, #5a5a72);
  margin-bottom: 6px;
}
.kb-refresh {
  border: 1px solid var(--border, #e2e4ec);
  background: transparent;
  color: var(--text-secondary, #5a5a72);
  padding: 3px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
}
.kb-refresh:hover:not(:disabled) { background: var(--bg-hover, #f0f1f5); }
.kb-refresh:disabled { opacity: 0.5; cursor: not-allowed; }

.kb-empty { font-size: 12.5px; color: var(--text-muted, #999aae); margin: 8px 0 0; }
.kb-doclist { list-style: none; margin: 0; padding: 0; }
.kb-doc {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 9px 2px;
  border-bottom: 1px solid var(--border-light, #eef0f5);
  font-size: 13px;
}
.kb-doclist > .kb-doc:last-child { border-bottom: none; }
.kb-doc-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kb-doc-meta {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-muted, #999aae);
}
/* chunk_count=0 是"入库了但检索不到"，必须显眼 */
.kb-doc-meta .warn { color: var(--error, #ef4444); font-weight: 600; }
.kb-badge {
  padding: 1px 7px;
  border-radius: var(--radius-full, 9999px);
  font-size: 11px;
  background: var(--bg-input, #f0f2f5);
  color: var(--text-secondary, #5a5a72);
}
.kb-badge.completed { background: rgba(16, 185, 129, 0.12); color: #0f766e; }
.kb-badge.failed { background: rgba(239, 68, 68, 0.12); color: #b91c1c; }
.kb-badge.processing { background: rgba(77, 107, 254, 0.12); color: var(--accent, #4d6bfe); }

/* 删除：破坏性操作 —— 平时低调，hover 才显出危险色，避免误点 */
.kb-doc-del {
  border: 1px solid var(--border, #e2e4ec);
  background: transparent;
  color: var(--text-muted, #999aae);
  padding: 2px 9px;
  border-radius: 6px;
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
  flex-shrink: 0;
}
.kb-doc-del:hover:not(:disabled) {
  border-color: var(--error, #ef4444);
  color: var(--error, #ef4444);
  background: rgba(239, 68, 68, 0.06);
}
.kb-doc-del:disabled { opacity: 0.45; cursor: not-allowed; }
</style>