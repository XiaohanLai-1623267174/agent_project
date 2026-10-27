/**
 * agent 会话状态（跨路由保留）—— §39.71
 *
 * 为什么需要这个文件：
 *   agent 页的对话记录原本是组件内 `ref`，切「对话 ⇄ 助手」时路由跳转会让组件卸载重建，
 *   于是记录整条消失；而 chat 页的历史是登录后从后端拉回来的，天然不会丢
 *   ⇒ 两页体验不一致，用户会以为「切回来把刚才的活弄丢了」。
 *
 * 为什么用模块级 ref，而不是 Pinia：
 *   本项目已有 `stores/auth.js` 这种「模块级单例」写法（D3：不擅自新增依赖），
 *   这几个状态只有 agent 页读写，不需要 devtools / 插件生态，一个模块足够。
 *   模块级 ref 在同一个 SPA 生命周期内只有一份实例 ⇒ 组件重建不影响它。
 *
 * 为什么不做 sessionStorage 持久化（刻意不做，不是漏了）：
 *   刷新后恢复会把 `running=true` 一起带回来——但此时**并没有真实的 SSE 流在跑**，
 *   界面会永久卡在「生成中」且无法再发消息，必须再补一套状态机兜底。
 *   本轮只修「切页丢失」这一个已确认的缺口，不顺手扩大边界。
 *   刷新后仍会清空：这条限制是已知的，已写进 §39.71。
 */
import { ref } from "vue"

/** 时间线：{role:'user'|'tool_call'|'tool_result'|'assistant'|'error', ...} */
export const items = ref([])

/** 多轮上下文：只存纯文本（user/assistant），回传给后端 `_agent_messages` 使用 */
export const history = ref([])

/** 工具返回 exists=true 时置为 {filename, existing}，用于渲染「再导一次 / 先不导了」按钮 */
export const existsInfo = ref(null)

/** 是否正在流式接收。跨路由保留是**必要**的：切走时流还在跑，
 *  切回来必须继续显示「生成中」，而不是放开输入框让用户重发一遍。 */
export const running = ref(false)

/** 上一轮耗时（ms），仅用于展示 */
export const elapsedMs = ref(0)

/**
 * 清空本次会话。
 *
 * ★ 一并清 `history`：原来的 `reset()` 只清时间线与耗时，**留着 history**，
 *   于是「清空」之后模型仍然会接到上一轮的 user/assistant 文本、接着旧话题回答
 *   —— 界面看起来清空了，上下文却没有。这是同一次状态整理里顺手修掉的缺陷。
 */
export function resetSession() {
  items.value = []
  history.value = []
  existsInfo.value = null
  elapsedMs.value = 0
}