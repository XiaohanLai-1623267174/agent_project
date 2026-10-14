/**
 * [零 API] P1-1「黏底自动滚动」v2 的反向对照：证明 _autoscroll_test.mjs 的断言真的有效。
 *
 * 纪律（沿用 §39.31.6 / §39.32.3 / §39.33.5）：
 *   ① 每份坏副本**只坏一处**，锚点在源文件里必须命中**预期次数**（防"锚点漂移"）；
 *   ② 判据 = exit 1 **且** stdout 有 FAIL 行 **且** stderr 无堆栈（抛栈 = 装置崩了 = 假红）；
 *   ③ 每份都必须指名"该被哪条断言抓住"；
 *   ④ 带【行为指纹】自检：坏副本若在本装置抽取的代码范围内**行为无差别**，
 *      先怀疑"无效突变"，不许直接下"断言太弱"的结论。
 *
 * ★ 坏副本写到**系统临时目录**：本项目 dev server 在 watch 项目目录，
 *   往里写临时文件曾导致 Vite watcher EBUSY → 整个进程退出（§39.34 的坑）。
 *
 * v2 新增的关键一份是 **AA**：把"用 progTarget 排除程序滚动"这层保护去掉 ——
 * 那正是用户 2026-10-13 报的缺陷（"没动滚轮也不自动往下"）的**病根**，
 * 必须由 ⑬a 指名抓住，否则这层保护等于没被证明。
 *
 * 用法：node _autoscroll_mutate.mjs
 */
