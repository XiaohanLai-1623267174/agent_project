// P1-1「黏底自动滚动」行为装置：抽【真代码】+ 用【假滚动容器】驱动，验的是**策略**而不是类名。
//
// 为什么要有它
// ------------
// 史前行为是"每读完一个网络分片就无条件拉到底"（一次回答几十次），用户在往回翻时会被反复拽下去。
// 这类问题**用"类名/样式在不在"验不出来**（旧代码同样"有滚动、有动画"），
// 必须把"跟不跟随"的判断当成策略来测：给定**用户动作**与**滚动位置** ⇒ 该不该动、动了要不要 smooth。
//
// v2（2026-10-13，用户实测报缺陷后重做）
// ------------------------------------
// v1 的判据是【位置】：距底 ≤60px 才跟随。用户实测"**不需要向上翻阅时也无法自动往下**"。
// 病根：**内容自己在长**的时候距底会自然变大，而我们**自己触发的 scroll 事件**会被当成
// "用户滚走了" ⇒ 跟随被永久停掉。v2 改成按【用户意图】：只有滚轮上滚 / 触摸下拉 / 键盘上翻 /
// 拖动滚动条向上才停；没动过滚轮就一路跟到底。**⑬ 就是钉这个病根的回归断言。**
//
// 装置结构（沿用本项目已验证过的"真代码 + 假宿主"模式）
//   · 抽取：ChatView.vue 里 `// ========== Auto-scroll（P1-1` 到 `// ========== Send ==========` 之间的**真代码**
//   · 宿主：假 `ref` / 假 `nextTick` / 假滚动容器（含 scrollTo 调用记录、**真实的 clamp 行为**，
//     以及一个"派发 scroll 事件"的 helper —— 用来分别模拟【我们自己滚的】与【用户滚的】）
//   · 反向对照见 `_autoscroll_mutate.mjs`（每份坏副本必须被**具名断言**抓住）
import fs from 'node:fs'
import path from 'node:path'

const FRONT = process.cwd()
// 反向对照会把"坏副本"写到**系统临时目录**并指到这里（AUTOSCROLL_SFC）。
// ★ 刻意不写到项目里：本项目的 dev server 在 watch 项目目录，
//   往里塞临时文件曾把 Vite 的 watcher 打崩（EBUSY → 进程退出，见 §39.34 的坑）。
const SFC_PATH = process.env.AUTOSCROLL_SFC || path.join(FRONT, 'src/views/ChatView.vue')
const SFC = fs.readFileSync(SFC_PATH, 'utf8')

const START = '// ========== Auto-scroll（P1-1'
const END = '// ========== Send =========='
const i0 = SFC.indexOf(START)
const i1 = SFC.indexOf(END)
if (i0 < 0 || i1 < 0 || i1 <= i0) {
  console.error('FAIL 装置前置：抽不到 Auto-scroll 代码块（起始 ' + i0 + ' / 结束 ' + i1 + '）')
  process.exit(1)
}
const CODE = SFC.slice(i0, i1)

