/**
 * Unified HTTP request helper.
 * - Auto-attaches JWT token from localStorage
 * - Redirects to /login on 401
 * - Centralized baseURL
 */

const BASE_URL = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000"

function getToken() {
  return localStorage.getItem("auth_token")
}

async function request(path, options = {}) {
  const { method = "GET", body, headers = {}, ...rest } = options

  const token = getToken()
  if (token) {
    headers["Authorization"] = `Bearer ${token}`
  }

  const res = await fetch(`${BASE_URL}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...headers,
    },
    body: body ? JSON.stringify(body) : undefined,
    ...rest,
  })

  // Unauthorized → redirect to login
  if (res.status === 401) {
    localStorage.removeItem("auth_token")
    localStorage.removeItem("auth_username")
    window.location.href = "/login"
    throw new Error("未登录")
  }

  return res
}

/** JSON request (parsed body) */
export async function apiGet(path) {
  const res = await request(path)
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

export async function apiPost(path, body = {}, options = {}) {
  const res = await request(path, { ...options, method: "POST", body })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

export async function apiPut(path, body = {}, options = {}) {
  const res = await request(path, { ...options, method: "PUT", body })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

export async function apiDelete(path) {
  const res = await request(path, { method: "DELETE" })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

/**
 * 上传一份 PDF 到【当前用户】的知识库（后端会同步跑完解析入库）。
 *
 * ⚠️ 刻意不复用上面的 `request()`：它无条件把 `Content-Type` 写成
 *    `application/json`，而 multipart 必须让浏览器自己生成 boundary ——
 *    手写这个头会让后端解析不出 file 字段（FastAPI 直接 422）。
 *
 * ⚠️ 刻意【不】加超时 / AbortController：入库是同步重活
 *    （LLM 清洗 + 逐块元数据提取 + embedding），小文件也要数十秒，
 *    真正的 PDF 可能数分钟。前端不能先于服务端放弃。
 *
 * 后端失败时会回 400 + `detail`，里面是【真实原因】（例如模型额度耗尽的
 * 403 文本）——所以这里必须把 detail 原样抛出去给用户看，不能吞成
 * 一句"上传失败"。
 */
export async function uploadDocument(file, title) {
  const form = new FormData()
  form.append("file", file)
  if (title) form.append("title", title)

  const token = getToken()
  const headers = {}
  if (token) headers["Authorization"] = `Bearer ${token}`

  const res = await fetch(`${BASE_URL}/documents`, {
    method: "POST",
    headers,            // 不要设 Content-Type！
    body: form,
  })

  if (res.status === 401) {
    // 原本这里调用了【根本不存在的】handleUnauthorized()：上传遇到 401 会抛
    // ReferenceError，用户看到一句莫名其妙的报错、而且不会跳登录页。
    // 这里与上面 request() 的口径保持一致：清 token + 跳登录。
    localStorage.removeItem("auth_token")
    localStorage.removeItem("auth_username")
    window.location.href = "/login"
    throw new Error("登录已过期，请重新登录")
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

/**
 * SSE streaming request — returns a ReadableStream reader.
 * Caller is responsible for reading chunks.
 *
 * forceRag：用户点了「知识库搜索」⇒ 本条强制走 RAG（覆盖后端的 classify 自动判断）。
 * 默认 false ⇒ 请求体与改造前完全一致。
 * webSearch（§39.38）：用户点了「联网搜索」⇒ 本条去公开网页搜一次。
 *   与 forceRag **相互独立**（只查库 / 只联网 / 都要，三种组合都支持）。
 *   默认 false ⇒ 请求体与改造前完全一致（不开时后端 prompt 也逐字不变）。
 */
export function streamChat(query, conversationId, signal, forceRag = false, webSearch = false) {
  return fetch(`${BASE_URL}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${getToken()}`,
    },
    body: JSON.stringify({ query, conversation_id: conversationId,
                           force_rag: !!forceRag, web_search: !!webSearch }),
    signal,
  })
}

export { BASE_URL }


/**
 * 增量 SSE 解析器 —— 替代 ChatView 里"用正则把 data: 和换行全删掉"的写法。
 *
 * 原实现那一句
 *   （原实现是三个 replace 连删：先删掉 data: 前缀，再把换行全部删掉）
 * 有三个真问题：
 *   1) 删掉【所有】换行 => 段落/列表/表格结构在页面全部丢失。实测库里 8/25
 *      条助手消息含空行，UI 一律压成一行（库里存的是对的 => 存对了、显示错了）。
 *   2) 逐 chunk 跑正则，而 chunk 边界可能落在 data: 中间，漏配会残留 ta: 碎片。
 *   3) 答案里若出现字面量 data:（如 JSON 示例）会被静默删除。
 *
 * 正确做法是按规范【先切帧、再解字段】：空行分帧，帧内按 字段:值 解释，
 * 多条 data 用换行拼回。缓冲区跨 chunk 保留，一帧被网络切成几段也无碍。
 * 与后端 _sse_event() 的编码严格互逆。
 */


/** 解析一帧（帧 = 若干 "字段:值" 行），结果交给 onEvent */
function parseSseFrame(frame, onEvent) {
  let event = 'message'
  const dataLines = []

  for (const line of frame.split('\n')) {
    if (line === '' || line.startsWith(':')) continue // 空行 / 注释（心跳）忽略
    const idx = line.indexOf(':')
    const field = idx === -1 ? line : line.slice(0, idx)
    let value = idx === -1 ? '' : line.slice(idx + 1)
    // 刻意不剥冒号后的可选空格：后端从不输出，而模型分片常以空格开头，剥了会丢字符
    if (field === 'data') dataLines.push(value)
    else if (field === 'event') event = value
    // id / retry 当前用不到，忽略
  }

  if (dataLines.length === 0) return
  onEvent({ event, data: dataLines.join('\n') })
}


export function createSseParser(onEvent) {
  let buffer = ''
  // 上一个 chunk 以【孤立 \r】结尾（\r 和 \n 被网络切到了两个 chunk 里）。
  // 必须扣住等下一个 chunk 补全，否则会误判出一个帧边界。
  let crPending = false

  function drain() {
    let sep
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      parseSseFrame(frame, onEvent)
    }
  }


  return {
    push(chunk) {
      let s = chunk
      if (crPending) {
        if (s.startsWith('\n')) s = s.slice(1) // 吃掉 \r\n 里的 \n
        buffer += '\n' // 无论 \r\n 还是孤立 \r，被推迟的换行都要落地
        crPending = false
      }
      if (s.endsWith('\r')) { crPending = true; s = s.slice(0, -1) }
      buffer += s.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
      drain()
    },

    /** 流结束时把残留的不完整帧也交出去（服务端正常会补空行，这里只兜底） */
    flush() {
      if (crPending) { buffer += '\n'; crPending = false }
      const rest = buffer
      buffer = ''
      if (rest.trim()) parseSseFrame(rest, onEvent)
    },
  }
}
