/**
 * [零 API] 「知识库搜索」开关**跨刷新记住**的行为验证。
 *
 * 为什么不能只查"代码里有没有 localStorage"：
 *   持久化最容易错的不是"没写"，而是**读回来的语义**——
 *     · 没存过 ⇒ 必须回到默认【开】（不能因为读不到就变成关）
 *     · 存了脏值 / 读抛异常 / 写抛异常 ⇒ 必须**照常工作**，绝不能把提问挡住
 *     · 写进去的值必须能让下一次读回同一个状态（否则"跨刷新记住"是假的）
 *   这些都只有**真跑一遍**才知道。
 *
 * 做法：从 ChatView.vue 里把 FORCE_SEARCH_KEY / FORCE_SEARCH_DEFAULT /
 *   loadForceSearch / toggleForceSearch **原样抽出来**，用一个假 localStorage 执行。
 *   ★ 抽的是真代码（不是重写的副本）；抽不到就当场报错，绝不静默跳过。
 *   ★ 浏览器里真正的 localStorage 由用户在页面上刷新验证（这一步机器替不了）。
 *
 * ★★ 设计纪律（第十二轮踩出来的）：**装置自己的前置条件也是断言，也只许以 FAIL 行报出。**
 *   第一版把"抽到代码块"写成 `throw` ⇒ 反向对照喂进坏副本时脚本**崩了**：
 *   exit 1 却**一条 FAIL 都没有** —— 按项目判据那是**假红**（它只证明"装置坏了"，
 *   没证明"断言能抓住这个坏法"）。现在：锚点缺失 / 关键语句缺失 / 语法失效，
 *   全部先记 FAIL 行再收尾退出。
 *
 * 用法：node _kbsearch_persist_test.mjs
 * 反向对照：KBSEARCH_VUE=<坏副本.vue> node _kbsearch_persist_test.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const VUE_PATH = process.env.KBSEARCH_VUE || join(HERE, 'src', 'views', 'ChatView.vue')
const sfc = readFileSync(VUE_PATH, 'utf8')

const results = []
function check(name, ok, detail = '') {
  results.push([name, !!ok])
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${name.padEnd(52)} ${detail}`)
}
function finish() {
  const nOk = results.filter(([, ok]) => ok).length
  console.log()
  console.log(`合计 ${nOk}/${results.length} 通过${nOk === results.length ? '' : '   <-- 有失败'}`)
  console.log('='.repeat(96))
  process.exit(nOk === results.length ? 0 : 1)
}

console.log('='.repeat(96))
console.log('「知识库搜索」开关：跨刷新记住（真代码 + 假 localStorage，零 API）')
console.log('='.repeat(96))
console.log(`  模板来源: ${VUE_PATH}`)

// ---------- 1) 原样抽出真代码（抽不到 = FAIL 行，不是崩溃） ----------
const startAt = sfc.indexOf('const FORCE_SEARCH_KEY')
const toggleAt = sfc.indexOf('function toggleForceSearch()')
check('★防空过：定位到持久化代码块的两个锚点', startAt >= 0 && toggleAt > startAt,
      `KEY=${startAt} toggle=${toggleAt}`)
if (startAt < 0 || toggleAt <= startAt) finish()

const tailEnd = sfc.slice(toggleAt).indexOf('\n}')
check('★防空过：toggleForceSearch 函数体有结尾', tailEnd >= 0, `offset=${tailEnd}`)
if (tailEnd < 0) finish()

const code = sfc.slice(startAt, toggleAt + tailEnd + 2)
let extractOk = true
for (const must of ['localStorage.getItem', 'localStorage.setItem', 'FORCE_SEARCH_DEFAULT']) {
  const okm = code.includes(must)
  check(`★防空过：抽出的代码块含 ${must}`, okm, okm ? '' : '缺 —— 这段已被改坏')
  extractOk = extractOk && okm
}
if (!extractOk) finish()

// ---------- 2) 编译（语法坏 = FAIL 行，不是崩溃） ----------
let makeModule = null
try {
  makeModule = new Function('localStorage', 'ref',
    `${code}\nreturn { loadForceSearch, toggleForceSearch, forceSearch, FORCE_SEARCH_KEY, FORCE_SEARCH_DEFAULT }`)
  check('★防空过：抽出的代码块能编译（语法有效）', true, `${Buffer.byteLength(code)} B`)
} catch (e) {
  check('★防空过：抽出的代码块能编译（语法有效）', false, 'SyntaxError: ' + (e && e.message))
}
if (!makeModule) finish()

// ---------- 3) 假 localStorage + ref 桩 ----------
function makeStorage(initial = {}, opts = {}) {
  const map = new Map(Object.entries(initial))
  return {
    map,
    getItem(k) { if (opts.throwOnGet) throw new Error('SecurityError'); return map.has(k) ? map.get(k) : null },
    setItem(k, v) { if (opts.throwOnSet) throw new Error('QuotaExceeded'); map.set(k, String(v)) },
  }
}
const ref = (v) => ({ value: v })
const load = (storage) => makeModule(storage, ref)

// ---------- 4) 用例（每条都包 try/catch：抛异常记为 FAIL 行，而不是让脚本带栈退出） ----------
function run(name, fn) {
  try {
    const r = fn()
    check(name, r === true || r === undefined, r === true ? '' : String(r))
  } catch (e) {
    check(name, false, '抛异常: ' + (e && e.message))
  }
}

const probe = load(makeStorage())
check('★防空过：抽出的代码真的执行起来了（默认值可读）',
      probe.FORCE_SEARCH_DEFAULT === true, `FORCE_SEARCH_DEFAULT=${probe.FORCE_SEARCH_DEFAULT}`)
check('键名钉死为 kb_force_search（改键名 = 静默丢掉所有用户的选择）',
      probe.FORCE_SEARCH_KEY === 'kb_force_search', String(probe.FORCE_SEARCH_KEY))

run('①没存过 ⇒ 默认【开】（不能读不到就变关）',
    () => load(makeStorage()).forceSearch.value !== true ? '实际不是 true' : true)

run('②存过 "0" ⇒ 关（跨刷新记住"关"）',
    () => load(makeStorage({ kb_force_search: '0' })).forceSearch.value !== false ? '实际不是 false' : true)

run('③存过 "1" ⇒ 开',
    () => load(makeStorage({ kb_force_search: '1' })).forceSearch.value !== true ? '实际不是 true' : true)

run('④脏值 "yes" ⇒ 退回默认【开】（不把脏数据当用户意图）',
    () => load(makeStorage({ kb_force_search: 'yes' })).forceSearch.value !== true ? '实际不是 true' : true)

run('⑤读存储抛异常（隐私模式）⇒ 退回默认【开】且不崩',
    () => load(makeStorage({}, { throwOnGet: true })).forceSearch.value !== true ? '实际不是 true' : true)

run('⑥toggle 开→关：状态翻转**且真的写进存储**"0"',
    () => {
      const s = makeStorage()
      const m = load(s)
      m.toggleForceSearch()
      if (m.forceSearch.value !== false) return '状态没翻转'
      return s.map.get('kb_force_search') === '0' ? true : `存储里是 ${JSON.stringify(s.map.get('kb_force_search'))}`
    })

run('⑦toggle 关→开：状态翻转**且真的写进存储**"1"',
    () => {
      const s = makeStorage({ kb_force_search: '0' })
      const m = load(s)
      if (m.forceSearch.value !== false) return '初始没读成关'
      m.toggleForceSearch()
      if (m.forceSearch.value !== true) return '状态没翻转'
      return s.map.get('kb_force_search') === '1' ? true : `存储里是 ${JSON.stringify(s.map.get('kb_force_search'))}`
    })

run('⑧★ 写存储抛异常 ⇒ 不崩，且本次会话仍然生效（不挡住提问）',
    () => {
      const m = load(makeStorage({}, { throwOnSet: true }))
      m.toggleForceSearch()
      return m.forceSearch.value !== false ? '状态没变' : true
    })

run('⑨★ 写进去的值能被下一次读回同一个状态（"跨刷新记住"的闭环）',
    () => {
      const s = makeStorage()
      load(s).toggleForceSearch()          // 开 → 关，写 "0"
      return load(s).forceSearch.value !== false ? '重新读回来的不是关' : true
    })

finish()