// ---- 前置自检（缺失一律报 FAIL 行，绝不 throw —— 装置自己崩掉＝假红，见 §39.32.3）----
const PRE = [
  ['const NEAR_BOTTOM_PX = 60', '阈值常量'],
  ['const TOUCH_INTENT_PX = 8', '触摸意图常量'],
  ['const following = ref(true)', 'following 状态'],
  ['let progTarget = null', '程序滚动目标'],
  ['function isNearBottom(el)', '距底判断'],
  ['function stopFollowing()', '"用户想看上文"的统一入口'],
  ['function resumeFollowing()', '"用户回到下面"的统一入口'],
  ['function onWheel(e)', '滚轮意图'],
  ['function onTouchMove(e)', '触摸意图'],
  ['function onMessagesKeydown(e)', '键盘意图（★ 名字不能叫 onKeydown：与本文件输入框那个重名 ⇒ SFC 编译报错）'],
  ['function onMessagesScroll()', '滚动监听'],
  ['async function followBottom()', '逐分片跟随'],
  ['async function jumpToBottom()', '强制到底'],
  ['function jumpLabel()', '按钮文案'],
  ['hasNewBelow.value = true', '记录"下面有新内容"'],
]
let preOk = true
for (const [needle, label] of PRE) {
  if (!CODE.includes(needle)) { console.error('FAIL 装置前置：' + label + ' 不在抽取范围内（锚点漂移？）'); preOk = false }
}
// 关键语义：不跟随时必须【提前 return】，不许再往下拉。
// ★ 条件本身刻意**不写死**（只查"记新内容 + return"这个顺序还在）：本行只防"锚点漂移/结构被拆"，
//   条件语义由 ⑥⑬ 的**行为断言**负责 —— 否则给条件加一项（如偷偷加回位置闸门）的坏副本
//   会被前置检查抢先拦下，变成"红得不对"（红了但不是被指名的那条断言抓住）。见 _autoscroll_mutate.mjs 的 AD。
if (!/hasNewBelow\.value = true[\s\S]{0,80}?return/.test(CODE)) {
  console.error('FAIL 装置前置：followBottom 里"先记新内容、再 return"的结构不在了')
  preOk = false
}
if (!CODE.includes('behavior: smooth ? \'smooth\' : \'instant\'')) {
  console.error('FAIL 装置前置：scrollToBottom 的 instant 分支不在抽取范围内')
  preOk = false
}
// v2 的核心结构：scroll 事件里必须用 progTarget 把"自己滚的"排除掉。
// ★ 同样只查"两半机制都在"（取目标 / 清目标），**不查比较式** ——
//   把比较式改成 `if (false)` 的坏副本必须由 ⑬a 指名抓住，不能被前置拦下。
if (!CODE.includes('progTarget = Math.min') || !CODE.includes('progTarget = null')) {
  console.error('FAIL 装置前置：progTarget 的"设目标 / 清目标"两半不都在抽取范围内（v2 的核心）')
  preOk = false
}
if (!preOk) { console.error('装置前置失败 ⇒ 不继续（下面的结论都会是假的）'); process.exit(1) }

const API = ['following', 'canScroll', 'hasNewBelow', 'isNearBottom', 'syncScrollState',
  'stopFollowing', 'resumeFollowing', 'onWheel', 'onTouchStart', 'onTouchMove', 'onMessagesKeydown',
  'onMessagesScroll', 'scrollToBottom', 'followBottom', 'jumpToBottom', 'jumpLabel',
  'NEAR_BOTTOM_PX', 'TOUCH_INTENT_PX']
let build
try {
  build = new Function('ref', 'nextTick', 'messagesContainer',
    CODE + '\nreturn {' + API.join(', ') + '}')
} catch (e) {
  console.error('FAIL 装置前置：抽取的代码无法求值 —— ' + e.message)
  process.exit(1)
}

/** 假滚动容器：记录每次 scrollTo，像浏览器一样把 scrollTop **夹到**合法范围。 */
function makeEl({ scrollHeight = 1000, clientHeight = 400, scrollTop = 600 } = {}) {
  return {
    scrollHeight, clientHeight, scrollTop,
    calls: [],
    scrollTo({ top, behavior }) {
      this.calls.push({ top, behavior })
      const max = Math.max(0, this.scrollHeight - this.clientHeight)
      this.scrollTop = Math.min(Math.max(0, top), max)
    },
  }
}

function makeHost(el) {
  const messagesContainer = { value: el }
  const ref = (v) => ({ value: v })
  const nextTick = async () => {}
  return build(ref, nextTick, messagesContainer)
}

// ---------------- 断言 ----------------
let pass = 0
const fails = []
function check(ok, label, extra = '') {
  if (ok) { pass++; console.log('  [PASS] ' + label + (extra ? '  ' + extra : '')) }
  else { fails.push(label); console.log('  [FAIL] ' + label + (extra ? '  ' + extra : '')) }
}
const nearBottom = (el) => el.scrollHeight - el.scrollTop - el.clientHeight

console.log('='.repeat(84))
console.log('P1-1 黏底自动滚动 · 行为装置 v2（真代码 + 假滚动容器；判据=用户意图）')
console.log('='.repeat(84))

// ① 起始状态
{
  const el = makeEl()
  const H = makeHost(el)
  check(H.following.value === true && H.canScroll.value === false && H.hasNewBelow.value === false,
    '① 起始：跟随底部、尚无溢出、无"新内容"', `following=${H.following.value} canScroll=${H.canScroll.value}`)
}

// ②③④ 在底部时跟随：滚到底 + 逐分片必须 instant
{
  const el = makeEl({ scrollTop: 600 })          // 距底 0 ⇒ 在底部
  const H = makeHost(el)
  await H.followBottom()
  const c = el.calls
  check(c.length === 1 && c[0].top === el.scrollHeight,
    '② 在底部时来一个分片 ⇒ 真的滚到底', `调用 ${c.length} 次，top=${c[0] && c[0].top} / scrollHeight=${el.scrollHeight}`)
  check(c[0].behavior === 'instant',
    '③ 逐分片必须 instant（smooth 会被重启几十次）', `behavior=${c[0].behavior}`)
  await H.followBottom()
  check(H.canScroll.value === true, '④ 内容超出可视高度 ⇒ canScroll=true', `canScroll=${H.canScroll.value}`)
}

