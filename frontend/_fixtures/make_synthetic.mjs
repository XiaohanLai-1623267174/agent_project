/**
 * 生成随仓库发布的【合成】SSE 抓包夹具。
 *
 * 用法：node _fixtures/make_synthetic.mjs   （在 frontend/ 目录下跑）
 *
 * 为什么要合成而不是直接用真实抓包：真实抓包里含**用户内容** ——
 * 用户所在学校名（`海洋信息工程学院专业奖学金评定办法`）、上传的文档名（`奖学金评定办法.pdf`）、
 * 制度原文与 snippet。这些属于用户数据，**不随公开仓库发布**。
 * 合成夹具的**帧结构与字段结构完全一致**，只是内容全是假的，因此装置照样能验解析器。
 *
 * 为什么"权威解析"要用独立实现算：如果拿装置的解析器输出当期望值，就是自己证自己。
 * 这里用另一套正则解析（与 src/api/request.js 的解析器不同源）来产出期望值。
 *
 * ★ 换行怎么编码（第一版就栽在这里，被下面的自检当场拦住）：
 *   换行**不能直接写进 `data:` 行里** —— 那会让这一帧被空行截断。真实抓包用的是
 *   【帧内多行 data】：
 *       data:            <- 空载荷
 *       data:二、         <- 同一帧的第二行
 *   同一帧内多行 data 用 \n 连接 ⇒ 载荷 = "\n二、"。
 *   帧与帧之间则**直接拼接、不加任何分隔符**（所以 `data:]` 与下一帧 `data:1` 是 "]" + "1"）。
 */
import { writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))

// ---- 合成来源载荷（字段名抄真实抓包，值全是假的） ----
const SOURCES = {
  attempted: true,
  candidates: 3,
  sources: [
    {
      n: 1, document_id: 1, chunk_id: 'chunk_001',
      filename: '示例制度文档.md',
      doc_title: '示例制度文档（合成夹具）',
      section_path: '示例制度文档（合成夹具） -> 第一条 示例条款 -> （一） 示例基本条件',
      score_type: '示例类别', target_audience: '示例对象', score: 0.9,
      snippet: '示例条文：本条由 _fixtures/make_synthetic.mjs 生成，与任何真实制度无关。',
    },
    {
      n: 2, document_id: 1, chunk_id: 'chunk_002',
      filename: '示例制度文档.md',
      doc_title: '示例制度文档（合成夹具）',
      section_path: '示例制度文档（合成夹具） -> 第一条 示例条款 -> （二） 示例具体条件',
      score_type: '示例类别', target_audience: '示例对象', score: 0.8,
      snippet: '示例条文：示例课程学时加权平均成绩达 70 分。',
    },
    {
      n: 3, document_id: 1, chunk_id: 'chunk_003',
      filename: '示例制度文档.md',
      doc_title: '示例制度文档（合成夹具）',
      section_path: '示例制度文档（合成夹具） -> 第一条 示例条款 -> （三） 示例禁止情形',
      score_type: '示例类别', target_audience: '示例对象', score: 0.7,
      snippet: '示例条文：示例情形甲、示例情形乙。',
    },
  ],
  reranked: true,
}

// ---- 合成正文：三个角标 [1][2][3] 各出现一次 ----
// （装置要求"每个角标都有依据"且"真角标数 = 正文里不同编号的个数"，故每个编号只能出现一次）
const LINES = [
  '一、示例基本条件 [1]',
  '',
  '1. 本条为合成夹具内容，与任何真实制度无关。',
  '2. 用于验证 SSE 解析器在按 7 字节切分时的边界处理与中文解码。',
  '3. 标点应原样保留：，。「」（）——《》 等。',
  '',
  '二、示例具体条件 [2]',
  '',
  '1. 示例课程学时加权平均成绩达 70 分。',
  '2. 示例综合素质测评分在本专业前 50% 以内。',
  '',
  '三、示例禁止情形 [3]',
  '',
  '1. 示例情形甲。',
  '2. 示例情形乙。',
]
const TEXT = LINES.join('\n')

