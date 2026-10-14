/**
 * 装置用的路径解析器 —— 让这些验收装置在【两种布局】下都能跑。
 *
 * 为什么要它：这几个装置默认去 `../kb_bench/<文件>` 找后端源码与抓包夹具，那是【原始开发布局】
 * （后端在 `kb_bench\`、前端在 `rag_frontend\`）。但本仓库里后端就在旁边的 `../server/`，
 * 于是「克隆下来跑不了」——而且失败原因是 `FileNotFoundError`，看起来像项目坏了。
 *
 * 解析顺序：
 *   ① 环境变量显式指定（装置各自的 DEBUG_PANEL_* / *_VUE 等覆盖，优先级最高）
 *   ② `../kb_bench`（开发布局）
 *   ③ `../server`（本仓库布局）
 *   夹具再退一层：④ `./_fixtures`（随仓库发布的【合成】夹具）
 *
 * 为什么夹具要有合成版：真实抓包（`_sse_body.txt`）里含用户所在学校名、上传文档名与制度原文，
 * 属于用户内容 ⇒ 不进公开仓库。合成夹具字段结构完全一致、内容全是假的，见 frontend/README.md。
 */
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const DEV_KB = process.env.DEVICE_KB_DIR || join(HERE, '..', 'kb_bench')
const DEV_SERVER = process.env.DEVICE_SERVER_DIR || join(HERE, '..', 'server')
const FIXTURE_DIR = process.env.DEVICE_FIXTURE_DIR || join(HERE, '_fixtures')

// 合成夹具的对应名（真实抓包叫 _sse_body.txt，随仓库发布的那份带 synthetic 前缀）
const SYNTHETIC = {
  '_sse_body.txt': 'synthetic_sse_body.txt',
  '_sse_expect.json': 'synthetic_sse_expect.json',
}

/** 后端源码所在目录：开发布局优先，其次本仓库布局。 */
export function backendHome() {
  for (const d of [DEV_KB, DEV_SERVER]) {
    if (existsSync(join(d, 'config.py'))) return d
  }
  return DEV_KB // 都找不到时返回开发布局路径，让报错指向原始位置而不是一个凭空造的路径
}

/** 后端某个源文件：override 是该装置原有的环境变量覆盖。 */
export function backendFile(name, override) {
  if (override) return override
  for (const d of [DEV_KB, DEV_SERVER]) {
    const p = join(d, name)
    if (existsSync(p)) return p
  }
  return join(DEV_KB, name)
}

/** 抓包夹具：开发布局的真实抓包优先，否则用随仓库发布的合成夹具。 */
export function fixtureFile(name) {
  const dev = join(DEV_KB, name)
  if (existsSync(dev)) return dev
  return join(FIXTURE_DIR, SYNTHETIC[name] || name)
}

/** 这份夹具是不是合成的（装置会把它打出来，避免把合成夹具当成真实抓包的证据）。 */
export function isSyntheticFixture(path) {
  return String(path).includes(`${'fixtures'}`)
}