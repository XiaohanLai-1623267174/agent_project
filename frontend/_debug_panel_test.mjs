/**
 * [零 API] 「检索详情」面板的【前后端字段对齐】检查。
 *
 * 为什么需要它：面板读的是 msg.retrieval.* 字段，而这些字段靠 ChatView.vue 里
 * 一小段手写映射（payload.doc_router -> docRouter 这类 camelCase 转换）搭桥。
 * 这种桥最容易出的错是【名字对不上】：模板写 msg.retrieval.doc_router，
 * 映射里却是 docRouter —— 界面上不报错，只静默显示空白/undefined。
 * 本脚本把三处真源对齐：
 *   ① 后端 langchain_project.py 真正写进 trace 的键与 reason 取值
 *   ② ChatView.vue 的映射表（retrieval = { ... }）
 *   ③ 模板里真正读的 msg.retrieval.<key>
 * 任一处对不上就红。
 *
 * 另有两组（十一会话后加入）：
 *   ④「没检索 / 没数据」可分辨：空状态与新提示块的条件、角色守卫、补发状态帧
 *   ⑤「知识库搜索」按钮的**贯通**：schemas → /chat → rag_response → route → request.js → ChatView 按钮，
 *     哪一处断了界面都**不报错**，只是"点了没反应"（行为本身由 test_force_rag_route.py 真调 route() 验）
 *
 * 用法：node _debug_panel_test.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { parse } from '@vue/compiler-dom'
import { backendFile, backendHome } from './_device_paths.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
// 路径可被环境变量覆盖 —— 不是为了生产，而是为了让【反向对照】能指向坏副本。
// 第一版没这个开关，我用临时目录做对照时脚本直接崩了：失败理由错了 == 一次假绿。
const PY = backendFile('langchain_project.py', process.env.DEBUG_PANEL_PY)
const VUE = process.env.DEBUG_PANEL_VUE || join(HERE, 'src', 'views', 'ChatView.vue')
// 让"跑不起来"这件事一眼可诊断：路径解析器会在 kb_bench（开发布局）与 server（本仓库布局）之间选
console.log('  后端源码目录: ' + backendHome())

const py = readFileSync(PY, 'utf8')
const vue = readFileSync(VUE, 'utf8')

// ★ 联网搜索的原因词汇表（§39.38）：唯一真源是 websearch.py 的 REASON_TEXT。
//   为什么要单独读它：下面那条「后端每个 reason 都有中文标签」原来是把
//   langchain_project.py 里**所有** reason 字面量都要求出现在前端的 ROUTER_LABELS 里 ——
//   而 ROUTER_LABELS 只解释【doc_router 的原因】。加上联网后，`_web_block()` 里出现
//   http_error 这套**另一套词汇**，这条检查就误报了（把两套互不相干的词汇表耦合在一起）。
//   现在：doc_router 那套仍对 ROUTER_LABELS；联网那套改对【后端降级 legend + REASON_TEXT】。
const WS = backendFile('websearch.py', process.env.DEBUG_PANEL_WS)
const ws = readFileSync(WS, 'utf8')
const wsReasonBlock = (ws.match(/REASON_TEXT\s*=\s*\{([\s\S]*?)\n\}/) || [])[1] || ''
const wsReasons = new Set([...wsReasonBlock.matchAll(/"([a-z_]+)":/g)].map(m => m[1]))

const results = []
function check(name, ok, detail = '') {
  results.push([name, !!ok])
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${name.padEnd(52)} ${detail}`)
}

const traceKeys = new Set()
for (const m of py.matchAll(/trace\["(\w+)"\]\s*(?:=|\[)/g)) traceKeys.add(m[1])
const reasons = new Set()
for (const m of py.matchAll(/reason\s*=\s*"([a-z_]+)"/g)) reasons.add(m[1])
for (const m of py.matchAll(/"reason":\s*"([a-z_]+)"/g)) reasons.add(m[1])
for (const m of py.matchAll(/"reason":\s*\(\s*"([a-z_]+)"\s+if\s+\w+\s+else\s+"([a-z_]+)"/g)) {
  reasons.add(m[1]); reasons.add(m[2])
}
const mapBlock = vue.match(/messages\.value\[aiIndex\]\.retrieval\s*=\s*\{([\s\S]*?)\n\s*\}/)
const mapping = new Map()
if (mapBlock) {
  // 注意：映射值可能带强制转换前缀（attempted: !!payload.attempted）。
  // 第一版正则只认「冒号后紧跟 payload.」=> 静默漏掉 attempted 这个真实键，
  // 是上面的防空断言把它报出来的。故允许冒号后有任何非逗号内容。
  for (const m of mapBlock[1].matchAll(/(\w+):\s*[^,\n]*?payload\.(\w+)/g)) mapping.set(m[1], m[2])
}
const labelsBlock = vue.match(/const ROUTER_LABELS\s*=\s*\{([\s\S]*?)\n\}/)
const labelKeys = new Set()
if (labelsBlock) {
  for (const m of labelsBlock[1].matchAll(/^\s*([a-z_]+):/gm)) labelKeys.add(m[1])
}
const reads = new Set()
for (const m of vue.matchAll(/msg\.retrieval\.([A-Za-z_]\w*)/g)) reads.add(m[1])

console.log('='.repeat(96))
console.log('检索详情面板：前后端字段对齐检查（零 API）')
console.log('='.repeat(96))
console.log(`  后端 trace 键(${traceKeys.size}): ${[...traceKeys].sort().join(', ')}`)
console.log(`  后端 reason(${reasons.size}):   ${[...reasons].sort().join(', ')}`)
console.log(`  前端映射键(${mapping.size}):    ${[...mapping.keys()].sort().join(', ')}`)
console.log(`  模板读取(${reads.size}):        ${[...reads].sort().join(', ')}`)
console.log('-'.repeat(96))

check('★ 防空过：后端 reason >= 6 个', reasons.size >= 6, String(reasons.size))
check('★ 防空过：映射键 >= 6 个', mapping.size >= 6, String(mapping.size))
check('★ 防空过：模板读取 >= 5 个', reads.size >= 5, String(reads.size))
check('★ 防空过：ROUTER_LABELS 非空', labelKeys.size >= 6, String(labelKeys.size))

const missingInMap = [...reads].filter(k => !mapping.has(k)).sort()
check('★ 模板读的每个 msg.retrieval.<key> 都在映射表里', missingInMap.length === 0, missingInMap.join(', '))

const missingInBackend = [...mapping.entries()].filter(([, p]) => !traceKeys.has(p)).map(([k, p]) => `${k}->${p}`).sort()
check('★ 映射读的每个 payload.<key> 都是后端真写过的 trace 键', missingInBackend.length === 0, missingInBackend.join(', '))

// ★ 口径（§39.38）：只拿 doc_router 那套 reason 去对 ROUTER_LABELS；
//   联网那套由下面的「web reason 词汇表」专条管。
const routerReasons = new Set([...reasons].filter(r => !wsReasons.has(r)))
check('★ 防空过：doc_router reason >= 6 个', routerReasons.size >= 6, String(routerReasons.size))
const unlabeled = [...routerReasons].filter(r => !labelKeys.has(r)).sort()
check('★ 后端每个 reason 都有中文标签', unlabeled.length === 0, unlabeled.join(', '))
const extraLabels = [...labelKeys].filter(r => !routerReasons.has(r)).sort()
check('标签表没有多余/拼错的键', extraLabels.length === 0, extraLabels.join(', '))

for (const [k, label] of [['docRouter', '文档路由'], ['candidates', '召回'], ['reranked', '重排']]) {
  check(`模板真的渲染了「${label}」这一段`, reads.has(k), k)
}

// ★ 用 AST 里"哪个元素带哪个条件"来做判定 —— 不按源码行/文本行匹配。
//   §36.7.1 记过这条纪律：任何"有没有 / 覆盖没覆盖"的结论，文本匹配只能当线索。
const nodes = []
function walkChildren(children, parentEl = null) {
  if (!Array.isArray(children)) return
  for (let i = 0; i < children.length; i++) {
    const n = children[i]
    if (n && n.type === 1) {
      // parentEl：为了断言"按钮确实在 .input-wrapper 里"，而不是"看着在输入区"
      nodes.push({ el: n, siblings: children, idx: i, parentEl })
      walkChildren(n.children, n)
    }
  }
}
const tplMatch = vue.match(/<template>([\s\S]*)<\/template>/)
if (!tplMatch) throw new Error('ChatView.vue: <template> not found')
walkChildren(parse(tplMatch[1]).children)

const dirOf = (el, name) => (el.props || []).find((p) => p.type === 7 && p.name === name)
const condOf = (el, name) => {
  const d = dirOf(el, name)
  return d ? (d.exp ? d.exp.content : '') : null
}
const classOf = (el) => {
  const a = (el.props || []).find((p) => p.type === 6 && p.name === 'class')
  return a && a.value ? a.value.content : ''
}
const textOf = (el) => {
  let s = ''
  const rec = (n) => {
    if (!n) return
    if (Array.isArray(n)) return n.forEach(rec)
    if (n.type === 2) s += n.content
    if (n.children) rec(n.children)
  }
  rec(el.children)
  return s
}
const find = (pred) => nodes.find((n) => pred(n.el)) || null

// ★ 定位必须用【类名 + 指令】这种"元素自己身上的属性"，绝不能用"文本包含"：
//   textOf 会把子孙文本一起算进来 ⇒ 任何祖先都"包含"该串，find 返回的是最外层祖先
//   （第一版就是这么错的：空状态与新提示块都被解析成同一个 .msg-body，5 条断言连锁误红）。
//   这是本项目第 5 次"用文本匹配冒充语义判定"（前四次见 §36.7.1），已就地记档 §39.29。
const EL_PANEL = find((e) => classOf(e) === 'msg-sources' && condOf(e, 'if') !== null)
const EL_EMPTY = find((e) => classOf(e) === 'msg-sources is-empty' && condOf(e, 'else-if') !== null)
const EL_HINT = find((e) => classOf(e) === 'msg-sources is-empty' && condOf(e, 'if') !== null)
const EL_DEBUG = find((e) => classOf(e) === 'msg-debug')
const EL_NOTE = find((e) => classOf(e) === 'msg-debug-note')

console.log('-'.repeat(96))
console.log('B 项：「没检索 / 没数据」可分辨 —— 结构（AST）与前后端对齐')
console.log('-'.repeat(96))

// ★ 防空：五个元素必须【都定位到】，并且【互不相同】。
//   只查"非空"是不够的 —— 第一版正是两个不同的名字指向了同一个祖先元素，
//   "5/5 定位到"照样报 OK。所以这里额外要求 pairwise distinct（见 §39.29）。
const located = [EL_PANEL, EL_EMPTY, EL_HINT, EL_DEBUG, EL_NOTE]
check('★ 防空过：5 个消息区元素全部定位到', located.every(Boolean),
      ['panel', 'empty', 'hint', 'debug', 'note'].filter((_, i) => !located[i]).join(', ') || '5/5')
check('★ 防空过：这 5 个元素互不相同（防"都指向同一个祖先"）',
      new Set(located.filter(Boolean).map((n) => n.el)).size === located.filter(Boolean).length,
      `distinct=${new Set(located.filter(Boolean).map((n) => n.el)).size}`)

// 文案也各查一遍：元素定位对了，内容还得对
check('空状态文案仍是「未检索到相关资料」',
      !!EL_EMPTY && textOf(EL_EMPTY.el).includes('未检索到相关资料'), '')
check('新提示文案含「本条未检索资料」',
      !!EL_HINT && textOf(EL_HINT.el).includes('本条未检索资料'), '')

// 来源面板 / 空状态 / 详情面板 / 历史提示 + 新提示块：条件里都必须有角色守卫。
// 理由：这些块所在的 .msg-body 不在角色分支里（角色只用于选头像），
// 一旦某条用户消息被"继承"了来源/检索状态，界面就会显示荒谬的"这条提问的引用依据"。
const GUARDS = [['来源面板', EL_PANEL], ['空状态', EL_EMPTY], ['本条未检索提示', EL_HINT],
                ['检索详情面板', EL_DEBUG], ['历史提示', EL_NOTE]]
for (const [label, node] of GUARDS) {
  if (!node) { check(`${label}条件含角色守卫`, false, '元素定位失败'); continue }
  const c = condOf(node.el, 'if') ?? condOf(node.el, 'else-if') ?? condOf(node.el, 'else') ?? ''
  check(`${label}条件含 msg.role === 'assistant'`, c.includes(`msg.role === 'assistant'`), '')
}

// ★ 链完整性（§39.24.3 的坑）：空状态是 v-else-if，它前面【最近的一个元素】必须
//   就是来源面板这个 v-if —— 按 Vue 编译器自己的回看规则（跳过注释与纯空白）。
//   往链中间插一个元素会把它俩拆开；本条就是那次"空状态被顶掉"的机器判定。
function prevElement(node) {
  for (let i = node.idx - 1; i >= 0; i--) {
    const s = node.siblings[i]
    if (!s) continue
    if (s.type === 3) continue                                   // 注释：Vue 自己会跳过
    if (s.type === 2 && !s.content.trim()) continue               // 纯空白
    return s.type === 1 ? s : null
  }
  return null
}
if (EL_EMPTY && EL_PANEL) {
  check('空状态仍是「紧跟来源面板」的 v-else-if（链没被截断）',
        prevElement(EL_EMPTY) === EL_PANEL.el && condOf(EL_EMPTY.el, 'else-if') !== null, '')
} else check('空状态仍是「紧跟来源面板」的 v-else-if（链没被截断）', false, '元素定位失败')

if (EL_HINT) {
  const c = condOf(EL_HINT.el, 'if')
  check('新提示块是独立 v-if（不塞进上面那条链）', c !== null && condOf(EL_HINT.el, 'else-if') === null, '')
  check('新提示块条件是 attempted === false（精确判定，不用 !attempted）',
        (c || '').includes('msg.retrieval.attempted === false'), '')
}

if (EL_DEBUG) {
  const c = condOf(EL_DEBUG.el, 'if') || ''
  // 收紧的意义：补发帧的 reranked 默认是 true，面板会给"压根没检索"的消息报假"已重排"
  check('详情面板条件已收紧到 msg.retrieval.attempted', c.includes('msg.retrieval.attempted'), '')
}

// ---------- 后端补发帧 ----------
const FASTAPI = backendFile('fastapi_project.py', process.env.DEBUG_PANEL_FASTAPI)
const api = readFileSync(FASTAPI, 'utf8')
// ★ 帧是**多行**调用（`event="sources",` 后面还有单独一行 `)`）—— 正则要把两种写法都认下，
//   否则"防空过"会误报"没找到"，后面几条断言全部不再执行（第一版就踩了这个）。
const frameBlock = (api.match(/if\s+not\s+sent_sources[\s\S]{0,900}?event="sources",?\s*\n\s*\)/) || [])[0] || ''
// ★ attempted=False 现在住在 `_status_payload()` 里（§39.36 把它抽出来，好附带 direct_history_*）。
//   所以这条断言必须**两处一起看**：帧要真的调用它（接线），载荷里要真的写着 attempted=False（语义）。
const payloadFn = (api.match(/def _status_payload\([\s\S]{0,900}?\n    return out\n/) || [])[0] || ''
if (!frameBlock) {
  check('★ 防空过：在 fastapi_project.py 里找到「补发状态帧」那段', false, '没找到 —— 正则或代码变了')
} else {
  check('★ 防空过：在 fastapi_project.py 里找到「补发状态帧」那段', true,
        `${Buffer.byteLength(frameBlock)} B`)
  check('★ 防空过：取到 _status_payload 函数体（attempted=False 住在那里）', payloadFn.length > 0,
        `${Buffer.byteLength(payloadFn)} B`)
  check('补发帧带 attempted=False（经 _status_payload）', /"attempted":\s*False/.test(payloadFn), '')
  check('补发帧真的调用了 _status_payload（否则那层语义等于没接上）',
        /_status_payload\(trace\)/.test(frameBlock), '')
  // ★ 这一条是致命的：帧若不以 event="sources" 发出，前端会把它当【正文】拼进回答里
  check('补发帧以 event="sources" 发出（否则会被拼进正文）', /event="sources"/.test(frameBlock), '')
  const reason = ((payloadFn || frameBlock).match(/"reason":\s*"([a-z_]+)"/) || [])[1] || '(取不到)'
  // ★ 出错时不许补发：错误流同样没有来源帧，补发就等于把"崩了"说成"我判断不需要查库"
  check('补发帧有"出错时不发"的守卫（is_error）', /is_error/.test(frameBlock), '')
  check('前端真的消费了补发帧的键（attempted 在映射表里）', mapping.has('attempted'), '')
  console.log(`  （信息）补发帧 reason = ${reason}；前端只按 attempted 分派，不依赖 reason 文案`)
}

// ---------- 「知识库搜索」按钮：前后端贯通（6 个环节，断一处就是"点了没反应"） ----------
// 为什么必须查贯通：这条链断在任何一处，界面都**不报错** —— 只是静静走了 classify 的老路，
// 用户点了按钮、结果和没点一样（§39.29 记的就是这类"静默失效"）。
// ★ 这里用「按钮元素(AST) + 四处真源码的关键传参」判定**接线**；
//   行为本身（classify 到底有没有被调）由 kb_bench/test_force_rag_route.py 真调 route() 负责。
{
  const SCHEMAS = backendFile('schemas.py', process.env.DEBUG_PANEL_SCHEMAS)
  const schemas = readFileSync(SCHEMAS, 'utf8')
  const reqBlock = (schemas.match(/class ChatRequest\(BaseModel\):([\s\S]*?)\nclass /) || [])[1] || ''
  check('★ 防空过：取到 schemas.py 的 ChatRequest 类体', reqBlock.length > 0, `${Buffer.byteLength(reqBlock)} B`)
  check('ChatRequest 有 force_rag 且默认 False', /force_rag:\s*bool\s*=\s*False/.test(reqBlock), '')

  check('/chat 把 request.force_rag 透传给 rag_response', /force_rag=request\.force_rag/.test(api), '')
  check('rag_response 把它继续透传给 route',
        /route\(query,\s*user_id,\s*context,\s*trace,\s*force_rag=force_rag,\s*web=web_search\)/.test(api), '')
  // ★ 联网搜索（§39.38）：同一条链再走一遍。断了的表现与 force_rag 一模一样 ——
  //   界面不报错，只是"点了联网却什么都没搜"（§39.29 那类静默失效）。
  check('ChatRequest 有 web_search 且默认 False', /web_search:\s*bool\s*=\s*False/.test(reqBlock), '')
  check('/chat 把 request.web_search 透传给 rag_response', /web_search=request\.web_search/.test(api), '')
  check('rag_response 签名收 web_search', /web_search:\s*bool\s*=\s*False/.test(api), '')
  // ★ 直答分支也必须带 web：用户开了联网、这条被判为闲聊时，
  //   若状态帧不带 web，前端就"什么都不知道" ⇒ 又一次"点了没反应"。
  check('补发的状态帧（直答分支）也带 web',
        /def _status_payload\(trace\)[\s\S]{0,900}?out\["web"\]\s*=\s*trace\["web"\]/.test(api), '')

  // ★ 位置比较（不是"有没有这个词"）：force_rag 分支必须在 classify 之前 ——
  //   放到后面就变成"classify 说了算、按钮无效"，而源码里两句话都还在，grep 抓不到。
  const iForce = py.indexOf('if force_rag:')
  const iClassify = py.indexOf('if classify(question, context):')
  check('★ 防空过：langchain_project.py 两个分支锚点都在', iForce >= 0 && iClassify >= 0,
        `force=${iForce} classify=${iClassify}`)
  check('route() 的 force_rag 分支在 classify 之前（覆盖而非并存）',
        iForce >= 0 && iClassify >= 0 && iForce < iClassify, '')
  check('force_rag 分支确实返回 RAG 链', /build_rag_chain\(/.test(iForce >= 0 ? py.slice(iForce, iForce + 400) : ''), '')

  const REQ = process.env.DEBUG_PANEL_REQ || join(HERE, 'src', 'api', 'request.js')
  const req = readFileSync(REQ, 'utf8')
  check('request.js: streamChat 接受 forceRag 参数', /streamChat\([^)]*forceRag/.test(req), '')
  check('request.js: 请求体里带上 force_rag', /force_rag:\s*!{1,2}forceRag/.test(req), '')

  check('ChatView: 发送时把 forceSearch 取成 forceRag', /const forceRag = forceSearch\.value/.test(vue), '')
  // ★ 常驻开关（用户 2026-10-12 拍板：从"一次性"改成"常驻 + 默认开 + 跨刷新记住"）
  //   ⇒ 这两条是**反向断言**：把"发送后复位"加回来 = 回归；把默认值改回 false = 丢掉"默认 rag"。
  check('ChatView: 常驻开关——send() 里不复位', !/forceSearch\.value = false/.test(vue), '')
  check('ChatView: 开关默认是开（用户拍板"默认 rag"）',
        /const FORCE_SEARCH_DEFAULT = true/.test(vue), '')

  // ★ 跨刷新记住（用户同日追加要求）：读、写、回退三件事都要在，
  //   而"读回来的语义"（默认/脏值/异常）由 _kbsearch_persist_test.mjs 真跑一遍验证。
  const persistBlock = (vue.match(/const FORCE_SEARCH_KEY[\s\S]*?function toggleForceSearch\(\)[\s\S]*?\n\}/) || [])[0] || ''
  check('★ 防空过：取到开关的持久化代码块', persistBlock.length > 200, `${Buffer.byteLength(persistBlock)} B`)
  check('持久化键名是 kb_force_search（与 auth_token 同风格）',
        /const FORCE_SEARCH_KEY = 'kb_force_search'/.test(persistBlock), '')
  check('读：有 try/catch 回退（隐私模式/非浏览器下不许崩）',
        /catch/.test(persistBlock.split('function toggleForceSearch')[0]), '')
  check('读：只认 1/0，其余一律退回默认', /v === '1' \? true : v === '0' \? false : FORCE_SEARCH_DEFAULT/.test(persistBlock), '')
  check('写：toggle 里真的写进存储', /localStorage\.setItem\(FORCE_SEARCH_KEY/.test(persistBlock), '')
  check('写：写失败也不影响本次会话（有 try/catch 兜住）',
        /catch/.test(persistBlock.split('function toggleForceSearch')[1] || ''), '')
  check('ChatView: streamChat 调用处真的传了 forceRag',
        /streamChat\(q, activeConvId\.value, abortController\.signal, forceRag, useWeb\)/.test(vue), '')

  // ★ 联网搜索（§39.38）：前端这半边 —— 开关 → 请求体 → 帧 → 渲染，四处都要在。
  //   行为（开关持久化的语义、渲染出可点链接 / 没搜到文案）由 _websearch_test.mjs 真跑一遍；
  //   这里只判【跨文件接线】，因为这种线断掉时界面一律不报错，只是静默什么都没发生。
  check('request.js: streamChat 接受 webSearch 参数', /streamChat\([^)]*webSearch/.test(req), '')
  check('request.js: 请求体里带上 web_search', /web_search:\s*!{1,2}webSearch/.test(req), '')
  check('ChatView: 发送时把 webSearch 取成 useWeb', /const useWeb = webSearch\.value/.test(vue), '')
  check('ChatView: 帧里消费了 payload.web（两条路径都带它）',
        /messages\.value\[aiIndex\]\.web = payload\.web/.test(vue), '')
  check('ChatView: 联网默认【关】（不加成本、不让网页混入依据）',
        /const WEB_SEARCH_DEFAULT = false/.test(vue), '')
  check('ChatView: 联网开关跨刷新记住（键名 kb_web_search）',
        /const WEB_SEARCH_KEY = 'kb_web_search'/.test(vue), '')
  check('ChatView: 没搜到也要显示原因（诚实规则）',
        /本次联网搜索没搜到可用结果/.test(vue), '')
  check('ChatView: 网页条目不参与引用编号（不编号、不算依据）',
        !/web-title[^>]*>\s*\[/.test(vue) && /<ol class="web-list">/.test(vue)
        // 2026-10-16：网页参考改成可折叠（details）⇒ `<ol v-if="msg.web.count > 0" ...>` 那条守卫
        // 移到了外层 details 上。这里跟着改 —— 不改的话会因"标记变了"而**假红**（本装置当时确实红了）。
        && /<details v-if="msg\.web\.count > 0" class="web-fold">/.test(vue), '')
  // ★ 后端每个【联网】原因都要有 ①降级 legend 的中文解释 ②REASON_TEXT 的人话
  //   —— 前端「没搜到（原因）」直接显示 REASON_TEXT，缺一条就会显示英文原因码。
  const legendBlock = (api.match(/_DEGRADATION_LEGEND\s*=\s*\{([\s\S]*?)\n\}/) || [])[1] || ''
  const legendKeys = new Set([...legendBlock.matchAll(/"([a-z_]+)":/g)].map(m => m[1]))
  check('★ 防空过：取到 websearch 的 reason 词汇表（>= 6 个）', wsReasons.size >= 6, String(wsReasons.size))
  check('★ 防空过：取到降级 legend', legendKeys.size >= 6, String(legendKeys.size))
  const wsNoLegend = [...wsReasons].filter(r => !legendKeys.has(r)).sort()
  check('联网每个 reason 都有降级 legend 中文解释', wsNoLegend.length === 0, wsNoLegend.join(', '))
  const wsExtra = [...legendKeys].filter(r => /^(no_results|irrelevant|parse_empty|timeout|http_error|rate_limited|empty_query)$/.test(r) && !wsReasons.has(r)).sort()
  check('legend 里没有拼错的联网原因', wsExtra.length === 0, wsExtra.join(', '))

  // ★ 引用角标 ↔ 来源 对齐（第十二轮）：没有对应来源的编号必须【看得出来】没有依据。
  //   行为证据在 _cite_align_test.mjs（真跑 linkifyCitations/renderMarkdown）；
  //   这里只补它覆盖不到的【跨文件接线】：模板 → 函数 → CSS 样式这一条链。
  const cssPath = process.env.DEBUG_PANEL_CSS || join(HERE, 'src', 'style.css')
  const css = readFileSync(cssPath, 'utf8')
  const ghostBlock = (css.match(/\.markdown-body \.cite-ref\.is-ghost \{[\s\S]*?\n\}/) || [])[0] || ''
  check('★ 防空过：style.css 里取到幽灵角标样式块', ghostBlock.length > 40, `${Buffer.byteLength(ghostBlock)} B`)
  check('CSS: 幽灵角标去掉强调底色（不再像真依据）',
        /background:\s*transparent/.test(ghostBlock), '')
  check('CSS: 幽灵角标有虚线边框（肉眼可分辨）',
        /border:\s*1px\s+dashed/.test(ghostBlock), '')
  check('CSS: 幽灵角标不再是 pointer 光标（点了确实没反应，就不装作能点）',
        !/cursor:\s*pointer/.test(ghostBlock), '')
  const vmCalls = (vue.match(/renderMarkdown\(msg\.content[^)]*\)/g) || [])
  check('ChatView: 两个 v-html 调用点都把来源传给了 renderMarkdown',
        vmCalls.length === 2 && vmCalls.every((c) => /msg\.sources/.test(c)), vmCalls.join(' | '))
  check('ChatView: onCiteClick 仍拒绝给"幻号"任何反馈（双保险）',
        /msg\.sources\.some\(s => s\.n === n\)/.test(vue), '')

  const EL_KB = find((e) => classOf(e) === 'btn-kbsearch')
  check('★ 防空过：AST 里定位到「知识库搜索」按钮', !!EL_KB, EL_KB ? 'ok' : '没找到')
  if (EL_KB) {
    const bindOf = (argName) => {
      const p = (EL_KB.el.props || []).find(
        (x) => x.type === 7 && x.name === 'bind' && x.arg && x.arg.content === argName)
      return p && p.exp ? p.exp.content : null
    }
    const onOf = (evName) => {
      const p = (EL_KB.el.props || []).find(
        (x) => x.type === 7 && x.name === 'on' && x.arg && x.arg.content === evName)
      return p && p.exp ? p.exp.content : null
    }
    check('按钮在 .input-wrapper 内（不是"看着在输入区"）',
          !!EL_KB.parentEl && classOf(EL_KB.parentEl) === 'input-wrapper',
          EL_KB.parentEl ? classOf(EL_KB.parentEl) : '无父元素')
    check('按钮常显（没有 v-if / v-else-if）',
          condOf(EL_KB.el, 'if') === null && condOf(EL_KB.el, 'else-if') === null, '')
    check('按钮文案就是「知识库搜索」（用户点名）', textOf(EL_KB.el).includes('知识库搜索'), textOf(EL_KB.el).trim())
    check('按钮 @click 绑的是 toggleForceSearch', onOf('click') === 'toggleForceSearch', String(onOf('click')))
    check('按钮开启态由 forceSearch 驱动', (bindOf('class') || '').includes('forceSearch'), String(bindOf('class')))
    check('按钮 :disabled 绑的是 isStreaming', bindOf('disabled') === 'isStreaming', String(bindOf('disabled')))
  }
}

// ---------- 整份 SFC 的真编译 ----------
// 为什么不用 vite build：本沙箱下 vite 会在 Windows 上 spawn 子进程取真实路径，
// 而沙箱禁止以管道捕获子进程 stdio ⇒ `Error: spawn EPERM`（环境限制，非代码问题）。
// 这里改用【同进程内的真 Vue 编译器】做等价校验：模板 + <script setup> 都要编得过。
{
  let ok = true, detail = ''
  try {
    const sfc = await import('@vue/compiler-sfc')
    const parsed = sfc.parse(vue, { filename: 'ChatView.vue' })
    if (parsed.errors.length) { ok = false; detail = parsed.errors.map((e) => e.message).join('; ') }
    check('★ SFC 解析零错误', ok, detail)
    if (parsed.descriptor.template) {
      const out = sfc.compileTemplate({
        source: parsed.descriptor.template.content,
        filename: 'ChatView.vue', id: 'dbg',
        compilerOptions: { comments: false },
      })
      check('★ 整份模板编译零错误（vite build 的模板那一半）',
            out.errors.length === 0, out.errors.map((e) => e.message || e).join('; '))
    } else check('★ 整份模板编译零错误（vite build 的模板那一半）', false, '找不到 <template>')
    const script = sfc.compileScript(parsed.descriptor, { id: 'dbg' })
    check('★ <script setup> 编译零错误', typeof script.content === 'string' && script.content.length > 1000,
          `${script.content ? Buffer.byteLength(script.content) : 0} B`)
  } catch (e) {
    check('★ SFC 编译检查未抛异常', false, String(e && e.message))
  }
}

const nOk = results.filter(([, ok]) => ok).length
console.log()
console.log(`合计 ${nOk}/${results.length} 通过${nOk === results.length ? '' : '   <-- 有失败'}`)
console.log('='.repeat(96))
process.exit(nOk === results.length ? 0 : 1)