// ⑤⑥⑦ 用户滚上去（拖动滚动条那种，没有 wheel 事件）⇒ 停止跟随，且**绝不再动他的滚动条**
{
  const el = makeEl({ scrollTop: 600 })
  const H = makeHost(el)
  await H.followBottom()
  const before = el.calls.length
  el.scrollTop = 100                              // 用户往上滚
  H.onMessagesScroll()
  check(H.following.value === false, '⑤ 滚上去后 onMessagesScroll ⇒ following=false', `following=${H.following.value}`)
  await H.followBottom()                          // 又来了新分片
  check(el.calls.length === before,
    '⑥ 不跟随时再来分片 ⇒ 一次 scrollTo 都不发生（史前行为就是死在这里）',
    `调用 ${before} → ${el.calls.length}，scrollTop 仍是 ${el.scrollTop}`)
  check(H.hasNewBelow.value === true && H.jumpLabel() === '有新内容',
    '⑦ 不跟随时记下"下面有新内容"，按钮文案=有新内容', `hasNewBelow=${H.hasNewBelow.value} label=${H.jumpLabel()}`)
}

// ⑧ 点按钮 ⇒ 回到跟随
{
  const el = makeEl({ scrollTop: 100 })
  const H = makeHost(el)
  H.onMessagesScroll()
  await H.followBottom()
  await H.jumpToBottom()
  check(el.scrollTop === el.scrollHeight - el.clientHeight && H.following.value === true &&
        H.hasNewBelow.value === false && H.jumpLabel() === '回到底部',
    '⑧ 点「回到底部」⇒ 到底 + 恢复跟随 + 文案复位',
    `scrollTop=${el.scrollTop}/${el.scrollHeight - el.clientHeight} following=${H.following.value} label=${H.jumpLabel()}`)
}

// ⑨ 发送（following=false 时也强制到底）
{
  const el = makeEl({ scrollTop: 0 })
  const H = makeHost(el)
  H.onMessagesScroll()
  check(H.following.value === false, '⑨a 先造出"用户已经滚到顶"的状态', `following=${H.following.value}`)
  const n = el.calls.length
  await H.jumpToBottom()
  check(el.calls.length === n + 1 && el.scrollTop === el.scrollHeight - el.clientHeight,
    '⑨b 自己按发送 ⇒ 强制到底（不看跟不跟随）', `调用 ${n} → ${el.calls.length}`)
}

// ⑩ 阈值语义钉死：60 算"在底部附近"，61 不算（★ 它现在只用于【恢复/拖动识别】，不再决定"内容增长时停不停"）
{
  const el = makeEl({ scrollTop: 540 })           // 距底 = 600-540 = 60
  const H = makeHost(el)
  check(H.isNearBottom(el) === true, '⑩a 距底 60px ⇒ 算在底部附近（跟随）', `距底=${nearBottom(el)}`)
  el.scrollTop = 539                              // 距底 61
  check(H.isNearBottom(el) === false, '⑩b 距底 61px ⇒ 不算（停止跟随）', `距底=${nearBottom(el)}`)
  H.onMessagesScroll()
  check(H.following.value === false, '⑩c 阈值经 onMessagesScroll 生效（用户拖动场景）', `following=${H.following.value}`)
}

// ⑪ 内容够短 ⇒ 不该冒出按钮
{
  const el = makeEl({ scrollHeight: 400, clientHeight: 400, scrollTop: 0 })
  const H = makeHost(el)
  await H.followBottom()
  check(H.canScroll.value === false, '⑪ 内容没超出可视高度 ⇒ canScroll=false（按钮不该出现）', `canScroll=${H.canScroll.value}`)
}

