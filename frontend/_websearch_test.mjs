/**
 * [零 LLM / 零网络] 「联网搜索」开关 + 网页参考渲染：**行为**检查。
 *
 * 为什么还需要这个装置（跨文件接线已经由 _debug_panel_test.mjs 判了）：
 *   接线只证明"线上有这根线"，不证明"线那头的行为对"。这里真跑三件事：
 *     ① 开关的持久化语义（真代码 + 假 localStorage）：默认关、只认 1/0、脏值回退、
 *        读/写抛异常都不许崩（隐私模式下列表页都打不开是最糟的失败形态）；
 *     ② 渲染的三条诚实规则：**不是学校文件**要说出来、搜不到要说出"没搜到 + 原因"、
 *        网页条目**不参与引用编号**（编号只属于学校文件，混进来等于给网文发依据）；
 *     ③ 视觉上与「来源 N 段」分开（两套 CSS 类各自独立）。
 *
 * 反向对照（故意改坏 ⇒ 本装置必须具名变红）在 _gate_mutate.mjs 的 Z1/Z2 两条里。
 *
 * 用法：node _websearch_test.mjs        （可用 WEBSEARCH_VUE 指向坏副本）
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const VUE = process.env.WEBSEARCH_VUE || join(HERE, 'src', 'views', 'ChatView.vue')
const sfc = readFileSync(VUE, 'utf8')

const results = []
function check(name, ok, detail = '') {
  results.push([name, !!ok])
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${name.padEnd(56)} ${detail}`)
}
function finish() {
  const bad = results.filter(([, ok]) => !ok).length
  console.log('-'.repeat(100))
  console.log(`合计 ${results.length - bad}/${results.length} 通过${bad ? '   <-- 有失败' : '  ✅ 全部通过'}`)
  console.log('='.repeat(100))
  process.exit(bad ? 1 : 0)
}

console.log('='.repeat(100))
console.log('「联网搜索」开关与网页参考渲染：行为检查（真代码 + 假 localStorage，零 API）')
console.log('='.repeat(100))
console.log(`  模板来源: ${VUE}`)

// ---------- 1) 原样抽出开关的真代码 ----------
const startAt = sfc.indexOf('const WEB_SEARCH_KEY')
const toggleAt = sfc.indexOf('function toggleWebSearch()')
check('★防空过：定位到联网开关代码块的两个锚点', startAt >= 0 && toggleAt > startAt,
      `KEY=${startAt} toggle=${toggleAt}`)
if (startAt < 0 || toggleAt <= startAt) finish()

const tailEnd = sfc.slice(toggleAt).indexOf('\n}')
check('★防空过：toggleWebSearch 函数体有结尾', tailEnd > 0, `offset=${tailEnd}`)
const code = sfc.slice(startAt, toggleAt + tailEnd + 2)

check('★防空过：抽出的代码块含 localStorage.getItem', /localStorage\.getItem/.test(code))
check('★防空过：抽出的代码块含 localStorage.setItem', /localStorage\.setItem/.test(code))
check('★防空过：抽出的代码块含 WEB_SEARCH_DEFAULT', /WEB_SEARCH_DEFAULT/.test(code))

let makeModule = null
try {
  // ★ 与 _kbsearch_persist_test.mjs 同一手法：把真代码包成模块，注入假的 localStorage 与 ref。
  makeModule = new Function('localStorage', 'ref',
    code + '\nreturn { loadWebSearch, toggleWebSearch, webSearch };')
  check('★防空过：抽出的代码块能编译（语法有效）', true, `${Buffer.byteLength(code)} B`)
} catch (e) {
  check('★防空过：抽出的代码块能编译（语法有效）', false, 'SyntaxError: ' + (e && e.message))
  finish()
}

function makeHost(store, opts = {}) {
  const ls = {
    getItem(k) {
      if (opts.getThrows) { const e = new Error('The operation is insecure.'); e.name = 'SecurityError'; throw e }
      return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null
    },
    setItem(k, v) {
      if (opts.setThrows) { const e = new Error('QuotaExceededError'); e.name = 'QuotaExceededError'; throw e }
      store[k] = String(v)
    },
  }
  // ★ 构造期间也可能抛：代码块里有 `const webSearch = ref(loadWebSearch())`，
  //   它在**模块构造时**就会跑一次读（Z2 那条突变正是打在这里）。
  //   按本项目纪律：装置**不许把异常漏出去** —— 一旦漏出去，反向对照只会看到
  //   "装置崩了/未真红"，那条具名断言根本没机会说话（§39.32.3 的老坑）。
  try {
    return { mod: makeModule(ls, (v) => ({ value: v })) }
  } catch (e) {
    return { err: e }
  }
}
// 任何"跑一下真代码"的动作都经它：抛异常 ⇒ 变成可断言的 FAIL 理由，绝不让装置崩。
function safe(fn) {
  try { return { v: fn() } } catch (e) { return { err: e } }
}
const why = (r) => (r.err ? `抛异常: ${r.err.name || r.err.message}` : String(r.v))
// ★ 取用一律走它：构造期抛出的异常在这里**重新抛出**，这样 safe() 归因到真正的异常
//   （否则 `.mod` 是 undefined、调用它报成 TypeError —— 断言红了，但理由不准，
//    而"理由不准"正是下一次误判的种子）。
function host(store, opts = {}) {
  const h = makeHost(store, opts)
  if (h.err) throw h.err
  return h.mod
}

// ---------- 2) 持久化语义 ----------
console.log('-'.repeat(100))
const r1 = safe(() => host({}).loadWebSearch())
check('①没存过 ⇒ 默认【关】（不外呼、不把网页混进依据）', r1.v === false, why(r1))

const r2 = safe(() => host({ kb_web_search: '1' }).loadWebSearch())
check('②存过 1 ⇒ 开（跨刷新记住）', r2.v === true, why(r2))

const r3 = safe(() => host({ kb_web_search: '0' }).loadWebSearch())
check('③存过 0 ⇒ 关', r3.v === false, why(r3))

const r4 = safe(() => host({ kb_web_search: 'yes' }).loadWebSearch())
check('④脏值 "yes" ⇒ 退回默认【关】（不把脏数据当用户意图）', r4.v === false, why(r4))

const r5 = safe(() => host({}, { getThrows: true }).loadWebSearch())
check('⑤读存储抛异常 ⇒ 退回默认【关】且不崩（隐私模式也能用）', r5.v === false, why(r5))
// ↑ 这条是「默认是关 + 读有保护」的正向证明；Z1（默认改开）/Z2（去掉 try）
//   两条突变都必须让它具名变红 —— 红了才算证明，红在别的理由上不算。

const store1 = {}
const h1 = safe(() => host(store1))
const t1 = safe(() => h1.v.toggleWebSearch())
check('⑥toggle 开 ⇒ 状态翻转且真的写进存储 "1"',
      !h1.err && !t1.err && h1.v.webSearch.value === true && store1.kb_web_search === '1',
      `${why(t1)} 存储=${store1.kb_web_search}`)
const b1 = safe(() => h1.v.loadWebSearch())
check('⑥b 写进去的值能被读回同一个状态（"跨刷新记住"的闭环）', b1.v === true, why(b1))
safe(() => h1.v.toggleWebSearch())
check('⑦toggle 再点 ⇒ 关，且存储是 "0"',
      !h1.err && h1.v.webSearch.value === false && store1.kb_web_search === '0',
      `值=${h1.err ? 'n/a' : h1.v.webSearch.value} 存储=${store1.kb_web_search}`)

const host2 = safe(() => host({}, { setThrows: true }))
const t2 = safe(() => host2.v.toggleWebSearch())
check('⑧写存储抛异常 ⇒ 不崩，且本次会话仍然生效（不挡住提问）',
      !host2.err && !t2.err && host2.v.webSearch.value === true,
      `${why(t2)} 值=${host2.err ? 'n/a' : host2.v.webSearch.value}`)

check('★ 开关默认值就是字面量 false（不是靠"没写"来碰巧为假）',
      /const WEB_SEARCH_DEFAULT = false/.test(code), '')

// ---------- 3) 渲染的三条诚实规则 ----------
console.log('-'.repeat(100))
check('⑨网页参考块条件是 msg.web && msg.web.attempted（没搜过就整块不出现）',
      /v-if="!msg\.streaming && msg\.role === 'assistant' && msg\.web && msg\.web\.attempted"/.test(sfc), '')

const webListBlock = (sfc.match(/<ol v-if="msg\.web\.count > 0" class="web-list">([\s\S]*?)<\/ol>/) || [])[1] || ''
check('★防空过：取到网页列表模板块', webListBlock.length > 100, `${Buffer.byteLength(webListBlock)} B`)
check('⑩每条网页是可点链接（href/target/rel 三件套齐全）',
      /:href="w\.url"/.test(webListBlock) && /target="_blank"/.test(webListBlock)
      && /rel="noopener noreferrer"/.test(webListBlock), '')
check('⑪标题旁标出来源站点（让用户知道这是哪个网站，而不是"某处"）',
      /class="web-site">\{\{ w\.site \}\}/.test(webListBlock), '')
check('⑫★ 网页条目**不参与引用编号**（没有 src-n / 没有 [n] 角标）',
      !/src-n/.test(webListBlock) && !/\[\s*\{\{/.test(webListBlock), '')
check('⑬搜索失败时写明"没搜到 + 原因"（绝不静默留白）',
      /本次联网搜索没搜到可用结果/.test(sfc) && /msg\.web\.reason_text/.test(sfc), '')
check('⑭★ 明确写出「不是学校文件」（不能让人把网页当校规）',
      /不是学校文件/.test(sfc), '')
check('⑮与「来源 N 段」用两套独立 CSS 类（视觉可区分）',
      /\.msg-web \{/.test(sfc) && /\.web-title \{/.test(sfc)
      && /class="msg-sources"/.test(sfc) && /class="msg-web"/.test(sfc),
      // 学校来源那套样式在 src/style.css 里（.msg-sources），网页这套在 ChatView 内 —— 
      // 这里只判「两类名各自独立存在」，不要求它们写在同一个文件里。
      '')
check('⑯检索详情面板里有「联网」这一行（这次到底搜没搜、走哪个源、多久、为什么没结果）',
      /<span class="debug-k">联网<\/span>/.test(sfc) && /msg\.web\.provider_label/.test(sfc)
      && /msg\.web\.elapsed_ms/.test(sfc), '')

finish()