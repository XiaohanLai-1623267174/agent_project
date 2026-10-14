/**
 * [零 API] 引用角标与来源的**对齐**验证：正文写的 [n] 到底有没有依据。
 *
 * 背景（第十二轮）：待办里记着"`msg77` 正文 8 个角标、只持久化 5 条来源 ⇒ 点 [7] 找不到依据"。
 *   先查库（`kb_bench/audit_citation_alignment.py`，只读）发现**当前库 0/46 条越界**，
 *   那条记档无法复现 —— 但**风险是真的**：提示词只是"叮嘱"模型不许编造编号（rag_template 规则 14），
 *   没有任何机制保证。旧实现把所有 [n] 一律渲染成和真引用**一模一样**的可点角标
 *   （还带 tooltip「查看来源 n」），点下去毫无反应 ⇒ **UI 承诺了它没有的依据**。
 *
 * 本装置只测一件事：**渲染出来的角标有没有如实表达"这条编号有没有来源"**。
 *   跑的是从 ChatView.vue 里**原样抽出**的 linkifyCitations（不是重写的副本）。
 *
 * 用法：node _cite_align_test.mjs
 * 反向对照：CITE_VUE=<坏副本.vue> node _cite_align_test.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { marked } from 'marked'
import { fixtureFile } from './_device_paths.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const VUE_PATH = process.env.CITE_VUE || join(HERE, 'src', 'views', 'ChatView.vue')
const sfc = readFileSync(VUE_PATH, 'utf8')

const results = []
function check(name, ok, detail = '') {
  results.push([name, !!ok])
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${name.padEnd(56)} ${detail}`)
}
function finish() {
  const nOk = results.filter(([, ok]) => ok).length
  console.log()
  console.log(`合计 ${nOk}/${results.length} 通过${nOk === results.length ? '' : '   <-- 有失败'}`)
  console.log('='.repeat(96))
  process.exit(nOk === results.length ? 0 : 1)
}

console.log('='.repeat(96))
console.log('引用角标 ↔ 来源 对齐（真代码 linkifyCitations，零 API）')
console.log('='.repeat(96))
console.log(`  模板来源: ${VUE_PATH}`)

// ---------- 1) 原样抽出真函数（抽不到 = FAIL 行，不是崩溃；见 §39.32.3） ----------
// ★ 连 renderMarkdown 一起抽：否则"来源有没有从模板一路透传到 linkifyCitations"
//   只能靠查字符串，容易被蒙混（字符串在、参数没接上照样能绿）。
const atLink = sfc.indexOf('function linkifyCitations')
const atRender = sfc.indexOf('function renderMarkdown')
check('★防空过：定位到 linkifyCitations / renderMarkdown 两个锚点',
      atLink >= 0 && atRender > atLink, `link=${atLink} render=${atRender}`)
if (atLink < 0 || atRender <= atLink) finish()
const endRender = sfc.indexOf('\n}', atRender) + 2
check('★防空过：renderMarkdown 函数体有结尾', endRender > 2, `offset=${endRender}`)
if (endRender <= 2) finish()

const code = sfc.slice(atLink, endRender)
check('★防空过：抽出的代码块含两个函数',
      code.includes('function linkifyCitations') && code.includes('function renderMarkdown'),
      `${Buffer.byteLength(code)} B`)
let linkify = null
let render = null
try {
  const factory = new Function('marked', code + '\nreturn { linkifyCitations, renderMarkdown }')
  check('★防空过：抽出的代码块能编译（语法有效）', true, `${Buffer.byteLength(code)} B`)
  const mod = factory(marked)
  linkify = mod.linkifyCitations
  render = mod.renderMarkdown
  check('★防空过：两个函数都能调用', typeof linkify === 'function' && typeof render === 'function')
} catch (e) {
  check('★防空过：抽出的代码块能编译（语法有效）', false, 'SyntaxError: ' + (e && e.message))
}
if (!linkify || !render) finish()

// marked 配置与组件保持一致（breaks/gfm）—— 这里只关心角标，配置差异不影响正则行为。
marked.setOptions({ breaks: true, gfm: true })
const html = (text, sources) => render(text, sources)     // ★ 走真 renderMarkdown（覆盖透传）
const chips = (s) => (s.match(/class="cite-ref"/g) || []).length
const ghosts = (s) => (s.match(/class="cite-ref is-ghost"/g) || []).length

const SRC5 = [1, 2, 3, 4, 5].map(n => ({ n, filename: `f${n}.docx` }))

// ---------- 2) 主路径：全部编号都有依据 ----------
{
  const out = html('基本条件见 [2]，具体见 [1][5]。', SRC5)
  check('①有效编号 ⇒ 3 个真角标、0 个幽灵', chips(out) === 3 && ghosts(out) === 0,
        `chips=${chips(out)} ghosts=${ghosts(out)}`)
  check('①真角标仍带 data-cite 与「查看来源」提示',
        out.includes('data-cite="2"') && out.includes('title="查看来源 2"'))
}

// ---------- 3) 核心：越界编号必须【看得出来】没有依据 ----------
{
  const out = html('据说可以加分 [7]，另外还有 [1]。', SRC5)
  check('②越界 [7] ⇒ 恰好 1 个幽灵角标', ghosts(out) === 1, `ghosts=${ghosts(out)}`)
  check('②越界 [7] ⇒ 不再带「查看来源 7」这种承诺',
        !out.includes('title="查看来源 7"'))
  check('②越界 [7] ⇒ 提示语明说没有对应来源',
        /本条没有来源 7/.test(out), '')
  check('②同一句里的有效 [1] 不受影响（仍为真角标）',
        chips(out) === 1 && out.includes('title="查看来源 1"'), `chips=${chips(out)}`)
  check('②★ 回答原文一个字都没改（[7] 的字符仍然在）', out.includes('>7</sup>'))
}

// ---------- 4) 一条来源都没有 ----------
{
  const outA = html('直接回答里出现了 [1] 和 [3]。', [])
  check('③无任何来源 ⇒ 全部编号都是幽灵', chips(outA) === 0 && ghosts(outA) === 2,
        `chips=${chips(outA)} ghosts=${ghosts(outA)}`)
  const outB = html('直接回答里出现了 [1]。', undefined)
  check('③sources 为 undefined（未检索/直答）⇒ 也是幽灵',
        chips(outB) === 0 && ghosts(outB) === 1, `chips=${chips(outB)} ghosts=${ghosts(outB)}`)
  const outC = html('直接回答里出现了 [1]。', [{ filename: '没有 n 字段' }, { n: 0 }, { n: null }])
  check('③来源里没有合法 n ⇒ 不把任何编号当真依据',
        chips(outC) === 0 && ghosts(outC) === 1, `chips=${chips(outC)} ghosts=${ghosts(outC)}`)
}

// ---------- 5) 既有刻意行为必须继续成立 ----------
{
  const mk = ['正文 [1]。', '', '```js', 'const a = b[0] + c[1]', '```', '', '行内 `x[1]` 也不能动。'].join('\n')
  const out = html(mk, SRC5)
  const segs = out.split(/(<pre[\s\S]*?<\/pre>|<code[\s\S]*?<\/code>)/g).filter((_, i) => i % 2 === 1)
  check('④代码段里 0 个角标（含 0 个幽灵）',
        segs.every(s => !s.includes('cite-ref')), `segs=${segs.length}`)
  const preText = (segs.find(s => s.startsWith('<pre')) || '').replace(/<[^>]+>/g, '')
  check('④代码块文本内容未被改动（仍含 b[0] / c[1]）',
        preText.includes('b[0]') && preText.includes('c[1]'))
  check('④行内代码 x[1] 原样保留', out.includes('x[1]'))
  check('④正文里那个真 [1] 仍是真角标', chips(out) === 1, `chips=${chips(out)}`)
}

// ---------- 6) 边界 ----------
{
  const out = html('两位数编号 [12] 也应识别；[1] 有效。', SRC5)
  check('⑤[12] 越界 ⇒ 幽灵；[1] 有效 ⇒ 真角标',
        chips(out) === 1 && ghosts(out) === 1, `chips=${chips(out)} ghosts=${ghosts(out)}`)
  const out2 = html('三位数 [123] 不该被当成引用编号（正则本就是 1–2 位）', SRC5)
  check('⑤[123] 不匹配（保持既有 1–2 位约定）', chips(out2) === 0 && ghosts(out2) === 0)
  check('⑤没有方括号时原样返回（早退分支仍在）',
        linkify('<p>没有编号</p>', SRC5) === '<p>没有编号</p>')
  check('⑤html 为空/null 时不炸', linkify('', SRC5) === '' && linkify(null, SRC5) === null)
}

// ---------- 7) 模板确实把来源传进去了（否则整套守卫等于没接上） ----------
// ★ 注意：这条是【接线】检查，与上面 ⑦ 的行为检查互补 ——
//   行为检查证明"函数接住了参数"，接线检查证明"模板把参数递了出去"。两者缺一不可。
{
  const calls = (sfc.match(/renderMarkdown\(msg\.content[^)]*\)/g) || [])
  check('★ 防空过：模板调用点找得到', calls.length === 2, `calls=${calls.length}`)
  check('★ 两个 v-html 调用点都传了 msg.sources',
        calls.length === 2 && calls.every(c => /renderMarkdown\(msg\.content,\s*msg\.sources\)/.test(c)),
        calls.join(' | '))
  check('★ renderMarkdown 把来源透传给 linkifyCitations（接线文本）',
        /linkifyCitations\(marked\.parse\(text\), sources\)/.test(sfc))
  check('★ onCiteClick 的"幻号不给反馈"守卫仍在（双保险）',
        /msg\.sources\.some\(s => s\.n === n\)/.test(sfc))
}

// ---------- 8) 真 renderMarkdown 的行为：来源一路透传到头 ----------
{
  const out = render('越界 [7] 与有效 [2] 混在一句里。', SRC5)
  check('⑦renderMarkdown 端到端：[7] 幽灵、[2] 真角标',
        ghosts(out) === 1 && chips(out) === 1, `chips=${chips(out)} ghosts=${ghosts(out)}`)
  check('⑦renderMarkdown 端到端：sources 省略时不把任何编号当真依据',
        ghosts(render('出现 [1]。')) === 1, '')
  check('⑦renderMarkdown 端到端：空文本返回空串', render('', SRC5) === '')
}

// ---------- 9) 真实抓包：对齐不是"合成用例才成立" ----------
// 用【真后端响应体】过【真前端 SSE 解析器】，拿真实正文 + 真实来源去渲染。
// ★ 防空：必须先证明这份抓包里**确实有角标**、来源也**确实非空** ——
//   否则"0 个幽灵"会变成"没有角标所以没幽灵"的空过。
{
  let ok = true
  let body = ''
  let expect = null
  try {
    body = readFileSync(fixtureFile('_sse_body.txt'), 'utf8')
    expect = JSON.parse(readFileSync(fixtureFile('_sse_expect.json'), 'utf8'))
  } catch (e) {
    check('★防空过：能读到抓包夹具（真实抓包，或随仓库发布的合成夹具）', false, String(e && e.message))
    ok = false
  }
  if (ok) {
    const { createSseParser } = await import('./src/api/request.js')
    let text = ''
    let payload = null
    const parser = createSseParser(({ event, data }) => {
      if (event === 'sources') payload = JSON.parse(data)
      else text += data
    })
    parser.push(body)
    parser.flush()
    const nums = [...new Set((text.match(/\[(\d{1,2})\]/g) || []).map(s => Number(s.slice(1, -1))))].sort((a, b) => a - b)
    const srcN = (payload && payload.sources) || []
    check('★防空过：真实抓包里确实有角标', nums.length >= 1, `编号=${JSON.stringify(nums)}`)
    check('★防空过：真实抓包里来源非空', srcN.length >= 1, `n=${srcN.length}`)
    check('★防空过：抓包正文与后端权威解析逐字一致', text === expect.text,
          `${text.length} vs ${expect.text.length}`)
    const out = render(text, srcN)          // 走真 renderMarkdown，端到端到渲染这一步
    const g = ghosts(out)
    const c = chips(out)
    check('⑥真实抓包：越界编号 = 0（每个角标都有依据）', g === 0,
          `幽灵=${g} 真角标=${c} 编号=${JSON.stringify(nums)} 来源=${JSON.stringify(srcN.map(s => s.n))}`)
    check('⑥真实抓包：真角标数 = 正文里不同编号的个数', c === nums.length, `${c} vs ${nums.length}`)
  }
}

finish()