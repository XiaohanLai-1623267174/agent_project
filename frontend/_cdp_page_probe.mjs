/**
 * [取证] 用 CDP 真驱动页面：两个常驻开关的页面级验收（§39.38.11 那条「机器验不了」的空白）。
 *
 * 为什么要这么查：前端装置只能在「抽出来的 JS + 假宿主」上断言，
 * 证明不了【真浏览器里按钮在不在、点了有没有反应、刷新还记不记得】。
 * 这个探针所做的事：真导航 → 真登录 → 真 click() → 读 aria-pressed/class/计算样式/localStorage
 * → 真刷新 → 再读一次 → 截图落盘。全程**不发任何消息** ⇒ 零 LLM 调用。
 *
 * ★ 必须由外部先起 Chrome（本探针只连 CDP，不 spawn 浏览器）：
 *   chrome --headless=new --remote-debugging-port=9333 --user-data-dir=<某个可写目录> about:blank
 *   理由：Chrome 的 Mojo IPC 在 Windows 走命名管道，受限沙箱会以 0x5 拒绝 ⇒ 必须用宽权限起。
 * ★ 站点必须是 localhost 而不是 127.0.0.1：Vite 只绑 localhost（§39.29.9）。
 */
import { writeFileSync } from 'node:fs'

const CDP_PORT = 9333
const APP = 'http://localhost:5173'
const API = 'http://127.0.0.1:8000'
const USER = 'pagecheck6076'
const PASS = 'pw123456'
const OUTDIR = process.cwd()   // 相对 cwd，不写死具体机器路径

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const checks = []
const check = (name, ok, detail = '') => {
  checks.push({ name, ok: !!ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  —— ' + detail : ''}`)
}

async function cdpTarget() {
  for (let i = 0; i < 30; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json()
      const page = list.find((t) => t.type === 'page')
      if (page?.webSocketDebuggerUrl) return page.webSocketDebuggerUrl
    } catch { /* chrome 还没起来 */ }
    await sleep(500)
  }
  throw new Error('找不到 CDP page target')
}

class Cdp {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.events = []
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result)
      } else if (msg.method) this.events.push(msg)
    })
  }
  send(method, params = {}) {
    const id = ++this.id
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.ws.send(JSON.stringify({ id, method, params }))
    })
  }
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
    if (r.exceptionDetails) throw new Error('eval 异常: ' + JSON.stringify(r.exceptionDetails).slice(0, 300))
    return r.result.value
  }
  async shot(file) {
    const r = await this.send('Page.captureScreenshot', { format: 'png' })
    writeFileSync(file, Buffer.from(r.data, 'base64'))
    return file
  }
}

// 读两个开关的「真状态」：不猜选择器 —— 直接从源码里确认过的类名取（.btn-kbsearch / .is-web）
const READ_TOGGLES = `(() => {
  const btns = [...document.querySelectorAll('.btn-kbsearch')].map(b => {
    const cs = getComputedStyle(b)
    return {
      text: b.innerText.trim(),
      cls: b.className,
      pressed: b.getAttribute('aria-pressed'),
      disabled: b.disabled,
      color: cs.color,
      border: cs.borderColor,
      bg: cs.backgroundColor,
    }
  })
  return {
    url: location.href,
    n: btns.length,
    btns,
    ls_web: localStorage.getItem('kb_web_search'),
    ls_force: localStorage.getItem('kb_force_search'),
    hasInput: !!document.querySelector('.input-wrapper'),
    hasSend: !!document.querySelector('.btn-send'),
    bodyLen: document.body.innerText.length,
  }
})()`

const url = await cdpTarget()
const ws = new WebSocket(url)
await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej) })
const cdp = new Cdp(ws)
await cdp.send('Page.enable')
await cdp.send('Runtime.enable')
await cdp.send('Log.enable')

// [1] 拿真 token（零 LLM：只是登录接口）
const lr = await fetch(`${API}/auth/login`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: USER, password: PASS }),
})
const { token } = await lr.json()
check('① 测试账号登录拿到 token', lr.status === 200 && !!token, `status=${lr.status} token_len=${token ? token.length : 0}`)

// [2] 先到本页 origin 才能写它的 localStorage
await cdp.send('Page.navigate', { url: `${APP}/login` })
await sleep(3000)
await cdp.eval(`localStorage.setItem('auth_token', ${JSON.stringify(token)});
  localStorage.setItem('auth_username', ${JSON.stringify(USER)});
  localStorage.removeItem('kb_web_search'); localStorage.removeItem('kb_force_search'); 'ok'`)

// [3] 进 /chat
cdp.events.length = 0
await cdp.send('Page.navigate', { url: `${APP}/chat` })
await sleep(5000)
const s0 = await cdp.eval(READ_TOGGLES)
check('② 页面真的渲染出来了（不是空壳）', s0.bodyLen > 50 && s0.hasInput && s0.hasSend,
  `bodyText=${s0.bodyLen} 字, 输入框=${s0.hasInput}, 发送键=${s0.hasSend}`)