import { readFileSync, writeFileSync, rmSync, existsSync, mkdirSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'

const HERE = dirname(fileURLToPath(import.meta.url))
const SRC = join(HERE, 'src', 'views', 'ChatView.vue')
const TMP = join(tmpdir(), 'dsh_autoscroll_mutate')
const TEST = join(HERE, '_autoscroll_test.mjs')

const MUTATIONS = [
  {
    id: 'X', hits: 1, tag: '⑥',
    why: 'followBottom 的"不跟随就返回"变成永不成立（= 回到史前行为：用户滚上去照样被拽到底）',
    from: 'if (!following.value) {',
    to: 'if (false) {',
    caughtBy: '⑥不跟随时再来分片 ⇒ 一次 scrollTo 都不发生',
  },
  {
    id: 'Y', hits: 1, tag: '③',
    why: '逐分片又用 smooth（一次回答几十次重启动画 ⇒ 观感一顿一顿）',
    from: "await scrollToBottom(false)      // ★ 逐分片必须是 instant",
    to: 'await scrollToBottom(true)',
    caughtBy: '③逐分片必须 instant',
  },
  {
    id: 'Z', hits: 1, tag: '⑩a',
    why: '阈值比较改成 0（只有"正好贴在最底"才算在底部附近 ⇒ 差几像素就恢复不了跟随）',
    // ★ 刻意改**比较**而不是改常量声明：常量被前置检查钉着，改它会被前置拦下 ⇒ "红得不对"。
    from: '<= NEAR_BOTTOM_PX',
    to: '<= 0',
    caughtBy: '⑩a距底 60px ⇒ 算在底部附近',
  },
  {
    id: 'AA', hits: 1, tag: '⑬a',
    why: '★ 去掉"用 progTarget 排除程序滚动"这层保护（= v1 的病根：内容在长时把自己触发的 scroll 事件当成"用户滚走了"）',
    from: 'if (progTarget !== null && Math.abs(el.scrollTop - progTarget) <= 1) {',
    to: 'if (false) {',
    caughtBy: '⑬a程序滚动触发的 scroll 事件 ⇒ 跟随状态【不变】',
  },
  {
    id: 'AB', hits: 1, tag: '⑪',
    why: 'canScroll 恒真（内容根本没超出也冒出「回到底部」按钮）',
    from: 'canScroll.value = el.scrollHeight > el.clientHeight + 1',
    to: 'canScroll.value = true',
    caughtBy: '⑪内容没超出可视高度 ⇒ canScroll=false',
  },
  {
    id: 'AC', hits: 1, tag: '⑭',
    why: '滚轮上滚不再停止跟随（= 用户往回翻时被内容拽着走，P1-1 想解决的原始问题）',
    from: 'if (e.deltaY < 0) { stopFollowing(); return }',
    to: 'if (false) { stopFollowing(); return }',
    caughtBy: '⑭滚轮往上（哪怕只 5px）⇒ 立刻停止跟随',
  },
  {
    id: 'AD', hits: 1, tag: '⑬b',
    why: 'followBottom 里偷偷加回【位置闸门】（= v1 的规则：离底远就不跟随 ⇒ 用户报的"不自动往下"原样复现）',
    from: 'if (!following.value) {',
    to: 'if (!following.value || !isNearBottom(messagesContainer.value)) {',
    caughtBy: '⑬b用户没动滚轮 ⇒ 下一个分片仍然自动跟到底',
  },
  {
    id: 'AE', hits: 1, tag: '⑯b',
    why: '触摸下拉不再停止跟随（手机上往回翻会被拽下去）',
    from: 'if (t.clientY - lastTouchY > TOUCH_INTENT_PX) stopFollowing()',
    to: 'if (false) stopFollowing()',
    caughtBy: '⑯b触摸下拉 ⇒ 停止跟随',
  },
  {
    id: 'AF', hits: 1, tag: '⑱b',
    why: '拖动滚动条往上（没有 wheel 事件的那种）不再停止跟随 —— 位置判据被整个拿掉',
    from: '  if (isNearBottom(el)) resumeFollowing()   // 用户滚回下面了\n  else stopFollowing()                      // 用户拖滚动条往上（这类滚动**没有** wheel 事件）',
    to: '  if (isNearBottom(el)) resumeFollowing()',
    caughtBy: '⑱b用户拖动滚动条往上 ⇒ 停止跟随',
  },
]

// ---------- 行为指纹（防空：区分"无效突变"与"断言太弱"）----------
// 只覆盖抽取范围内的 JS（策略代码）；改模板/接线的突变本就不在这段范围，指纹"无差别"是预期的。
async function fingerprint(text) {
  const i0 = text.indexOf('// ========== Auto-scroll（P1-1')
  const i1 = text.indexOf('// ========== Send ==========')
  if (i0 < 0 || i1 <= i0) return 'EXTRACT-FAILED'
  const code = text.slice(i0, i1)
  const api = ['following', 'canScroll', 'hasNewBelow', 'isNearBottom', 'onMessagesScroll',
    'onWheel', 'onTouchMove', 'onMessagesKeydown', 'followBottom', 'jumpToBottom', 'jumpLabel']
  const el = {
    scrollHeight: 1000, clientHeight: 400, scrollTop: 600, calls: [],
    scrollTo({ top, behavior }) {
      this.calls.push(top + '/' + behavior)
      this.scrollTop = Math.min(Math.max(0, top), Math.max(0, this.scrollHeight - this.clientHeight))
    },
  }
  let mod
  try {
    mod = new Function('ref', 'nextTick', 'messagesContainer',
      code + '\nreturn {' + api.join(', ') + '}')((v) => ({ value: v }), async () => {}, { value: el })
  } catch (e) {
    return 'BUILD-FAILED:' + e.message.slice(0, 40)
  }
  const obs = []
  // ★ 记**完整调用序列**（含 top 与 behavior），不只记次数：
  //   只记次数的话，"把逐分片从 instant 改成 smooth"这种行为差异会被指纹抹平（本版踩过）。
  const snap = (tag) => obs.push([tag, el.calls.join('|'), el.scrollTop, mod.following.value,
    mod.hasNewBelow.value, mod.canScroll.value, mod.jumpLabel()])
  const run = async () => {
    snap('init')
    await mod.followBottom(); snap('follow@bottom')
    el.scrollTop = 100; mod.onMessagesScroll(); snap('scrolledUp')
    await mod.followBottom(); snap('follow@up')
    await mod.jumpToBottom(); snap('jump')
    el.scrollHeight = 400; el.scrollTop = 0; mod.onMessagesScroll(); snap('short')
    // 阈值边界也要探：不然"把阈值改成 0"这种突变在指纹里看不出来
    el.scrollHeight = 1000; el.scrollTop = 540; mod.onMessagesScroll(); snap('exactly60')
    el.scrollTop = 539; mod.onMessagesScroll(); snap('exactly61')
    // 滚轮/触摸/键盘三条"用户意图"通道（★ 每条前先复位成 following=true，
    //   否则上一步 already-false 的状态会把差异掩盖掉 ⇒ 指纹误报"无差别"）
    await mod.jumpToBottom(); mod.onWheel({ deltaY: -5 }); snap('wheelUp')
    await mod.jumpToBottom(); mod.onWheel({ deltaY: 120 }); snap('wheelDown')
    await mod.jumpToBottom(); mod.onTouchMove({ touches: [{ clientY: 999 }] }); snap('touch')
    await mod.jumpToBottom(); mod.onMessagesKeydown({ key: 'PageUp' }); snap('keyPageUp')
    // ★ v2 核心：程序滚动 + 内容增长（不该被当成"用户滚走了"）
    //   ★ 先 jumpToBottom 复位成 following=true：否则上一步 PageUp 已经把状态置 false，
    //     "内容增长后还跟不跟"这个差异会被前一步掩盖掉（指纹显示"无差别"＝误导性提示）。
    await mod.jumpToBottom()
    el.scrollHeight = 1000; el.scrollTop = 600
    await mod.followBottom()
    el.scrollHeight = 1700
    mod.onMessagesScroll(); snap('grownAfterProgScroll')
    // ★ 离底还很远时**再**来一个分片：v2 必须继续跟；v1（位置闸门）会拒绝 ⇒ 指纹能看出差别
    await mod.followBottom(); snap('followAfterGrowth')
  }
  // ★ 必须 await：followBottom/jumpToBottom 是 async 函数，体里第一个 await 之后的部分
  //   会推到微任务里；不等它们，"指纹"读到的全是**动作之前**的状态 ⇒ 差异被抹平、
  //   好突变会被误报成"无效突变"（这一版就是这么发现的）。
  try { await run() } catch (e) { return 'RUN-FAILED:' + e.message.slice(0, 40) }
  return JSON.stringify(obs)
}

const src = readFileSync(SRC, 'utf8')
const fp0 = await fingerprint(src)
if (existsSync(TMP)) rmSync(TMP, { recursive: true, force: true })
mkdirSync(TMP, { recursive: true })

console.log('='.repeat(100))
console.log('P1-1「黏底自动滚动」v2 反向对照（%d 份坏副本，每份只坏一处）', MUTATIONS.length)
console.log('='.repeat(100))

const rows = []
for (const m of MUTATIONS) {
  const hits = src.split(m.from).length - 1
  if (hits !== m.hits) {
    rows.push({ ...m, anchorBad: `锚点命中 ${hits} 次（预期 ${m.hits}）` })
    continue
  }
  const bad = join(TMP, `bad_${m.id}.vue`)
  const mutated = src.split(m.from).join(m.to)
  writeFileSync(bad, mutated, 'utf8')
  const fpChanged = (await fingerprint(mutated)) !== fp0
  const r = spawnSync(process.execPath, [TEST], {
    cwd: HERE, encoding: 'utf8', env: { ...process.env, AUTOSCROLL_SFC: bad },
  })
  const out = r.stdout || ''
  const err = r.stderr || ''
  // ★ 失败行两种格式都要认：行为断言打 `[FAIL] ⑥ …`（stdout），
  //   前置检查打 `FAIL 装置前置：…`（stderr）。只认一种 ⇒ 真红会被误判成"未真红"（本版踩过）。
  const failLines = (out + '\n' + err).split('\n').map((s) => s.trim())
    .filter((s) => s.startsWith('[FAIL]') || s.startsWith('FAIL'))
  const threw = /\bat file:/.test(err) || /^\s+at /.test(err.split('\n').find((l) => l.trim().startsWith('at ')) || '')
  const tail = out.trim().split('\n').pop() || ''
  const tagHit = failLines.some((l) => l.includes(m.tag))
  rows.push({
    ...m, status: r.status, n: failLines.length, threw, fpChanged, failLines, tagHit,
    trueRed: r.status === 1 && failLines.length >= 1 && !threw, tail,
  })
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
  console.log(`        exit=${r.status}  FAIL 行=${r.n}  抛栈=${r.threw}  行为指纹=${r.fpChanged ? '有变化' : '无差别'}  指名断言命中=${r.tagHit ? '是' : '否'}`)
  console.log(`        装置结论: ${r.tail}`)
  if (!r.trueRed && !r.fpChanged) {
    console.log('        ⚠ 提示：这份突变在抽取的 JS 范围内【行为无差别】。')
    console.log('          先怀疑它是【无效突变】或改在模板/接线层，不要直接下"断言太弱"的结论。（§39.33.5）')
  }
  for (const l of r.failLines.slice(0, 3)) console.log(`          · ${l}`)
}

// 期望断言命中自检：坏副本红了，但**必须是被指名的那条**抓住的（避免"红得不对"）
console.log()
let named = 0
const red = rows.filter((r) => r.trueRed)
for (const r of red) {
  if (r.tagHit) named++
  else console.log(`  ⚠ [${r.id}] 红了，但 FAIL 行里没有指名的「${r.caughtBy}」—— 请核对是不是"红得不对"`)
}
console.log(`指名的断言命中 ${named}/${red.length} 份真红`)

const nRed = red.length
console.log()
console.log(`${nRed}/${rows.length} 真红${named === nRed ? '、且每份都由指名断言抓住' : ''} —— ${nRed === rows.length && named === nRed ? '断言有效性已证明' : '有突变没被抓住 / 红得不对，断言有效性未证明'}`)
console.log('='.repeat(100))
rmSync(TMP, { recursive: true, force: true })
process.exit(nRed === rows.length && named === nRed ? 0 : 1)