// ⑫ 接线（模板层）：滚动监听与按钮确实挂在真模板上
{
  check(SFC.includes('@scroll.passive="onMessagesScroll"'),
    '⑫a 滚动容器绑定了 @scroll.passive="onMessagesScroll"')
  check(/v-if="!following && canScroll"/.test(SFC) && SFC.includes('class="btn-jump-bottom"'),
    '⑫b 按钮只在「不跟随 且 有溢出」时出现')
  check(/class="btn-jump-bottom"[^>]*@click="jumpToBottom\(\)"/s.test(SFC),
    '⑫c 按钮的点击接到 jumpToBottom()')
  check(SFC.includes('await followBottom()') && !/parser\.push\(decoder\.decode\(value, \{ stream: true \}\)\)\s*\n\s*await scrollToBottom\(\)/.test(SFC),
    '⑫d 流式逐分片已改成 followBottom（无条件 scrollToBottom 不再出现在那个位置）')
}

// ⑬ ★★ 本轮核心回归（用户 2026-10-13 报的缺陷）：
//    内容自己在长 + 我们自己那次滚动补发的 scroll 事件 ⇒ **不许**被当成"用户滚走了"
{
  const el = makeEl({ scrollHeight: 1000, clientHeight: 400, scrollTop: 600 })
  const H = makeHost(el)
  await H.followBottom()                          // 到底（progTarget=600, scrollTop=600）
  el.scrollHeight = 1600                          // 网速快：这一帧内容又长了一大截
  H.onMessagesScroll()                            // 浏览器为**我们自己**那次 scrollTo 补发的事件
  check(H.following.value === true,
    '⑬a 程序滚动触发的 scroll 事件 ⇒ 跟随状态【不变】（v1 病根：这里被误判成"用户滚走了"）',
    `following=${H.following.value} 距底=${nearBottom(el)}`)
  const n = el.calls.length
  await H.followBottom()
  check(el.calls.length === n + 1 && el.scrollTop === el.scrollHeight - el.clientHeight,
    '⑬b ★ 用户没动滚轮 ⇒ 下一个分片仍然自动跟到底（用户报的"不自动往下"）',
    `调用 ${n} → ${el.calls.length}，scrollTop=${el.scrollTop}/${el.scrollHeight - el.clientHeight}`)
}

// ⑬c 再补一刀：连续多个分片之间夹杂 scroll 事件，也必须一路跟到底
{
  const el = makeEl({ scrollHeight: 1000, clientHeight: 400, scrollTop: 600 })
  const H = makeHost(el)
  for (let i = 1; i <= 5; i++) {
    el.scrollHeight = 1000 + i * 300              // 每个分片都长 300px
    await H.followBottom()
    H.onMessagesScroll()                          // 补发的 scroll 事件
  }
  check(H.following.value === true && el.scrollTop === el.scrollHeight - el.clientHeight,
    '⑬c 连续 5 个分片 + 每次都补发 scroll 事件 ⇒ 依然黏在底部',
    `following=${H.following.value} scrollTop=${el.scrollTop}/${el.scrollHeight - el.clientHeight}`)
}

// ⑭⑮ 滚轮意图
{
  const el = makeEl({ scrollHeight: 2000, clientHeight: 400, scrollTop: 1600 })   // 在底部
  const H = makeHost(el)
  await H.followBottom()
  H.onWheel({ deltaY: -5 })                       // 只往上滚 5px
  check(H.following.value === false,
    '⑭ ★ 滚轮往上（哪怕只 5px）⇒ 立刻停止跟随（位置判据在这里正好是瞎的：5px 仍在 60px 内）',
    `following=${H.following.value} 距底=${nearBottom(el)}`)
  // 不跟随时又来分片：绝不动他的滚动条
  const n = el.calls.length
  await H.followBottom()
  check(el.calls.length === n, '⑭b 不跟随时来分片 ⇒ 不动滚动条', `调用 ${n} → ${el.calls.length}`)
  // 往下滚 + 已经在底部附近 ⇒ 恢复
  H.onWheel({ deltaY: 120 })
  check(H.following.value === true, '⑮ 滚轮往下且已在底部附近 ⇒ 恢复跟随', `following=${H.following.value}`)
  // 往下滚但人还在上面 ⇒ 不许恢复（否则用户在上文里会被拽下去）
  const el2 = makeEl({ scrollHeight: 2000, clientHeight: 400, scrollTop: 200 })
  const H2 = makeHost(el2)
  H2.onWheel({ deltaY: -30 })
  H2.onWheel({ deltaY: 30 })
  check(H2.following.value === false,
    '⑮b 往下滚但离底还很远 ⇒ 保持不跟随（正在看上文）', `following=${H2.following.value} 距底=${nearBottom(el2)}`)
}

