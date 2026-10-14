# 校园奖学金制度 RAG 知识库（多租户）

面向校内奖学金 / 综合测评制度文件的问答系统：用户上传制度 PDF，系统解析、切块、入库；提问时走「检索 → 重排 → 生成」，并在回答里给出可点击的引用来源。

服务端不托管前端：后端只提供 API（实测 `fastapi_lianxi.py` 里没有 `StaticFiles`/`mount`），前端是独立应用。

## 组成（三个进程，各自独立启动）

| 组件 | 端口 | 作用 |
|---|---|---|
| `server/` | `127.0.0.1:8000` | FastAPI 业务 API。SQLite（SQLAlchemy）+ Chroma 向量库 |
| `frontend/` | `[::1]:5173` | Vue 3 + Vite 聊天界面（含「知识库搜索」「联网搜索」两个开关） |
| `rerank/` | `127.0.0.1:8090` | 本地重排服务（`bge-reranker-v2-m3`），可选：不装则把 `RERANK_BACKEND` 设为 `cloud` 或 `off` |

## 目录

```
server/                     FastAPI 服务端（扁平 import，必须整目录一起跑）
  fastapi_lianxi.py         入口：HTTP 层（9 个接口）
  langchain_project.py      检索与生成主链路（embedding / chroma / rerank / BM25）
  config.py                 ★ 路径与模型角色的唯一来源
  database.py               SQLAlchemy 模型与会话
  auth.py                   注册 / 登录 / JWT
  llm_roles.py              按角色拿模型 + 重试 + 降级计数
  key_pool.py               多 key 轮换池（不碰 .env、不做 io）
  env_bootstrap.py          ★ .env 的唯一起点
  websearch.py              联网搜索（无 key：结果页 HTML 解析 + 超时/限流/字符硬截断）
  conversation_context.py   给模型看的上文（含 user_id 归属校验）
  agent/                    Agent 与工具（含上传目录越权防护）
  skills/knowledge_ingestion/  入库管线：PDF → Markdown → chunks JSON → 入库
  parse_guard.py            解析链路的护栏（已写好，尚未接进入库管线）
  mineru_tables.py          MinerU 表格规范化（已写好，尚未接进入库管线）
rerank/                     本地重排服务（torch，依赖重，与 server 分开装）
frontend/                   Vue 3 前端 + 一组零依赖的验收装置（`_*_test.mjs` / `_*_mutate.mjs`）
docs/初始化说明.md          从克隆到第一次问答的完整步骤
```

## 快速开始

```bash
# 1) 密钥：模板里没有任何真实 key
cp .env.example server/.env            # 填 QW_API_KEY / MS_API_KEY 等

# 2) 服务端依赖
pip install -r requirements.txt
cd server && python -m uvicorn fastapi_lianxi:app --host 127.0.0.1 --port 8000

# 3) 前端（新开一个终端）
cd frontend && npm install && npm run dev
# ★ Vite 监听 IPv6：[::1]:5173 —— 请用 http://localhost:5173/ 访问。
#   用 127.0.0.1:5173 会连不上，这不是前端没启动。

# 4) 本地重排（可选，新开一个终端）
cd rerank && pip install -r requirements.txt
# 把 bge-reranker-v2-m3 放到 rerank/models/ 或用 RERANK_MODEL_PATH 指定
python rerank_server.py
```

细节与首次入库流程见 `docs/初始化说明.md`。

## 接口（9 个，取自运行中的 `openapi.json`）

| 路径 | 方法 |
|---|---|
| `/auth/register` `/auth/login` | POST |
| `/conversations` | GET, POST |
| `/conversations/{conv_id}` | GET, PUT, DELETE |
| `/chat` | POST（SSE 流式，含 `event: sources` 帧） |
| `/documents` | GET, POST |
| `/documents/{document_id}` | DELETE |
| `/agent` | POST |
| `/health/degradations` | GET（降级计数） |

## 关键设计（都是被具体故障逼出来的）

- **路径唯一来源**：`config.py` 集中所有路径常量。起因是同一个 Chroma 目录在两个模块里写成相对 / 绝对两种字符串，而 chromadb 以路径字符串为键复用进程内 System，导致同一磁盘目录被当成两套索引，出现「入库后立刻检索必报 `Error finding id`」。
- **`.env` 唯一解析点**：`env_bootstrap.py`。起因是几十个脚本各自 `load_dotenv` 且路径写法不一，同一个 key 在不同脚本里有读得到、有读不到。
- **降级必须可见**：`/health/degradations` 把降级计数暴露出来，不让失败静默成「点了没反应」。
- **多租户隔离**：上传与文档按 `user_id` 分开；Agent 的入库工具只接受解析后位于该用户上传目录内的路径，否则一个用户就能让 Agent 去读服务器上任意文件。
- **联网搜索无需 key**：只吃搜索结果摘要，不做页面正文抓取；受超时、每分钟调用上限与 prompt 块字符硬截断约束。
- **模型分角色**：`config.py` 的 `ROLE_MODELS` 里 7 个角色（`classify` / `title` / `rewrite` / `answer` / `direct` / `clean` / `metadata`），换后端改一行或由 `.env` 覆盖。
- **前端验收装置**：`frontend/` 下的 `_*_test.mjs` 是零依赖的断言装置，`_*_mutate.mjs` 是反向对照（故意把源码改坏，验证对应断言真的会红）。请在 `frontend/` 目录下运行它们。

## 本仓库有意不包含

- **任何密钥**：`.env` 系列一律不进（只提供 `.env.example`）。
- **运行期数据**：`app.db`、`.chroma_db/`、`uploads/`、`knowledge_markdown/`。其中 `app.db` 含真实用户的密码哈希与会话内容。实测这些数据**缺失时服务端能自举**：启动即自动创建 `app.db` 与 `.chroma_db/`。
- **开发期的实验与诊断脚本**、内部交接文档。

## 已知限制（如实列出）

1. **联网搜索不能保证「最新」**：它依赖无 key 的搜索结果页解析。实测结论——(a) 结果页里的发布时间可提取且与页面权威标记逐日一致；(b) 但两家引擎的**服务端时间过滤都无效**（一个返回空页、一个结果集与排序完全不变），只能在已拿到的候选里按时间筛；(c) 无 key 源**没有可用性保证**，连续请求后会被反爬（实测页面从 554 KB 缩到 5.4 KB 并出现验证标记）。因此「保证最新」需要接入带结构化发布时间与时间范围过滤的正式搜索 API。
2. **对话记忆有轮数上限**，超出后早期事实会「看不到」。
3. **本地重排需要自备模型**（`bge-reranker-v2-m3`，fp16 约 1.1 GB 显存）；不装则用云端重排或关闭。
4. **首次入库需要可用的 LLM 额度**：清洗与 chunk 元数据两环会调用模型，额度不足时上传会失败（而聊天仍正常，容易误判为「只有上传坏了」）。
5. `requirements.txt` 的版本取自开发环境（Python 3.14.6 / Node v24.20.0），**尚未在干净环境复核**。