/**
 * 对齐门（_debug_panel_test.mjs）「知识库搜索贯通」那一组的**反向对照**。
 *
 * 只做**接线级**突变（真源码里删掉/改坏某一段传参），因为那一组负责的就是"接线有没有断"。
 * 「分支变成死代码」这类**行为级**坏法不归它管 —— 由 kb_bench/test_force_rag_route.py
 * 真调 route() 抓（两份装置分工不同，不能互相代替）。
 *
 * 判定（§39.28 升级判据）：真红 = exit 1 且 stdout 有 FAIL 行 且 无抛栈。
 *
 * 用法：node _gate_mutate.mjs
 */
import { readFileSync, writeFileSync, mkdirSync, rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { backendHome } from './_device_paths.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const KB = backendHome()
const TMP = join(HERE, '_gate_tmp')
const GATE = join(HERE, '_debug_panel_test.mjs')

const MUTATIONS = [
  {
    id: 'J', env: 'DEBUG_PANEL_FASTAPI', src: join(KB, 'fastapi_project.py'),
    why: '/chat 忘了把 request.force_rag 传下去（按钮变成纯装饰）',
    // ★ 锚点随 §39.38 更新：这个调用现在多传一个 web_search，旧锚点（只到 force_rag)）命中 0 次。
    from: ',\n                                                 force_rag=request.force_rag,\n                                                 web_search=request.web_search)',
    to: ')',
    caughtBy: '/chat 把 request.force_rag 透传给 rag_response',
  },
  {
    id: 'J2', env: 'DEBUG_PANEL_FASTAPI', src: join(KB, 'fastapi_project.py'),
    why: '★ 联网版：/chat 忘了把 request.web_search 传下去（点了联网却什么都没搜）',
    from: ',\n                                                 force_rag=request.force_rag,\n                                                 web_search=request.web_search)',
    to: ',\n                                                 force_rag=request.force_rag)',
    caughtBy: '/chat 把 request.web_search 透传给 rag_response',
  },
  {
    id: 'K', env: 'DEBUG_PANEL_REQ', src: join(HERE, 'src', 'api', 'request.js'),
    why: '请求体里没带 force_rag（后端永远收到 false）',
    // ★ 锚点随 §39.38 更新：请求体改成多行并新增 web_search，旧的单行锚点命中 0 次。
    from: ',\n                           force_rag: !!forceRag, web_search: !!webSearch',
    to: '',
    caughtBy: 'request.js: 请求体里带上 force_rag',
  },
  {
    id: 'K2', env: 'DEBUG_PANEL_REQ', src: join(HERE, 'src', 'api', 'request.js'),
    why: '★ 联网版：请求体没带 web_search（开关点了，后端永远收到 false）',
    from: ', web_search: !!webSearch', to: '',
    caughtBy: 'request.js: 请求体里带上 web_search',
  },
  {
    id: 'X', env: 'DEBUG_PANEL_VUE', src: join(HERE, 'src', 'views', 'ChatView.vue'),
    why: '★ 联网版：帧里的 payload.web 不再被消费（搜了也不显示，等于没搜）',
    from: 'messages.value[aiIndex].web = payload.web || null', to: 'void 0',
    caughtBy: 'ChatView: 帧里消费了 payload.web',
  },
  {
    id: 'Y', env: 'DEBUG_PANEL_FASTAPI', src: join(KB, 'fastapi_project.py'),
    why: '★ 联网版：直答分支补发的状态帧不再带 web（开了联网、走了闲聊 ⇒ 前端一无所知）',
    from: '    if isinstance(trace, dict) and isinstance(trace.get("web"), dict):\n        out["web"] = trace["web"]\n',
    to: '',
    caughtBy: '补发的状态帧（直答分支）也带 web',
  },
  {
    id: 'L', env: 'DEBUG_PANEL_SCHEMAS', src: join(KB, 'schemas.py'),
    why: 'ChatRequest 丢掉 force_rag 字段（FastAPI 会静默忽略多传的键）',
    from: '    force_rag: bool = False\n', to: '',
    caughtBy: 'ChatRequest 有 force_rag 且默认 False',
  },
  {
    id: 'M', env: 'DEBUG_PANEL_VUE', src: join(HERE, 'src', 'views', 'ChatView.vue'),
    why: '把"发送后复位"加回来（常驻开关退回一次性，用户已明确否掉的那种）',
    from: 'const forceRag = forceSearch.value',
    to: 'const forceRag = forceSearch.value\n  forceSearch.value = false',
    caughtBy: 'ChatView: 常驻开关——send() 里不复位',
  },
  {
    id: 'N', env: 'DEBUG_PANEL_VUE', src: join(HERE, 'src', 'views', 'ChatView.vue'),
    why: '默认值改回 false（丢掉用户拍板的"默认 rag"）',
    from: 'const FORCE_SEARCH_DEFAULT = true', to: 'const FORCE_SEARCH_DEFAULT = false',
    caughtBy: 'ChatView: 开关默认是开（用户拍板"默认 rag"）',
  },
  {
    id: 'V', env: 'DEBUG_PANEL_VUE', src: join(HERE, 'src', 'views', 'ChatView.vue'), hits: 2,
    why: '模板不再把来源传给 renderMarkdown（幽灵角标的接线断了）',
    from: '(msg.content, msg.sources)"', to: '(msg.content)"',
    caughtBy: 'ChatView: 两个 v-html 调用点都把来源传给了 renderMarkdown',
  },
  {
    id: 'W', env: 'DEBUG_PANEL_CSS', src: join(HERE, 'src', 'style.css'),
    why: '幽灵角标又变成有强调底色的样子（等于把"没有依据"伪装成"有依据"）',
    // ★ 锚点必须连选择器一起写：style.css 里另有一处（L194 的按钮）同样是
    //   `background: transparent;` + `color: var(--text-muted);`，只写这两行会命中 2 次。
    from: '.cite-ref.is-ghost {\n  background: transparent;\n  color: var(--text-muted);',
    to: '.cite-ref.is-ghost {\n  background: var(--accent-dim);\n  color: var(--accent);',
    caughtBy: 'CSS: 幽灵角标去掉强调底色（不再像真依据）',
  },
  {
    id: 'Z1', gate: '_websearch_test.mjs', env: 'WEBSEARCH_VUE',
    src: join(HERE, 'src', 'views', 'ChatView.vue'),
    why: '★ 联网开关默认改成【开】（每条提问都外呼、把网页内容混进"依据"里）',
    from: 'const WEB_SEARCH_DEFAULT = false', to: 'const WEB_SEARCH_DEFAULT = true',
    caughtBy: '①没存过 ⇒ 默认【关】',
  },
  {
    id: 'Z2', gate: '_websearch_test.mjs', env: 'WEBSEARCH_VUE',
    src: join(HERE, 'src', 'views', 'ChatView.vue'),
    why: '★ 联网版：去掉读存储的 try/catch（隐私模式下整页崩，连提问都做不了）',
    // 与 _kbsearch_mutate.mjs 的 Q 同一条纪律：坏副本**必须仍可编译**，
    // 否则前置检查先红 ⇒ 那条具名断言根本没机会跑 ⇒ "红得不对"。
    from: 'try {\n    const v = localStorage.getItem(WEB_SEARCH_KEY)',
    to: 'const v = localStorage.getItem(WEB_SEARCH_KEY)\n  try {\n    void 0',
    caughtBy: '读存储抛异常 ⇒ 退回默认【关】且不崩',
  },
]

mkdirSync(TMP, { recursive: true })
const rows = []

for (const m of MUTATIONS) {
  const base = readFileSync(m.src, 'utf8')
  const want = m.hits || 1
  const hits = base.split(m.from).length - 1
  // ★ 锚点漂移**不许抛栈**：抛栈会让整轮对照崩掉、只剩"未真红"的假象（§39.32.3）。
  //   这里改成一行显式报告 —— 既不会静默放过，也不会让装置自己变成故障源。
  if (hits !== want) {
    rows.push({ ...m, anchorBad: `锚点命中 ${hits} 次（预期 ${want}）`, trueRed: false, failLines: [], threw: false, status: null })
    continue
  }
  const ext = m.src.slice(m.src.lastIndexOf('.'))
  const bad = join(TMP, `${m.id}${ext}`)
  const mutated = base.split(m.from).join(m.to)
  writeFileSync(bad, mutated, 'utf8')
  if (mutated === base) {
    rows.push({ ...m, anchorBad: '替换后文件没有变化（等于无效突变）', trueRed: false, failLines: [], threw: false, status: null })
    continue
  }

  const r = spawnSync(process.execPath, [m.gate ? join(HERE, m.gate) : GATE], {
    cwd: HERE, encoding: 'utf8', env: { ...process.env, [m.env]: bad },
  })
  const out = r.stdout || ''
  const err = r.stderr || ''
  const failLines = out.split('\n').map((s) => s.trim()).filter((s) => s.startsWith('FAIL'))
  const threw = /\bat file:|\bat [A-Za-z]:\\/.test(err) || /Error:/.test(err.split('\n')[0] || '')
  const trueRed = r.status === 1 && failLines.length >= 1 && !threw
  // ★ 具名命中（§39.38 补，与 _kbsearch_mutate.mjs 同一判据）：光"红了"不算数，
  //   必须红在 caughtBy 指定的那条断言上 —— 前置检查先红、锚点漂移都算"红得不对"。
  const namedHit = failLines.some((l) => l.includes(m.caughtBy))
  rows.push({ ...m, status: r.status, failLines, threw, trueRed, namedHit })
}

console.log('='.repeat(100))
console.log('对齐门「知识库搜索贯通 + 引用角标接线」反向对照（' + MUTATIONS.length + ' 份接线级坏副本）')
console.log('='.repeat(100))
for (const r of rows) {
  if (r.anchorBad) {
    console.log(`  锚点漂移 XX  [${r.id}] ${r.anchorBad}`)
    console.log(`        说明: ${r.why}  —— 源码改了就来更新本脚本的锚点`)
    continue
  }
  console.log(`  ${r.trueRed && r.namedHit ? '真红 OK ' : r.trueRed ? '红得不对 XX' : '未真红 XX'}  [${r.id}] ${r.why}`)
  console.log(`        该被抓住: ${r.caughtBy}    exit=${r.status} FAIL=${r.failLines.length} 抛栈=${r.threw} 具名命中=${r.namedHit ? '是' : '否'}`)
  for (const ln of r.failLines) console.log(`          - ${ln}`)
}
rmSync(TMP, { recursive: true, force: true })
const allRed = rows.every((r) => r.trueRed && r.namedHit)
const nRed = rows.filter((r) => r.trueRed).length
const nNamed = rows.filter((r) => r.namedHit).length
console.log(`\n${nRed}/${rows.length} 真红；${nNamed}/${rows.length} 具名命中`
  + `${allRed ? ' —— 断言有效性已证明' : ' —— 有突变没被具名断言抓住，断言太弱（红了也要红对理由）'}`)
console.log('='.repeat(100))
process.exit(allRed ? 0 : 1)