// ⑯ 触摸意图：手指往下拖（想看上文）
{
  const el = makeEl({ scrollHeight: 2000, clientHeight: 400, scrollTop: 1600 })
  const H = makeHost(el)
  H.onTouchStart({ touches: [{ clientY: 300 }] })
  H.onTouchMove({ touches: [{ clientY: 304 }] })   // 只动 4px ⇒ 不算意图
  check(H.following.value === true, '⑯a 触摸抖动 4px ⇒ 不算"想看上文"（阈值 8px）', `following=${H.following.value}`)
  H.onTouchMove({ touches: [{ clientY: 340 }] })   // 下拖 36px
  check(H.following.value === false, '⑯b ★ 触摸下拉 ⇒ 停止跟随', `following=${H.following.value}`)
}

// ⑰ 键盘意图
{
  const el = makeEl({ scrollHeight: 2000, clientHeight: 400, scrollTop: 1600 })
  const H = makeHost(el)
  H.onMessagesKeydown({ key: 'PageUp' })
  check(H.following.value === false, '⑰a PageUp ⇒ 停止跟随', `following=${H.following.value}`)
  const el2 = makeEl({ scrollHeight: 2000, clientHeight: 400, scrollTop: 1600 })
  const H2 = makeHost(el2)
  H2.onMessagesKeydown({ key: 'ArrowUp' })
  check(H2.following.value === false, '⑰b ArrowUp ⇒ 停止跟随', `following=${H2.following.value}`)
}

// ⑱ 拖动滚动条往上（这类滚动**没有** wheel 事件）⇒ 必须也能停
{
  const el = makeEl({ scrollHeight: 2000, clientHeight: 400, scrollTop: 1600 })
  const H = makeHost(el)
  await H.followBottom()
  H.onMessagesScroll()                            // 我们自己那次滚动的补发事件 ⇒ 不该改变状态
  check(H.following.value === true, '⑱a 程序滚动的补发事件不影响状态', `following=${H.following.value}`)
  el.scrollTop = 900                              // 用户拖滚动条往上
  H.onMessagesScroll()
  check(H.following.value === false, '⑱b ★ 用户拖动滚动条往上 ⇒ 停止跟随', `following=${H.following.value}`)
  el.scrollTop = el.scrollHeight - el.clientHeight // 又拖回底部
  H.onMessagesScroll()
  check(H.following.value === true, '⑱c 拖回底部 ⇒ 恢复跟随', `following=${H.following.value}`)
}

// ⑲ 模板接线（v2 新增的用户意图通道）
{
  check(/@wheel\.passive="onWheel"/.test(SFC), '⑲a 容器绑定了 @wheel.passive="onWheel"')
  check(/@touchstart\.passive="onTouchStart"/.test(SFC) && /@touchmove\.passive="onTouchMove"/.test(SFC),
    '⑲b 容器绑定了触摸意图通道')
  check(/@keydown="onMessagesKeydown"/.test(SFC), '⑲c 容器绑定了键盘意图通道（onMessagesKeydown，避开输入框那个 onKeydown）')
}

// ⑳ 闭环：用户滚上去 → 内容继续长 → 点按钮 ⇒ 到底并继续跟随
{
  const el = makeEl({ scrollHeight: 1000, clientHeight: 400, scrollTop: 600 })
  const H = makeHost(el)
  await H.followBottom()
  H.onWheel({ deltaY: -10 })                      // 用户想看上文
  el.scrollHeight = 2400                          // 回答继续变长
  await H.followBottom()
  check(H.following.value === false && H.hasNewBelow.value === true,
    '⑳a 用户在看上文时回答继续变长 ⇒ 不动滚动条，只提示"有新内容"',
    `following=${H.following.value} label=${H.jumpLabel()}`)
  await H.jumpToBottom()
  check(el.scrollTop === el.scrollHeight - el.clientHeight && H.following.value === true,
    '⑳b 点「有新内容」⇒ 到底 + 恢复跟随', `scrollTop=${el.scrollTop}/${el.scrollHeight - el.clientHeight}`)
  el.scrollHeight = 3000
  await H.followBottom()
  check(el.scrollTop === el.scrollHeight - el.clientHeight,
    '⑳c 之后继续自动跟随（回到"没动滚轮就跟着输出"的默认行为）',
    `scrollTop=${el.scrollTop}/${el.scrollHeight - el.clientHeight}`)
}

console.log('')
console.log('合计 ' + pass + '/' + (pass + fails.length) + (fails.length ? '  ❌ 失败：' + fails.join(' | ') : '  ✅ 全部通过'))
process.exit(fails.length ? 1 : 0)