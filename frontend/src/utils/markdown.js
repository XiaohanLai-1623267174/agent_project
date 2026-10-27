/**
 * 共享 Markdown 渲染（chat 页与 agent 页共用一份，避免两页规则漂移）。
 *
 * 为什么要抽：agent 页原来用 `<pre>{{ content }}</pre>` 原样输出，模型吐的
 * `**加粗**`、`- 列表`、表格全被当字面量显示，跟 chat 页观感不一致。
 * 两页各留一份 marked 配置 ⇒ 迟早改一处忘另一处，所以只留这一个真源。
 *
 * 依赖：marked / marked-highlight / highlight.js / dompurify
 * （dompurify 是 §39.77 为堵 v-html 的 XSS 洞新增的；其余三个 chat 页本来就有）
 */
import { marked } from 'marked'
import { markedHighlight } from 'marked-highlight'
import hljs from 'highlight.js'
import DOMPurify from 'dompurify'
import 'highlight.js/styles/atom-one-light.css'

// ★ marked v18 起 setOptions 的 highlight 选项已被【移除】，写在这里会被静默忽略 ——
//   实测渲染出的 <code> 没有任何 hljs-* 类，即语法高亮从未生效（而 highlight.js
//   仍被打进包里）。必须改用官方的 marked-highlight 扩展。
//   兜底刻意用 'plaintext' 而不是 hljs.highlightAuto()：后者每次渲染都要扫描全部语言，
//   而流式回答每收到一个分片就会整段重渲染一次。
marked.use(
  markedHighlight({
    emptyLangClass: 'hljs',
    langPrefix: 'hljs language-',
    highlight(code, lang) {
      const language = hljs.getLanguage(lang) ? lang : 'plaintext'
      return hljs.highlight(code, { language }).value
    },
  }),
)

marked.setOptions({ breaks: true, gfm: true })

/**
 * 把回答正文里的引用编号 [n] 变成可点击角标（点击后展开对应来源）。
 *
 * ★ 必须先按 <pre>/<code> 把 HTML 切开，只处理【代码块之外】的片段：
 *   代码里到处是 a[0]、x[1] 这种下标，无脑全局替换会直接把用户看到的代码改坏。
 *   String.split 带捕获组时，分隔符（代码段）会落在奇数下标 —— 正好跳过它们。
 *
 * ★★ sources 参数：**只有真的存在对应来源的编号才配当「依据」**。
 *   提示词里写了「绝不编造编号」，但那只是**叮嘱**：一旦模型写上 [7] 而本条只有 5 条来源，
 *   旧实现照样把它渲染成和真引用**一模一样**、还带 tooltip 的可点角标 —— 点下去毫无反应。
 *   那等于 UI 承诺了它没有的依据。现在这种编号渲染成「幽灵角标」：
 *   **字符照留**（绝不改动回答原文一个字），但样式与文案都表明「没有对应来源」。
 *
 * agent 页传 sources 为空时同理：agent 的上下文来自工具返回，通常没有 [n] 角标；
 * 万一模型自己写了编号，也会被标成幽灵角标（没有对应来源就不暗示可点）。
 */
export function linkifyCitations(html, sources) {
  if (!html || html.indexOf('[') === -1) return html
  const available = new Set(
    (Array.isArray(sources) ? sources : [])
      .map(s => Number(s && s.n))
      .filter(n => Number.isInteger(n) && n > 0)
  )
  return html
    .split(/(<pre[\s\S]*?<\/pre>|<code[\s\S]*?<\/code>)/g)
    .map((seg, i) => {
      if (i % 2 === 1) return seg          // 奇数位 = 代码段，原样放回
      return seg.replace(/\[(\d{1,2})\]/g, (whole, num) => (
        available.has(Number(num))
          ? `<sup class="cite-ref" data-cite="${num}" title="查看来源 ${num}">${num}</sup>`
          : `<sup class="cite-ref is-ghost" data-cite="${num}" title="本条没有来源 ${num} —— 编号可能是模型编造的，请勿当作依据">${num}</sup>`
      ))
    })
    .join('')
}

