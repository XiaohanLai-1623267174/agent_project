/**
 * [零 API] 「知识库搜索」按钮的行为级验证：真模板 → 真编译 → SSR → 断言**渲染结果**。
 *
 * 为什么不能只 grep：本项要防的是"按钮看着在、但状态不对/点不出效果"——
 *   例如开启态不显示高亮（用户不知道已经开着）、流式中还能点（并发发两条）。
 *   grep 只能证明字符串在文件里，证明不了**渲染出来是什么状态**。
 *
 * ★ 只取**按钮自己那一段** HTML 再断言：整页字符串里做子串判定会被同页其它元素、
 *   甚至模板注释污染（§39.29.6 陷阱 #14/#15 就是这么被骗的）。
 * ★ 防空：每个状态都先断言"这段按钮真的渲染出来了"，否则"不该有"的断言全是白送。
 *
 * 用法：node _kbsearch_test.mjs
 * 反向对照：KBSEARCH_VUE=<坏副本.vue> node _kbsearch_test.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import * as Vue from 'vue'
import { compile } from '@vue/compiler-dom'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'

const HERE = dirname(fileURLToPath(import.meta.url))
const VUE_PATH = process.env.KBSEARCH_VUE || join(HERE, 'src', 'views', 'ChatView.vue')
const sfc = readFileSync(VUE_PATH, 'utf8')

const results = []
function check(scenario, name, ok, detail = '') {
  results.push([scenario, name, !!ok])
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} [${scenario}] ${name.padEnd(46)} ${detail}`)
}

// ---------- 1) 原样切出「输入框那一块」----------
const tpl = sfc.match(/<template>([\s\S]*)<\/template>/)
if (!tpl) throw new Error('ChatView.vue: <template> not found')
const t = tpl[1]
const startAt = t.indexOf('<div class="input-wrapper">')
const hintAt = t.indexOf('<p class="input-hint">')
if (startAt < 0 || hintAt < 0 || hintAt < startAt) {
  throw new Error('模板锚点缺失：input-wrapper / input-hint（模板改动后必须来改本脚本，不许静默跳过）')
}
const snippet = t.slice(startAt, hintAt)
for (const must of ['class="input-wrapper"', 'btn-attach', 'chat-input', 'btn-send']) {
  if (!snippet.includes(must)) throw new Error('切出的片段缺关键标记: ' + must)
}
if (!snippet.trimEnd().endsWith('</div>')) throw new Error('切出的片段不是完整元素')

// ---------- 2) 编译（丢注释，与生产一致）----------
const { code } = compile(snippet, { mode: 'function', prefixIdentifiers: true, comments: false })
if (code.includes('with (')) throw new Error('still generated a with() statement')
const render = new Function('Vue', code)(Vue)
if (typeof render !== 'function') throw new Error('compile produced no render fn')

// ---------- 2.5) 期望值从【模板本身】提取，绝不手抄 ----------
// ★ 第一版是把 title 文案手抄进断言的，结果当场红了一次：抄漏一个字（"查知识库"→"查库"）。
//   手抄的期望值既会过期、又会把复制粘贴的错当成"代码的错"。改为提取真值，
//   并且**断言两态文案必须不同**——否则"随状态切换"这个断言就是白送的。
const titleExpr = snippet.match(/:title="forceSearch\s*\?\s*'([^']+)'\s*:\s*'([^']+)'"/)
if (!titleExpr) throw new Error('无法从模板提取「知识库搜索」按钮的 title 表达式（模板改了就来改本脚本）')
const [ARMED_TITLE, IDLE_TITLE] = [titleExpr[1], titleExpr[2]]
if (ARMED_TITLE === IDLE_TITLE) throw new Error('两态 title 文案相同 ⇒ 下面的切换断言毫无意义')

// ★ 开关的**默认值**也从源码提取，但"默认必须是开"是**用户拍板的需求**（不是实现细节）⇒ 钉死。
//   这样"把默认改回 false"会被抓住，而"改了默认值的写法"不会让测试跟着漂移。
//   （第十二轮起默认值由 FORCE_SEARCH_DEFAULT 常量承载，不再是 ref(true) 字面量。）
const defMatch = sfc.match(/const FORCE_SEARCH_DEFAULT = (true|false)/)
if (!defMatch) throw new Error('找不到 const FORCE_SEARCH_DEFAULT = ...（源码改了就来改本脚本）')
const DEFAULT_ON = defMatch[1] === 'true'

// ---------- 3) 取出"按钮自己那一段"再断言 ----------
function buttonHtml(html, cls) {
  const re = new RegExp(`<button[^>]*\\b${cls}\\b[^>]*>[\\s\\S]*?</button>`)
  const m = html.match(re)
  return m ? m[0] : ''
}

const noop = () => {}
const state = (over = {}) => ({
  isStreaming: false, kbHasFile: false, question: '', kbRef: null, inputEl: null,
  forceSearch: false,
  send: noop, onKeydown: noop, autoResize: noop, stopStreaming: noop,
  toggleForceSearch: noop,
  ...over,
})

const SCENARIOS = [
  {
    label: '①开',
    st: { forceSearch: true },
    // 「知识库搜索」这个名字是用户定的（§39.27.3），属于需求 ⇒ 这一条**必须钉死**，不许随代码漂移
    mustIn: ['知识库搜索', 'btn-kbsearch on', ARMED_TITLE],
    mustNotIn: [IDLE_TITLE, 'disabled'],
  },
  {
    label: '②关',
    st: { forceSearch: false },
    // ★ 关闭态也要肉眼可见：否则用户不知道"逐题判断又回来了"
    mustIn: [IDLE_TITLE],
    mustNotIn: ['btn-kbsearch on', ARMED_TITLE],
  },
  {
    label: '③流式中',
    st: { isStreaming: true, forceSearch: true, question: '综测怎么算' },
    // 流式中不许再点（否则会并发发第二条）；开关状态本身保持不变
    mustIn: [ARMED_TITLE, 'btn-kbsearch on', 'disabled'],
    mustNotIn: [],
  },
]

console.log('='.repeat(96))
console.log('「知识库搜索」按钮：真模板 → 编译 → SSR → 断言按钮自身的状态（零 API）')
console.log('='.repeat(96))
console.log(`  模板来源: ${VUE_PATH}`)
console.log(`  片段: ${Buffer.byteLength(snippet)} B（.input-wrapper，原样切出）`)
console.log(`  期望文案（从模板提取，非手抄）: 开启="${ARMED_TITLE}" / 未开启="${IDLE_TITLE}"`)

for (const sc of SCENARIOS) {
  const html = await renderToString(createSSRApp({ render, setup: () => state(sc.st) }))
  const btn = buttonHtml(html, 'btn-kbsearch')

  // ★ 防空：先把"按钮真的渲染出来了 + 输入框真的渲染出来了"钉死
  const composed = btn.length > 0 && html.includes('class="chat-input"')
  check(sc.label, '★防空过：按钮与输入框都渲染出来了', composed,
        `btn=${btn.length}B html=${Buffer.byteLength(html)}B`)

  for (const s of sc.mustIn) {
    const ok = btn.includes(s)
    check(sc.label, `按钮内含「${s}」`, ok, '')
    // ★ 失败时把"按钮自己那一段"原样打出来 —— 否则只能靠猜（本项第一版就猜错了一次）
    if (!ok) console.log('       实际按钮 HTML: ' + btn)
  }
  for (const s of sc.mustNotIn) {
    const ok = !btn.includes(s)
    check(sc.label, `按钮内不含「${s}」`, ok, '')
    if (!ok) console.log('       实际按钮 HTML: ' + btn)
  }

  // 结构：按钮必须在 .input-wrapper 里（否则它可能被挪到别处还"看着正常"）
  const idxWrap = html.indexOf('class="input-wrapper"')
  const idxBtn = html.indexOf('btn-kbsearch')
  check(sc.label, '按钮位于 .input-wrapper 内', idxWrap >= 0 && idxBtn > idxWrap, '')
}

// 流式态额外确认"停止按钮出现"（否则说明整块渲染逻辑坏了，上面的断言会白绿）
const streamingHtml = await renderToString(createSSRApp({ render, setup: () => state({ isStreaming: true }) }))
check('③流式中', '出现停止按钮（btn-stop）', streamingHtml.includes('btn-stop'), '')
check('③流式中', '发送按钮消失（btn-send）', !streamingHtml.includes('btn-send'), '')

// ---------- 4) 默认态：需求钉死 + 行为核对 ----------
// 「默认走 RAG」是用户 2026-10-12 拍板的**需求**，不是实现细节 ⇒ 必须钉死，
// 但期望值仍从源码提取（这样"改了实现"不会让测试跟着漂移，"改了默认值"会被抓住）。
check('④默认态', '★ 开关默认是【开】= 默认走 RAG（用户拍板）', DEFAULT_ON, `FORCE_SEARCH_DEFAULT=${defMatch[1]}`)
{
  const html = await renderToString(createSSRApp({ render, setup: () => state({ forceSearch: DEFAULT_ON }) }))
  const btn = buttonHtml(html, 'btn-kbsearch')
  check('④默认态', '★防空过：默认态也渲染出了按钮', btn.length > 0, `${btn.length}B`)
  check('④默认态', `默认渲染 = 开启态（${DEFAULT_ON ? '高亮' : '灰'}）`,
        DEFAULT_ON ? btn.includes('btn-kbsearch on') : !btn.includes('btn-kbsearch on'), '')
  check('④默认态', '默认 title 与默认状态一致',
        btn.includes(DEFAULT_ON ? ARMED_TITLE : IDLE_TITLE), '')
  check('④默认态', '默认 aria-pressed 与默认状态一致',
        btn.includes(`aria-pressed="${DEFAULT_ON ? 'true' : 'false'}"`), '')
}

const nOk = results.filter(([, , ok]) => ok).length
console.log()
console.log(`合计 ${nOk}/${results.length} 通过${nOk === results.length ? '' : '   <-- 有失败'}`)
console.log('='.repeat(96))
process.exit(nOk === results.length ? 0 : 1)