check('③ 两个开关都在页面上（.btn-kbsearch 共 2 个：知识库搜索 + 联网搜索）', s0.n === 2,
  `实际 ${s0.n} 个: ${s0.btns.map(b => b.text).join(' / ')}`)
const web0 = s0.btns.find((b) => b.cls.includes('is-web'))
const kb0 = s0.btns.find((b) => !b.cls.includes('is-web'))
check('④ 联网搜索默认【关】（aria-pressed=false、无 on 类、localStorage 为空）',
  web0 && web0.pressed === 'false' && !/\bon\b/.test(web0.cls) && s0.ls_web === null,
  `pressed=${web0?.pressed} cls="${web0?.cls}" ls=${s0.ls_web}`)
check('⑤ 知识库搜索默认【开】（§39.31：默认走 RAG）',
  kb0 && kb0.pressed === 'true' && /\bon\b/.test(kb0.cls),
  `pressed=${kb0?.pressed} cls="${kb0?.cls}"`)
check('⑥ 两个开关视觉可区分（联网=暖琥珀、知识库=主色蓝）',
  web0 && kb0 && web0.color !== kb0.color,
  `联网 color=${web0?.color} / 知识库 color=${kb0?.color}`)

const shotA = await cdp.shot(`${OUTDIR}\\_cdp_shot_1_default.png`)

// [4] 真点「联网搜索」
const clicked = await cdp.eval(`(() => {
  const b = [...document.querySelectorAll('.btn-kbsearch')].find(x => x.className.includes('is-web'))
  if (!b) return 'NO_BUTTON'
  b.click(); return 'CLICKED'
})()`)
await sleep(600)
const s1 = await cdp.eval(READ_TOGGLES)
const web1 = s1.btns.find((b) => b.cls.includes('is-web'))
check('⑦ 点一下「联网搜索」按钮真的被点到并切到【开】',
  clicked === 'CLICKED' && web1?.pressed === 'true' && /\bon\b/.test(web1?.cls),
  `click=${clicked} pressed=${web1?.pressed} cls="${web1?.cls}"`)
check('⑧ 开关写进了 localStorage（kb_web_search=1）', s1.ls_web === '1', `ls_web=${s1.ls_web}`)
check('⑨ 点开后颜色真的变了（视觉反馈存在）', web1?.color !== web0?.color,
  `${web0?.color} → ${web1?.color}`)

// [5] 真刷新，验「跨刷新记住」
await cdp.send('Page.navigate', { url: `${APP}/chat` })
await sleep(5000)
const s2 = await cdp.eval(READ_TOGGLES)
const web2 = s2.btns.find((b) => b.cls.includes('is-web'))
check('⑩ 刷新之后仍然记得【开】（用户 2026-10-12 明确要求的那条）',
  web2?.pressed === 'true' && s2.ls_web === '1',
  `刷新后 pressed=${web2?.pressed} ls=${s2.ls_web}`)
const shotB = await cdp.shot(`${OUTDIR}\\_cdp_shot_2_afterswitch.png`)

// [6] 再点回【关】，并验脏值回退
await cdp.eval(`[...document.querySelectorAll('.btn-kbsearch')].find(x => x.className.includes('is-web')).click(); 'ok'`)
await sleep(500)
await cdp.eval(`localStorage.setItem('kb_web_search', 'yes'); 'ok'`)   // 故意塞脏值
await cdp.send('Page.navigate', { url: `${APP}/chat` })
await sleep(5000)
const s3 = await cdp.eval(READ_TOGGLES)
const web3 = s3.btns.find((b) => b.cls.includes('is-web'))
check('⑪ 脏值（yes）不让页面崩、且回退成【关】',
  web3 && web3.pressed === 'false' && s3.hasInput,
  `pressed=${web3?.pressed} 输入框仍在=${s3.hasInput}`)

// [7] 运行时报错
const errs = cdp.events.filter((e) => e.method === 'Runtime.exceptionThrown' ||
  (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error') ||
  (e.method === 'Log.entryAdded' && e.params.entry.level === 'error'))
const errTexts = errs.map((e) => e.method === 'Runtime.exceptionThrown'
  ? (e.params.exceptionDetails.exception?.description || e.params.exceptionDetails.text)
  : e.method === 'Runtime.consoleAPICalled'
    ? e.params.args.map((a) => a.description || a.value).join(' ')
    : String(e.params.entry.text))
check('⑫ 全程没有运行时异常 / console.error', errs.length === 0,
  errs.length ? errTexts.slice(0, 3).map((t) => String(t).slice(0, 160)).join(' | ') : '（无）')

const nOk = checks.filter((c) => c.ok).length
console.log()
console.log('截图:')
console.log('  ' + shotA)
console.log('  ' + shotB)
console.log('='.repeat(100))
console.log(`页面级验收 ${nOk}/${checks.length} 通过`)
console.log('='.repeat(100))
writeFileSync(`${OUTDIR}\\_cdp_page_probe.json`, JSON.stringify({ checks, s0, s1, s2, s3, shots: [shotA, shotB] }, null, 2), 'utf8')
ws.close()
process.exit(nOk === checks.length ? 0 : 1)