// ---- 切成碎帧：模仿真实抓包的"一段话切成几十个小帧"，顺带逼出跨帧边界问题 ----
const LENS = [1, 2, 3, 11, 5, 1, 4, 7, 2, 9]   // 长度刻意参差
const parts = []                                // { nl, text }
let pending = 0
for (const seg of TEXT.split(/(\n)/)) {
  if (seg === '\n') { pending++; continue }
  if (seg === '') continue
  for (let i = 0, k = 0; i < seg.length; ) {
    const n = LENS[k++ % LENS.length]
    parts.push({ nl: pending, text: seg.slice(i, i + n) })
    pending = 0
    i += n
  }
}

// ---- 拼正文：sources 必须是第 0 帧；最后一帧后面不留空行（与真实抓包一致） ----
let body = 'event:sources\ndata:' + JSON.stringify(SOURCES) + '\n\n'
for (const p of parts) {
  const lines = []
  for (let i = 0; i < p.nl; i++) lines.push('data:')   // 帧内前导换行
  lines.push('data:' + p.text)
  body += lines.join('\n') + '\n\n'
}
body = body.replace(/\n\n$/, '\n')

// ---- 独立解析（另一套实现，不与前端解析器同源） ----
function parseBody(s) {
  const out = []
  let ev = null
  let data = []
  for (const line of s.split('\n')) {
    if (line === '') {                                  // 空行收帧
      if (data.length) out.push({ event: ev, data: data.join('\n') })
      ev = null
      data = []
      continue
    }
    if (line.startsWith('event:')) ev = line.slice(6)
    else if (line.startsWith('data:')) data.push(line.slice(5))
  }
  if (data.length) out.push({ event: ev, data: data.join('\n') })
  return out
}

const frames = parseBody(body)
const srcFrame = frames.find((f) => f.event === 'sources')
const text = frames.filter((f) => f.event !== 'sources').map((f) => f.data).join('')

// ---- 自检：生成意图与独立解析必须一致，否则宁可失败也不产出坏夹具 ----
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b)
if (!srcFrame) throw new Error('❌ 合成正文里没有 sources 帧')
if (frames[0].event !== 'sources') throw new Error('❌ sources 不是第 0 帧（_cite_test.mjs 会因此红）')
if (text !== TEXT) {
  const at = [...TEXT].findIndex((c, i) => c !== [...text][i])
  throw new Error(`❌ 独立解析出的正文与生成意图不一致：首个差异在第 ${at} 个字符` +
    `（意图 ${JSON.stringify([...TEXT].slice(at, at + 12).join(''))} vs 解析 ${JSON.stringify([...text].slice(at, at + 12).join(''))}）`)
}
if (!eq(JSON.parse(srcFrame.data), SOURCES)) throw new Error('❌ 独立解析出的 sources 与生成意图不一致')
const nums = [...new Set((text.match(/\[(\d{1,2})\]/g) || []).map((x) => Number(x.slice(1, -1))))].sort((a, b) => a - b)
const have = SOURCES.sources.map((s) => s.n).sort((a, b) => a - b)
if (!eq(nums, have)) throw new Error(`❌ 角标编号 ${JSON.stringify(nums)} 与来源编号 ${JSON.stringify(have)} 对不上（会产生幽灵角标）`)
if ((text.match(/\[1\]/g) || []).length !== 1 || (text.match(/\[2\]/g) || []).length !== 1 || (text.match(/\[3\]/g) || []).length !== 1) {
  throw new Error('❌ 每个角标必须恰好出现一次（否则 chips 数会与不同编号个数不等）')
}

writeFileSync(join(HERE, 'synthetic_sse_body.txt'), body, 'utf8')
writeFileSync(join(HERE, 'synthetic_sse_expect.json'), JSON.stringify({
  _note: '合成夹具：由 _fixtures/make_synthetic.mjs 生成，内容全为虚构，非真实抓包。',
  _why: '真实抓包含用户所在学校名与上传文档名，属用户内容，故不随公开仓库发布。',
  _parse: '本文件的 text / sources_payload 由 make_synthetic.mjs 里的独立正则解析产出，不是抓包现场的后端输出。',
  text,
  sources_payload: SOURCES,
}, null, 2) + '\n', 'utf8')

console.log('✅ 已生成合成夹具')
console.log('   帧数 =', frames.length, '（其中正文帧', frames.length - 1, '）')
console.log('   正文 =', text.length, '字符；角标编号 =', JSON.stringify(nums))
console.log('   文件 = synthetic_sse_body.txt / synthetic_sse_expect.json')