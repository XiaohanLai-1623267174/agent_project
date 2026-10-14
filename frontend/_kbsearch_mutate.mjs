/**
 * 「知识库搜索」按钮 SSR 测试的**反向对照**：生成 3 份"坏副本"，
 * 每份只坏一处，且每处都必须被**指定装置**里**指定的一条断言**抓住
 * （render 组跑 _kbsearch_test.mjs，persist 组跑 _kbsearch_persist_test.mjs）。
 *
 * 判定（沿 §39.28 升级后的判据）：**真红 = exit 1 且 stdout 有 FAIL 行 且 没有抛栈**。
 * 只 exit 1（脚本自己崩了）不算真红 —— 那证明不了断言有效。
 *
 * 用法：node _kbsearch_mutate.mjs
 */
import { readFileSync, writeFileSync, mkdirSync, rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { spawnSync } from 'node:child_process'

const HERE = dirname(fileURLToPath(import.meta.url))
const SRC = join(HERE, 'src', 'views', 'ChatView.vue')
const TMP = join(HERE, '_kb_tmp')

const MUTATIONS = [
  {
    id: 'G', test: 'render',
    why: '开启态不高亮（用户看不出当前是"一律先查"）',
    from: ':class="{ on: forceSearch }"',
    to: '',
    caughtBy: '①开 / 按钮内含「btn-kbsearch on」',
    want: '按钮内含「btn-kbsearch on」',
  },
  {
    id: 'H', test: 'render',
    why: '流式中仍可点（会并发发出第二条）',
    // ★ 锚点必须带上这一行前缀（§39.38）：加入「联网搜索」按钮后，
    //   它也带着同样的 `:disabled="isStreaming"` + `:aria-pressed` 两行 ⇒
    //   旧锚点命中 2 次，装置会如实报「锚点漂移」而不是去猜改哪一个。
    from: 'class="btn-kbsearch"\n            :class="{ on: forceSearch }"\n            :disabled="isStreaming"',
    to: 'class="btn-kbsearch"\n            :class="{ on: forceSearch }"',
    caughtBy: '③流式中 / 按钮内含「disabled」',
    want: '按钮内含「disabled」',
  },
  {
    id: 'I', test: 'render',
    why: '按钮被改名（用户点名的名字丢了）',
    from: '<span>知识库搜索</span>',
    to: '<span>强制查资料</span>',
    caughtBy: '①开 / 按钮内含「知识库搜索」',
    want: '按钮内含「知识库搜索」',
  },
  {
    id: 'N', test: 'render',
    why: '默认值被改回 false（丢掉用户拍板的"默认 rag"）',
    from: 'const FORCE_SEARCH_DEFAULT = true',
    to: 'const FORCE_SEARCH_DEFAULT = false',
    caughtBy: '④默认态 / ★ 开关默认是【开】= 默认走 RAG（用户拍板）',
    want: '开关默认是【开】',
  },
  // ↓ 持久化组（跑的是 _kbsearch_persist_test.mjs）
  {
    id: 'O', test: 'persist',
    why: 'toggle 不再写存储（"跨刷新记住"失效，只能记住到刷新为止）',
    // ★ 旧写法把 `localStorage.setItem(` 整段删掉，结果**前置检查**先红（"抽出的代码块含
    //   localStorage.setItem"），那条真正的行为断言（⑥/⑦ 真的写进存储）根本没跑 ——
    //   这就是"红得不对"：红了，但红在错误的理由上（§39.32/§39.33 记过的坑）。
    //   现在改成**写错键名**：`setItem` 仍在 ⇒ 前置检查全过 ⇒ 只有行为断言会红。
    from: 'localStorage.setItem(FORCE_SEARCH_KEY, forceSearch.value',
    to: "localStorage.setItem('kb_force_search_typo', forceSearch.value",
    caughtBy: '⑥/⑦ toggle …真的写进存储',
    want: '真的写进存储',
  },
  {
    id: 'P', test: 'persist',
    why: '没存过时不再回退默认（读不到就变关 = 刷新后静默关掉查库）',
    from: ": v === '0' ? false : FORCE_SEARCH_DEFAULT", to: ": v === '0' ? false : false",
    caughtBy: '①没存过 ⇒ 默认【开】',
    want: '①没存过 ⇒ 默认【开】',
  },
  {
    id: 'Q', test: 'persist',
    why: '去掉读的 try/catch（隐私模式下整页崩，连提问都做不了）',
    // ★ 这条突变的 to **必须让坏副本仍然可编译**（本项目的老规矩：坏副本编译不过，
    //   前置检查「抽出的代码块能编译」会先红，那条具名断言根本没机会跑 —— 等于没测到）。
    //   旧写法把 `try {` 直接换成 `{`，于是 `catch` 变成孤儿 ⇒ SyntaxError ⇒ 装置报的
    //   「抽出的代码块能编译」而不是「⑤读存储抛异常 ⇒ 退回默认且不崩」。
    //   现在改成：把读取**挪到 try 之外**（语义就是"这条读没有保护了"），语法仍然合法。
    from: 'try {\n    const v = localStorage.getItem(FORCE_SEARCH_KEY)',
    to: 'const v = localStorage.getItem(FORCE_SEARCH_KEY)\n  try {\n    void 0',
    caughtBy: '⑤读存储抛异常 ⇒ 退回默认【开】且不崩',
    want: '读存储抛异常',
  },
]

const TESTS = {
  render: { file: '_kbsearch_test.mjs', label: '按钮 SSR' },
  persist: { file: '_kbsearch_persist_test.mjs', label: '跨刷新记住' },
}

const base = readFileSync(SRC, 'utf8')
mkdirSync(TMP, { recursive: true })
const rows = []

for (const m of MUTATIONS) {
  const hits = base.split(m.from).length - 1
  // ★ 锚点漂移**不许抛栈**：抛栈会让整轮对照崩掉，只剩"未真红"的假象（§39.32.3 / §39.33）。
  //   改成一行显式报告：既不静默放过，也不让装置自己变成故障源。
  if (hits !== 1) {
    rows.push({ ...m, anchorBad: `锚点命中 ${hits} 次（应为 1）`, trueRed: false, namedHit: false, n: 0, threw: false, status: null, failLines: [] })
    continue
  }
  const bad = join(TMP, `${m.id}.vue`)
  const mutated = base.split(m.from).join(m.to)
  writeFileSync(bad, mutated, 'utf8')
  if (mutated === base) {
    rows.push({ ...m, anchorBad: '替换后没有变化（无效突变）', trueRed: false, namedHit: false, n: 0, threw: false, status: null, failLines: [] })
    continue
  }

  const r = spawnSync(process.execPath, [join(HERE, TESTS[m.test].file)], {
    cwd: HERE, encoding: 'utf8', env: { ...process.env, KBSEARCH_VUE: bad },
  })
  const out = r.stdout || ''
  const err = r.stderr || ''
  const failLines = out.split('\n').map((s) => s.trim()).filter((s) => s.startsWith('FAIL'))
  const threw = /^\s+at |^Error|^[A-Za-z]*Error:/.test(err.split('\n').slice(0, 3).join('\n')) || /\bat file:/.test(err)
  const trueRed = r.status === 1 && failLines.length >= 1 && !threw
  // ★ 具名命中（§39.38 补）：光"红了"不算数 —— 必须红在**该红的那条断言**上。
  //   今天已经踩过两次"红得不对"：坏副本编译不过 ⇒ 前置检查先红；
  //   锚点命中 2 次 ⇒ 装置报漂移。那两种情况都 exit=1，但**什么都没被证明**。
  //   want 是每条突变预期的 FAIL 行片段；命中不了就按未通过计。
  const namedHit = !!m.want && failLines.some((l) => l.includes(m.want))
  rows.push({ ...m, status: r.status, n: failLines.length, threw, trueRed, namedHit, failLines })
}

console.log('='.repeat(100))
console.log('「知识库搜索」按钮：反向对照（' + MUTATIONS.length + ' 份坏副本，每份只坏一处）')
console.log('='.repeat(100))
for (const r of rows) {
  if (r.anchorBad) {
    console.log(`  🔴 锚点漂移  [${r.id}/${TESTS[r.test].label}] ${r.anchorBad}`)
    console.log(`        说明: ${r.why}  —— 模板改了就来更新本脚本的锚点`)
    continue
  }
  console.log(`  ${r.trueRed && r.namedHit ? '真红 ✅' : r.trueRed ? '红得不对 🔴' : '未真红 🔴'}  [${r.id}/${TESTS[r.test].label}] ${r.why}`)
  console.log(`        该被抓住: ${r.caughtBy}`)
  console.log(`        exit=${r.status}  FAIL 行=${r.n}  抛栈=${r.threw}  具名命中=${r.namedHit ? '是' : '否'}`)
  for (const ln of r.failLines) console.log(`          · ${ln}`)
}
rmSync(TMP, { recursive: true, force: true })

const allRed = rows.every((r) => r.trueRed && r.namedHit)
const nRed = rows.filter((r) => r.trueRed).length
const nNamed = rows.filter((r) => r.namedHit).length
console.log()
console.log(`${nRed}/${rows.length} 真红；${nNamed}/${rows.length} 具名命中`
  + `${allRed ? ' —— 断言有效性已证明' : ' —— 有突变没被具名断言抓住，断言太弱（红了也要红对理由）'}`)
console.log('='.repeat(100))
process.exit(allRed ? 0 : 1)