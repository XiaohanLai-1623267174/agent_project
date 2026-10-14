/**
 * 前端引用溯源一致性测试 —— 跑【真代码】，不复制实现。
 *  1) linkifyCitations：从 ChatView.vue 源码里抽出来执行
 *  2) createSseParser：喂 _capture_sse.py 抓到的【真实后端响应体】
 *  3) 两者都要与后端权威解析（_sse_expect.json）逐字对齐
 */
import fs from 'fs'
import { marked } from 'marked'
import { markedHighlight } from 'marked-highlight'
import hljs from 'highlight.js'
import { createSseParser } from './src/api/request.js'

let fail = 0
function check(ok, label, extra = '') {
  console.log((ok ? '  [PASS] ' : '  [FAIL] ') + label + (extra ? '  ' + extra : ''))
  if (!ok) fail++
}

const vue = fs.readFileSync('src/views/ChatView.vue', 'utf8')
const mm = vue.match(/function linkifyCitations[\s\S]*?\n\}/)
check(!!mm, '从 ChatView.vue 抽到 linkifyCitations 源码')
const linkifyCitations = eval('(' + mm[0] + ')')

marked.use(markedHighlight({
  emptyLangClass: 'hljs',
  langPrefix: 'hljs language-',
  highlight(code, lang) {
    const l = hljs.getLanguage(lang) ? lang : 'plaintext'
    return hljs.highlight(code, { language: l }).value
  },
}))
marked.setOptions({ breaks: true, gfm: true })

const md = [
  '基本条件包括… [2]。',
  '',
  '具体条件为… [1]。不得申报… [3]。',
  '',
  '```js',
  'const a = b[0] + c[1]',
  '```',
  '',
  '行内下标 `x[1]` 也不能动。',
].join('\n')

// ★ 第十二轮：linkifyCitations 多了 sources 参数（"没有对应来源的编号"要渲染成幽灵角标）。
//   这里测的是"真依据"这条主路径，所以显式给出 1/2/3 三条来源；
//   "越界编号"的专门用例在 _cite_align_test.mjs。
const out = linkifyCitations(marked.parse(md), [{ n: 1 }, { n: 2 }, { n: 3 }])
const chips = (out.match(/class="cite-ref"/g) || []).length
check(chips === 3, '正文角标数 = 3', 'got=' + chips)
check(out.includes('x[1]'), '行内代码 x[1] 原样保留')
const codeSegs = out.split(/(<pre[\s\S]*?<\/pre>|<code[\s\S]*?<\/code>)/g)
  .filter((_, i) => i % 2 === 1)
check(codeSegs.every(s => !s.includes('cite-ref')), '代码段内 0 个角标', 'segs=' + codeSegs.length)
// ★ 不能直接断言 out.includes('b[0]')：marked-highlight 会把代码拆进 <span>
//   （数字 0 带 hljs-number），原串本来就不连续。必须看【文本内容】。
const preSeg = codeSegs.find(s => s.startsWith('<pre')) || ''
const preText = preSeg.replace(/<[^>]+>/g, '')
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&')
check(preText.includes('b[0]') && preText.includes('c[1]'),
      '代码块的文本内容里 b[0]/c[1] 未被改动', JSON.stringify(preText.trim()))
check(out.includes('data-cite="1"') && out.includes('data-cite="3"'), '角标带 data-cite 编号')

const body = fs.readFileSync('../kb_bench/_sse_body.txt', 'utf8')
const expect = JSON.parse(fs.readFileSync('../kb_bench/_sse_expect.json', 'utf8'))

let text = ''
let sources = null
let sourcesAt = -1
let frameNo = 0
const parser = createSseParser(({ event, data }) => {
  if (event === 'sources') {
    if (sourcesAt === -1) sourcesAt = frameNo
    sources = JSON.parse(data)
  } else {
    text += data
  }
  frameNo++
})

// ★ 故意每 7 个【字节】喂一次，强制把帧边界切断在网络分片中间。
//   必须用 TextDecoder 流式解码 —— 直接 toString 会把中文劈成乱码。
const bytes = Buffer.from(body, 'utf8')
const dec = new TextDecoder()
for (let i = 0; i < bytes.length; i += 7) {
  parser.push(dec.decode(bytes.subarray(i, i + 7), { stream: true }))
}
parser.push(dec.decode())
parser.flush()

check(sources !== null, '解析出 sources 帧')
check(sourcesAt === 0, 'sources 是第 0 帧（在正文之前）', 'idx=' + sourcesAt)
check(!!(sources && Array.isArray(sources.sources) && sources.sources.length > 0),
      'sources 非空', 'n=' + (sources && sources.sources.length))
check(text === expect.text, '正文与后端权威解析逐字一致',
      'len ' + text.length + ' vs ' + expect.text.length)
check(JSON.stringify(sources) === JSON.stringify(expect.sources_payload),
      'sources 载荷与后端权威解析一致')
console.log('  帧总数:', frameNo)
if (sources && sources.sources) {
  console.log('  来源[0]:', JSON.stringify(sources.sources[0]).slice(0, 150))
}
console.log('\n结果: ' + (fail ? 'FAILED ' + fail + ' 项' : '全部通过 ✓'))
process.exit(fail ? 1 : 0)
