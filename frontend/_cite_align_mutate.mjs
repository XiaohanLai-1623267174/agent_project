/**
 * [零 API] 「引用角标 ↔ 来源 对齐」的反向对照：证明 _cite_align_test.mjs 的断言真的有效。
 *
 * 纪律（沿用 §39.31.6 / §39.32.3）：
 *   ① 每份坏副本**只坏一处**，且锚点在源文件里必须命中**预期次数**（防"锚点漂移"）；
 *   ② 判据 = exit 1 **且** stdout 有 FAIL 行 **且** stderr 无堆栈（抛栈 = 装置崩了 = 假红）；
 *   ③ 每份都必须指名"该被哪条断言抓住"。
 *
 * 用法：node _cite_align_mutate.mjs
 */
import { readFileSync, writeFileSync, mkdirSync, rmSync, existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const SRC = join(HERE, 'src', 'views', 'ChatView.vue')
const TMP = join(HERE, '_cite_tmp')

const MUTATIONS = [
  {
    id: 'R', hits: 1,
    why: '越界编号照样渲染成真角标（等于 UI 承诺它没有的依据 —— 本轮要消灭的正是这个）',
    from: 'available.has(Number(num))',
    to: 'true',
    caughtBy: '②越界 [7] ⇒ 恰好 1 个幽灵角标 / ③无任何来源 ⇒ 全部编号都是幽灵',
  },
  {
    id: 'S', hits: 2,
    why: '模板不再把来源递给 renderMarkdown（守卫写了但没接上）',
    from: 'renderMarkdown(msg.content, msg.sources)',
    to: 'renderMarkdown(msg.content)',
    caughtBy: '★ 两个 v-html 调用点都传了 msg.sources',
  },
  {
    id: 'T', hits: 1,
    why: 'renderMarkdown 半路把 sources 丢掉（函数接住了参数但没往下传）',
    from: 'linkifyCitations(marked.parse(text), sources)',
    to: 'linkifyCitations(marked.parse(text))',
    caughtBy: '①有效编号 ⇒ 3 个真角标 / ⑦renderMarkdown 端到端',
  },
  {
    id: 'U', hits: 1,
    why: '"读不到来源就当全都有效"的宽容捷径（最诱人的兼容写法：不知道 ≠ 全都成立）',
    // ★ 第一版写成 `(Array.isArray(sources) ? sources : [1..12])`，结果**是无效突变**：
    //   后面紧跟的 `.map(s => Number(s && s.n))` 把裸数字全变成 NaN 又被 filter 掉，
    //   行为与原来**一模一样**（详见 §39.33 的"无效突变"陷阱与本次的行为指纹自检）。
    from: 'available.has(Number(num))',
    to: '(!available.size || available.has(Number(num)))',
    caughtBy: '③sources 为 undefined（未检索/直答）⇒ 也是幽灵',
  },
]

// ---------- 突变有效性自检（防空）----------
// 把"原始 vs 坏副本"里【抽出的那段真代码】跑同一组探针输入：
//   · 输出有差别 ⇒ 这份突变确实改变了行为，"未真红"才可能是断言太弱；
//   · 输出完全一致 ⇒ **先怀疑它是无效突变**（改了字面、没改行为）。
// ★ 注意：只覆盖【抽取范围内】的 JS（linkifyCitations + renderMarkdown）；
//   改模板/接线的那类突变本就不在这段范围里，所以这列只是**提示**，不是判据。
const PROBES = [
  ['<p>[1]</p>', [{ n: 1 }]],
  ['<p>[7]</p>', [{ n: 1 }, { n: 2 }, { n: 3 }, { n: 4 }, { n: 5 }]],
  ['<p>[1]</p>', []],
  ['<p>[1]</p>', undefined],
  ['<p>[12]</p>', [{ n: 1 }]],
  ['<p>[1]</p>', [{ filename: 'x' }, { n: 0 }, { n: null }]],
]
function fingerprint(text) {
  const at = text.indexOf('function linkifyCitations')
  const atR = text.indexOf('function renderMarkdown')
  if (at < 0 || atR <= at) return 'EXTRACT-FAILED'
  const end = text.indexOf('\n}', atR) + 2
  const stub = { parse: (t) => t }      // 只做指纹，mock 掉的 marked 用恒等解析即可
  const mod = new Function('marked', text.slice(at, end) + '\nreturn { linkifyCitations, renderMarkdown }')(stub)
  return JSON.stringify(PROBES.map(([h, s]) => mod.renderMarkdown(h, s)))
}

const TEST = join(HERE, '_cite_align_test.mjs')
const src = readFileSync(SRC, 'utf8')
const fp0 = fingerprint(src)
if (existsSync(TMP)) rmSync(TMP, { recursive: true, force: true })
mkdirSync(TMP, { recursive: true })

console.log('='.repeat(100))
console.log('「引用角标 ↔ 来源 对齐」反向对照（%d 份坏副本，每份只坏一处）', MUTATIONS.length)
console.log('='.repeat(100))

const rows = []
for (const m of MUTATIONS) {
  const hits = src.split(m.from).length - 1
  if (hits !== m.hits) {
    rows.push({ ...m, status: null, n: 0, threw: false, trueRed: false, failLines: [], anchorBad: `锚点命中 ${hits} 次（预期 ${m.hits}）` })
    continue
  }
  const bad = join(TMP, `bad_${m.id}.vue`)
  const mutated = src.split(m.from).join(m.to)
  writeFileSync(bad, mutated, 'utf8')
  const fpChanged = fingerprint(mutated) !== fp0
  const r = spawnSync(process.execPath, [TEST], {
    cwd: HERE, encoding: 'utf8', env: { ...process.env, CITE_VUE: bad },
  })
  const out = r.stdout || ''
  const err = r.stderr || ''
  const failLines = out.split('\n').map((s) => s.trim()).filter((s) => s.startsWith('FAIL'))
  const threw = /^\s+at |^Error|^[A-Za-z]*Error:/.test(err.split('\n').slice(0, 3).join('\n')) || /\bat file:/.test(err)
  rows.push({ ...m, status: r.status, n: failLines.length, threw, fpChanged, trueRed: r.status === 1 && failLines.length >= 1 && !threw, failLines })
}

console.log()
for (const r of rows) {
  if (r.anchorBad) {
    console.log(`  🔴 锚点漂移  [${r.id}] ${r.anchorBad}`)
    console.log(`        说明: ${r.why}`)
    continue
  }
  console.log(`  ${r.trueRed ? '真红 ✅' : '未真红 🔴'}  [${r.id}] ${r.why}`)
  console.log(`        该被抓住: ${r.caughtBy}`)
  console.log(`        exit=${r.status}  FAIL 行=${r.n}  抛栈=${r.threw}  行为指纹=${r.fpChanged ? '有变化' : '无差别'}`)
  if (!r.trueRed && !r.fpChanged) {
    console.log('        ⚠ 提示：这份突变在本装置抽取的 JS 范围内【行为无差别】。')
    console.log('          先怀疑它是【无效突变】（改了字面没改行为）或是改在模板/接线层，')
    console.log('          不要直接下"断言太弱"的结论。（§39.33 的 U 就是这么发现的。）')
  }
  for (const l of r.failLines.slice(0, 3)) console.log(`          · ${l}`)
}

const nRed = rows.filter((r) => r.trueRed).length
console.log()
console.log(`${nRed}/${rows.length} 真红 —— ${nRed === rows.length ? '断言有效性已证明' : '有突变没被抓住，断言太弱'}`)
console.log('='.repeat(100))
rmSync(TMP, { recursive: true, force: true })
process.exit(nRed === rows.length ? 0 : 1)