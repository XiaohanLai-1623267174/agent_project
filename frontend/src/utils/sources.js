/**
 * 来源清单的显示规则（agent 页专用）。
 *
 * 为什么单独一个文件而不是写在组件里：这两个函数是纯字符串处理，
 * 放进组件就只能靠人眼验；抽出来之后 `public/_markdown_sanitize_device.html`
 * 能 import 到真函数做断言（含反向对照），比读代码可靠。
 */

/**
 * 把 `section_path` 的主干段去掉，只留章节。
 *
 * 为什么不显示第一段：`md_to_json.py` 把 PDF 的**标题层级**按原样存进 section_path，
 * 第一段就是文档自己的根标题 —— 简历的根标题就是人名「赖肖韩」。而文件名已经显示在
 * 来源行左边（`简历.pdf`），再显示一次根标题只会让人以为「来源串到别的文档了」
 * （用户原话：「来源里显示 赖肖韩，但是我已经重新上传过了呀」）。
 *
 * 只在还有子节点时去掉：否则整条会变空，那就什么信息都没了。
 */
export function cleanSectionPath(path) {
  if (!path) return ""
  const segs = String(path)
    .split("->")
    .map(s => s.trim())
    .filter(Boolean)
  if (segs.length >= 2) segs.shift()
  return segs.join(" -> ")
}

/**
 * 折叠状态下的来源摘要：主文件名 + 处数。
 *
 * 为什么需要它：来源清单常比答案本身还长（实测一次检索 5 条、每条带一段原文），
 * 默认展开会把回答淹掉；但完全折叠又看不出「依据来自哪」，所以留一行摘要。
 */
export function sourcesHint(list) {
  const arr = Array.isArray(list) ? list : []
  if (!arr.length) return ""
  const first = arr[0]?.filename || "未知文件"
  const sameFile = arr.every(s => (s?.filename || "未知文件") === first)
  return sameFile ? `${first} · 共 ${arr.length} 处` : `${first} 等 ${arr.length} 处`
}