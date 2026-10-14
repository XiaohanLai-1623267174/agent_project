/**
 * [零 API] B 项反向对照：生成坏副本，用来证明 _noretr_render_test.mjs 真的会红。
 *
 * 为什么必须做：本会话已抓过多种假绿（§39.27.5 / §39.28）——
 * 反向对照 exit 1 却无 FAIL 行、断言被注释白送、正则漏键。
 * "测试全绿"只有在"把它弄坏会变红"被证明之后才算证据。
 *
 * 每个突变对应一个真实回归，并且**每个都必须被某条断言抓住**：
 *   A 详情面板忘掉"收紧"          -> 给"压根没检索"的消息报假状态（已重排）
 *   B 新提示块忘掉角色守卫        -> 用户消息显示"本条未检索资料"
 *   C 提示块塞回链中间 + 放宽条件 -> 顶掉「未检索到相关资料」（§39.24.3 忠实复现）
 *   D 空状态忘掉角色守卫          -> 用户消息显示"未检索到相关资料"
 *   E 来源面板忘掉角色守卫        -> 用户消息显示"来源 N 段"
 *   F 历史提示忘掉角色守卫        -> 用户消息显示"历史消息…"
 *
 * 用法：node _rt_mutate.mjs     （只生成坏副本，绝不改任何真文件）
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const SRC = join(HERE, 'src', 'views', 'ChatView.vue')
const OUT = join(HERE, '_rt_tmp')
const text = readFileSync(SRC, 'utf8')

const ROLE = `msg.role === 'assistant' && `
const HINT_IF = `v-if="!msg.streaming && msg.role === 'assistant' && msg.retrieval && msg.retrieval.attempted === false"`
const HINT_TEXT = `class="msg-sources is-empty">本条未检索资料（判断为无需查库）</div>`
const HINT_DIV = `            <div ${HINT_IF}\n                 ${HINT_TEXT}`
const EMPTY_ANCHOR = `            <div v-else-if="!msg.streaming && msg.role === 'assistant' && msg.retrieval && msg.retrieval.attempted && !msg.retrieval.candidates"`

/** 每个突变：id / 为什么 / 变换函数。变换后必须真的与原文不同。 */
const MUTS = [
  ['A', '详情面板忘掉"收紧"（去掉 msg.retrieval.attempted）',
    (t) => t.replace(`${ROLE}msg.retrieval && msg.retrieval.attempted" class="msg-debug"`,
                     `msg.retrieval" class="msg-debug"`)],

  ['B', '新提示块忘掉角色守卫',
    (t) => t.replace(HINT_IF, `v-if="!msg.streaming && msg.retrieval && msg.retrieval.attempted === false"`)],

  // ★ 忠实复现 §39.24.3：把提示块【搬到空状态之前】（即链中间），并放宽条件使其
  //   与空状态的条件【重叠】—— 只有重叠才会顶掉空状态（这是第一次对照没红的真因）。
  ['C', '提示块塞回链中间 + 条件放宽到与空状态重叠',
    (t) => {
      const loose = `            <div v-else-if="!msg.streaming && msg.role === 'assistant' && msg.retrieval"\n                 ${HINT_TEXT}`
      return t.replace(HINT_DIV, '').replace(EMPTY_ANCHOR, `${loose}\n${EMPTY_ANCHOR}`)
    }],

  ['D', '空状态忘掉角色守卫',
    (t) => t.replace(`v-else-if="!msg.streaming && ${ROLE}msg.retrieval && msg.retrieval.attempted && !msg.retrieval.candidates"`,
                     `v-else-if="!msg.streaming && msg.retrieval && msg.retrieval.attempted && !msg.retrieval.candidates"`)],

  ['E', '来源面板忘掉角色守卫',
    (t) => t.replace(`v-if="!msg.streaming && ${ROLE}msg.sources && msg.sources.length" class="msg-sources"`,
                     `v-if="!msg.streaming && msg.sources && msg.sources.length" class="msg-sources"`)],

  ['F', '历史提示忘掉角色守卫',
    (t) => t.replace(`v-else-if="!msg.streaming && ${ROLE}msg.sources && msg.sources.length" class="msg-debug-note"`,
                     `v-else-if="!msg.streaming && msg.sources && msg.sources.length" class="msg-debug-note"`)],
]

mkdirSync(OUT, { recursive: true })
const made = []
for (const [id, why, apply] of MUTS) {
  const bad = apply(text)
  // ★ 突变必须真的改到了东西，否则这份"坏副本"其实没坏，对照结论作废
  if (bad === text) throw new Error(`突变 ${id} 没改到任何内容 —— 源码改了，来更新本脚本`)
  const file = join(OUT, `${id}.vue`)
  writeFileSync(file, bad, 'utf8')
  made.push({ id, why, file, bytes: Buffer.byteLength(readFileSync(file, 'utf8')) })
}

console.log(JSON.stringify({ srcBytes: Buffer.byteLength(text), made }, null, 2))