/**
 * 消毒配置（§39.77）。
 *
 * 为什么必须有这一步：`v-html` 会把模型输出当 HTML 插进页面，而 `marked`
 * **默认不消毒** —— 模型只要吐出 `<img src=x onerror=...>`，那个事件属性就会执行。
 * 回答内容来自用户上传的文档，属于典型的间接注入面（文档里藏一段 HTML 即可）。
 *
 * 为什么用默认白名单而不自定义标签集：marked 产出的是标准 Markdown 元素
 * （p / ul / table / pre / code / span.hljs-* / sup.cite-ref），DOMPurify 默认都放行；
 * 自定义白名单反而容易漏掉表格或代码高亮的 class。这里只额外禁掉三类
 * 明确用不到、且历史上常被拿来做坏事的东西：内联样式、表单、嵌入类标签。
 * 注意 `data-cite` 依赖 DOMPurify 默认放行 data-* —— 所以**不能**设 ALLOW_DATA_ATTR:false。
 */
const SANITIZE_CONFIG = {
  FORBID_TAGS: ['style', 'form', 'iframe', 'object', 'embed'],
  // 内联 style 可以整块遮挡页面（点击劫持式），而 Markdown 根本用不到它
  FORBID_ATTR: ['style'],
}

/** 纯消毒入口（装置直接用它做载荷测试） */
export function sanitizeHtml(html) {
  return DOMPurify.sanitize(String(html), SANITIZE_CONFIG)
}

/**
 * 去掉答案里的引用编号（`【1】` 与 `[1]` 两种写法）。
 *
 * 为什么 agent 页要删、chat 页要留：
 *   · chat 页的角标点得开（右侧能展开对应来源），删了反而丢信息；
 *   · agent 页没有来源面板 —— 数字留着点不动、也不知道指哪份文件，只是让答案更生硬
 *     （用户原话：「【1】我知道这个是检索结果，但是没法查看，还不如删掉」）。
 *   · 另一个直接原因：提示词要求模型用**全角** `【n】`，而 linkifyCitations 只认半角 `[n]`，
 *     所以 agent 页这两个数字根本没被转成角标，是原样文字。
 *
 * ★ 必须先把 fenced code / 行内 code 摘出去：代码里到处是 `a[1]`、`arr[0]`，
 *   无脑全局替换会把用户看到的代码改坏（与 linkifyCitations 同一个坑）。
 *
 * ★ 这不是唯一解：search_knowledge 的返回值里其实**有**结构化的 sources
 *   （document_id / filename / snippet），只要后端把 sources 单独发一个 SSE 事件，
 *   就能在 agent 页做「来源清单」。届时把这个函数从模板里摘掉即可。
 */
export function stripCitations(text) {
  if (!text) return text
  const src = String(text)
  if (!/[【[]/.test(src)) return src
  return src
    .split(/(```[\s\S]*?```|`[^`\n]*`)/g)
    .map((seg, i) => (i % 2 === 1 ? seg : seg.replace(/[【[]\s*(\d{1,2})\s*[】\]]/g, '')))
    .join('')
    // 删掉编号后常留下多余空格或标点前的空格，顺手收一下（只收空格，不动换行）
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/[ \t]+([，。；：、！？）」』])/g, '$1')
}

/**
 * Markdown → HTML 的统一入口（**已消毒**）。
 * 解析异常时退回「转义后的纯文本」：宁可难看一点，也不能把半截 HTML 塞进 v-html。
 */
export function renderMarkdown(text, sources) {
  if (!text) return ''
  try {
    return sanitizeHtml(linkifyCitations(marked.parse(text), sources))
  } catch {
    return sanitizeHtml(String(text).replace(/</g, '&lt;').replace(/>/g, '&gt;'))
  }
}