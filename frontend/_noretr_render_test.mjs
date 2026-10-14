/**
 * [零 API] B 项【行为级】验证：「这条没检索」与「检索了但没数据」在界面上必须可分辨。
 *
 * 为什么不能只 grep：本轮要防的核心风险恰恰是【结构性】的 ——
 *   新提示块如果被塞进 `.msg-sources` 的 v-if/v-else-if 链里，
 *   就会截断那条链、把「未检索到相关资料」顶掉（§39.24.3 记过这个坑）。
 *   grep 只能证明"字符串在文件里"，证明不了"渲染出来是什么"。
 * 所以这里走真东西：
 *   ① 从 ChatView.vue 里**原样切出**消息行模板（v-for 的那个 .message-row）
 *   ② 用 @vue/compiler-dom 编成 render 函数（与 Vite 编 SFC 同模式）
 *   ③ SSR 出 HTML，再对 HTML 断言"有 / 没有"
 *   —— 断言的对象是"渲染结果"，不是源码文本。
 *
 * ★ 防空断言（本文件的假绿防线）：每条"不该出现"的断言，在【空 HTML】上都会自动通过。
 *   所以每个场景都先断言 HTML 里确实渲染出了这条回答的正文（.msg-bubble + 正文内容），
 *   否则整场判定作废。这是本会话第三种假绿的同一族教训（§39.27.5 / §39.28）。
 *
 * ★ 唯一的人工补足：renderMarkdown 用桩函数（正文渲染不在本项验证范围内），
 *   其余数据全部按真实形态构造。
 *
 * 反向对照（证明它真的会红）：
 *   NORETR_VUE=<坏副本.vue> node _noretr_render_test.mjs
 *
 * 用法：node _noretr_render_test.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import * as Vue from 'vue'
import { compile } from '@vue/compiler-dom'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'

const HERE = dirname(fileURLToPath(import.meta.url))
const VUE_PATH = process.env.NORETR_VUE || join(HERE, 'src', 'views', 'ChatView.vue')
const sfc = readFileSync(VUE_PATH, 'utf8')

const results = []
function check(scenario, name, ok, detail = '') {
  results.push([scenario, name, !!ok])
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} [${scenario}] ${name.padEnd(46)} ${detail}`)
}

// ---------- 1) 原样切出消息行模板 ----------
const tpl = sfc.match(/<template>([\s\S]*)<\/template>/)
if (!tpl) throw new Error('ChatView.vue: <template> not found')
const t = tpl[1]
const bubblesAt = t.indexOf('<!-- Message bubbles -->')
const loadingAt = t.indexOf('<!-- Loading -->')
if (bubblesAt < 0 || loadingAt < 0 || loadingAt < bubblesAt) {
  throw new Error('模板锚点缺失：Message bubbles / Loading（模板被改动后必须来改本脚本，不许静默跳过）')
}
const rowStart = t.indexOf('<div', bubblesAt)
const snippet = t.slice(rowStart, loadingAt)

// 切出来的东西必须真是"消息行"，否则后面的断言全是假绿
for (const must of ['class="message-row"', 'v-for="(msg, index) in messages"',
                    'msg-sources', 'msg-debug']) {
  if (!snippet.includes(must)) throw new Error('切出的模板片段缺关键标记: ' + must)
}
if (!snippet.trimEnd().endsWith('</div>')) throw new Error('切出的模板片段不是完整元素')

// ---------- 2) 编译成 render ----------
// ★ comments: false 不是美化 —— 新提示块上面那段注释正好写着「未检索到相关资料」，
//   而"必须不出现"的断言会被【注释文本】命中，直接造成误红；反过来"必须出现"的
//   断言也可能被注释白送。生产构建同样丢注释，这里与生产对齐。
//   （第一次跑就是被这个骗了：8 条红里 6 条是注释干的，见 §39.29）
const { code } = compile(snippet, { mode: 'function', prefixIdentifiers: true, comments: false })
if (code.includes('with (')) throw new Error('still generated a with() statement')
const render = new Function('Vue', code)(Vue)
if (typeof render !== 'function') throw new Error('compile produced no render fn')

// ---------- 2.5) 真实的中文标签表 + routerLabel（从组件脚本里原样取，不手抄）----------
const labelsSrc = sfc.match(/const ROUTER_LABELS = \{[\s\S]*?\n\}/)
const fnSrc = sfc.match(/function routerLabel\(reason\) \{[\s\S]*?\n\}/)
if (!labelsSrc || !fnSrc) throw new Error('ROUTER_LABELS / routerLabel 取不到（组件改了就得来改本脚本）')
const routerLabel = new Function(`${labelsSrc[0]}\n${fnSrc[0]}\nreturn routerLabel`)()

// ---------- 3) 按真实形态构造数据 ----------
const noop = () => {}
const SRC = [
  { n: 1, filename: '奖学金办法.pdf', doc_title: '奖学金办法', document_id: 5,
    section_path: '第三条 > 测评指标', snippet: '基本分5分，实行减分制', score: 0.91 },
  { n: 2, filename: '奖学金办法.pdf', doc_title: '奖学金办法', document_id: 5,
    section_path: '第二条 > 评审程序', snippet: '学生工作部', score: 0.88 },
]
const RET_OK = {
  attempted: true, candidates: 8, reranked: true, degradedReason: '',
  rerankBackend: 'local',
  docRouter: { docs_kept: 3, docs_total: 5, reason: 'filtered', min_docs: 2 },
}
// ★ 补发帧的真实形状：映射里 markers 是 payload 缺失时补出来的默认值。
const RET_NONE_SENT = {
  attempted: false, candidates: 0, reranked: true, degradedReason: '',
  rerankBackend: '', docRouter: null,
}
const RET_TRIED_EMPTY = {
  attempted: true, candidates: 0, reranked: true, degradedReason: '',
  rerankBackend: 'local', docRouter: { docs_kept: 3, docs_total: 5, reason: 'filtered' },
}

function state(msg) {
  return {
    messages: [msg],
    renderMarkdown: (s) => `<p>${s}</p>`,
    onCiteClick: noop, toggleSources: noop, toggleDebug: noop, copyMessage: noop,
    openSources: {}, openDebug: {}, activeCite: null,
    routerLabel,
  }
}

// 场景：label / msg / 必须出现 / 必须不出现
const SCENARIOS = [
  {
    label: '①检索成功',
    msg: { role: 'assistant', content: '综测由两部分构成 [1][2]', streaming: false,
           sources: SRC, retrieval: RET_OK },
    must: ['来源 2 段', '检索详情', 'debug-toggle', '已重排', '后端 local', routerLabel('filtered'), '2 段进入答案'],
    mustNot: ['本条未检索资料', '未检索到相关资料'],
  },
  {
    label: '②检索了但没数据',
    msg: { role: 'assistant', content: '资料里没有写这条。', streaming: false,
           sources: [], retrieval: RET_TRIED_EMPTY },
    must: ['未检索到相关资料', '检索详情', 'debug-toggle', '0 段进入答案'],
    mustNot: ['本条未检索资料'],
  },
  {
    label: '③压根没检索',
    msg: { role: 'assistant', content: '你好，有什么可以帮你？', streaming: false,
           sources: [], retrieval: RET_NONE_SENT },
    // ★「未检索到相关资料」绝不能出现（链被顶掉就是这里红）+ 详情面板不能冒出来假报"已重排"
    must: ['本条未检索资料（判断为无需查库）'],
    mustNot: ['未检索到相关资料', 'debug-toggle', '已重排', '历史消息'],
  },
  {
    label: '④用户消息不该有来源',
    msg: { role: 'user', content: '那班长加分怎么算', streaming: false,
           sources: SRC, retrieval: RET_OK },
    must: [],
    mustNot: ['来源 2 段', 'debug-toggle', '未检索到相关资料', '本条未检索资料', '历史消息'],
  },
  {
    // 防御性场景：数据层理论上不该让用户消息带上 retrieval（§39.26 修过一个真 bug：
    // 用户消息"继承"了来源/检索状态）。角色守卫就是为这一类兜底的 ——
    // 没有这条场景，把 msg.role === 'assistant' 删掉也不会有任何断言变红（守卫不可证伪）。
    label: '④b用户消息不该说"没检索"',
    msg: { role: 'user', content: '那班长加分怎么算', streaming: false,
           sources: [], retrieval: RET_NONE_SENT },
    must: [],
    mustNot: ['本条未检索资料', '未检索到相关资料', 'debug-toggle', '历史消息'],
  },
  {
    label: '④c用户消息不该说"没数据"',
    msg: { role: 'user', content: '那班长加分怎么算', streaming: false,
           sources: [], retrieval: RET_TRIED_EMPTY },
    must: [],
    mustNot: ['未检索到相关资料', '本条未检索资料', 'debug-toggle', '历史消息'],
  },
  {
    label: '④d用户消息不该显示历史提示',
    msg: { role: 'user', content: '那班长加分怎么算', streaming: false,
           sources: [SRC[0]], retrieval: null },
    must: [],
    mustNot: ['历史消息', '来源 1 段', 'debug-toggle', '本条未检索资料', '未检索到相关资料'],
  },
  {
    label: '⑤历史消息（有来源无详情）',
    msg: { role: 'assistant', content: '旧会话里的回答 [1]', streaming: false,
           sources: [SRC[0]], retrieval: null },
    must: ['来源 1 段', '历史消息：仅保存了来源，未保存检索详情'],
    // ★ 用面板类名而不是「检索详情」四个字：⑤ 的提示文案本身就含这四个字，会子串撞车。
    mustNot: ['debug-toggle', '本条未检索资料'],
  },
  {
    label: '⑥出错（后端不补发帧）',
    msg: { role: 'assistant', content: '发生错误:Connection error', streaming: false,
           sources: [], retrieval: null },
    must: ['发生错误'],
    mustNot: ['本条未检索资料', '未检索到相关资料'],
  },
]

console.log('='.repeat(96))
console.log('B 项行为验证：没检索 / 没数据 必须可分辨（真实模板 → 编译 → SSR → 断言渲染结果）')
console.log('='.repeat(96))
console.log(`  模板来源: ${VUE_PATH}`)
console.log(`  片段: ${Buffer.byteLength(snippet)} B（.message-row，原样切出）`)

// ★ 全局防空：证明"模板注释真的没进输出"。没有这一条，上面所有"不出现"的断言
//   都可能只是被注释骗红/骗绿（第一次跑正是如此）。
{
  const probe = await renderToString(createSSRApp({ render, setup: () => state(SCENARIOS[0].msg) }))
  const leaked = ['顶掉', 'Message bubbles', '★'].filter((s) => probe.includes(s))
  check('全局', '★防空过：模板注释未进入渲染输出', leaked.length === 0,
        leaked.length ? `泄漏: ${leaked.join(', ')}` : '')
}

for (const sc of SCENARIOS) {
  const app = createSSRApp({ render, setup: () => state(sc.msg) })
  const html = await renderToString(app)

  // ★ 防空：先证明这一场真的渲染出来了，否则"必须不出现"的断言全是白送
  const rendered = html.includes('class="msg-bubble"') && html.includes(sc.msg.content)
  check(sc.label, '★防空过：这一场真的渲染出了正文', rendered,
        rendered ? `${Buffer.byteLength(html)} B` : 'HTML 为空或正文没渲染出来')

  for (const s of sc.must) {
    check(sc.label, `出现「${s}」`, html.includes(s), '')
  }
  for (const s of sc.mustNot) {
    check(sc.label, `不出现「${s}」`, !html.includes(s), '')
  }
}

const nOk = results.filter(([, , ok]) => ok).length
console.log()
console.log(`合计 ${nOk}/${results.length} 通过${nOk === results.length ? '' : '   <-- 有失败'}`)
console.log('='.repeat(96))
process.exit(nOk === results.length ? 0 : 1)