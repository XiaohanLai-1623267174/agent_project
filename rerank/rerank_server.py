"""本地 rerank 服务 —— 裸 transformers 实现，替代 DashScope TextReRank.call。

为什么不用 FlagEmbedding（对比后改用 transformers）:
  FlagEmbedding 会拖进 datasets/peft/accelerate 等 30 个包，并把已验证的
  torch 2.12.0+cu126 换成别的版本。我们只需要对一个 0.6B cross-encoder
  做前向推理 —— transformers 就够了（官方对该模型的正规用法也是裸 transformers）。

设计要点（全部来自 SESSION_HANDOFF.md 的实测教训）:

1. 契约对齐：/rerank 返回 dashscope 风格的 results=[{index, relevance_score}]，
   项目侧只换 `TextReRank.call` 那一处（§25.7 指定的接入点）。

2. sigmoid 归一化 → [0,1]，与现有 relevance_score 同一语义空间。
   不归一化的话 bge 输出 [-11,+7] 量级原始 logit，任何基于 0.9~1.0 的阈值
   讨论都会变成 §23.1 式的静默错乱。

3. 失败必须可见（§23.1 教训）：异常 → 非 200 + 结构化错误，并计入 degraded，
   绝不静默退回"未重排但看起来正常"。

4. 截断必须记账：本项目 chunk 最长 3,743 字符。本实现**默认不截断**，
   按请求内实际 token 数动态选长度（上限 = 模型 max_position_embeddings），
   若被迫截断则统计篇数并写进响应与日志 —— 长 chunk 尾部内容绝不静默丢失。

5. 显存友好：fp16 权重 + 按 token 预算组批，避免一次把 43 篇长文档全塞进显存。
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator
from transformers import AutoModelForSequenceClassification, AutoTokenizer
import uvicorn

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
MODEL_PATH = os.environ.get(
    "RERANK_MODEL_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "bge-reranker-v2-m3"),
)
HOST = os.environ.get("RERANK_HOST", "127.0.0.1")
PORT = int(os.environ.get("RERANK_PORT", "8090"))
USE_FP16 = os.environ.get("RERANK_FP16", "1") not in ("0", "false", "False")
# 动态批处理：按 token 预算组批，避免长短文档混批浪费显存
TOKEN_BUDGET = int(os.environ.get("RERANK_TOKEN_BUDGET", "32768"))
MAX_BATCH = int(os.environ.get("RERANK_MAX_BATCH", "32"))
# 超过此 token 数的文档单独成批（它会把同批其它文档都 padding 到这个宽度）
LONG_DOC_TOKENS = int(os.environ.get("RERANK_LONG_DOC_TOKENS", "512"))
# 硬上限；0 = 用模型 config 的 max_position_embeddings
MAX_LENGTH_OVERRIDE = int(os.environ.get("RERANK_MAX_LENGTH", "0"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("rerank")

STATE: dict[str, Any] = {
    "model": None, "tokenizer": None, "device": None, "dtype": None,
    "max_pos": None, "load_s": None, "warmup_ms": None,
    "requests": 0, "degraded": 0, "truncated_total": 0, "docs_total": 0,
}


def _load() -> None:
    t0 = time.time()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    if dev == "cpu":
        log.error("!! CUDA 不可用 —— 将回落到 CPU，延迟会很差")
    else:
        p = torch.cuda.get_device_properties(0)
        log.info("GPU: %s  %.2f GB  cc=%s", torch.cuda.get_device_name(0),
                 p.total_memory / 1024**3, torch.cuda.get_device_capability(0))

    dtype = torch.float16 if (USE_FP16 and dev == "cuda") else torch.float32
    # 本地文件完整，禁止联网：tokenizer_config.json 里写的是慢版 XLMRobertaTokenizer
    # （需 spiece.model，本仓库没有），若允许联网 transformers 会去 HF 拉取修复文件，
    # 而 HF 在本网络只有 ~137 KB/s → 初始化会卡死数分钟。显式用 fast 类 + 离线。
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    from transformers import XLMRobertaTokenizerFast
    tok = XLMRobertaTokenizerFast.from_pretrained(MODEL_PATH)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_PATH, torch_dtype=dtype)
    model = model.to(dev).eval()

    max_pos = int(getattr(model.config, "max_position_embeddings", 512))
    STATE.update({
        "model": model, "tokenizer": tok, "device": dev, "dtype": dtype,
        "max_pos": MAX_LENGTH_OVERRIDE or max_pos,
        "load_s": time.time() - t0,
    })
    log.info("模型加载完成 %.1fs  device=%s dtype=%s  max_position_embeddings=%d"
             " 生效上限=%d", STATE["load_s"], dev, dtype, max_pos, STATE["max_pos"])

    # 预热：吃掉 cuDNN/显存分配的一次性成本
    t1 = time.time()
    _score("预热查询", ["预热文档内容"] * 8)
    STATE["warmup_ms"] = (time.time() - t1) * 1000
    log.info("预热完成 %.1f ms", STATE["warmup_ms"])


def _score(query: str, documents: list[str],
           max_length: int | None = None) -> tuple[list[float], int]:
    """返回 (sigmoid 分数列表, 被截断的篇数)。"""
    tok, model, dev = STATE["tokenizer"], STATE["model"], STATE["device"]
    if model is None:
        raise RuntimeError("模型未加载")

    pairs = [[query, d] for d in documents]
    # 先不截断编码，看这批的实际 token 长度
    raw = tok(pairs, padding=False, truncation=False, add_special_tokens=True)
    lens = [len(x) for x in raw["input_ids"]]
    limit = max_length or STATE["max_pos"]
    truncated = sum(1 for x in lens if x > limit)

    # ------------------------------------------------------------------
    # 分组策略（由 _diag_strategies.py 实测决定）:
    #   耗时 ∝ 批内宽度 × 篇数（显存带宽限制，不是算力）。
    #   一块 3674 字符的长文档若与短文档同批，会把整批 padding 到 2677 宽
    #   → 75% 浪费、慢 4.2 倍（43 篇 1465ms → 346ms）。
    #   正确做法：长块单独成批（不拖宽别人）+ 短块宽批次（吃满并行）。
    #   反例：每篇单独跑虽然 padding 浪费为 0，却因并行度低而更慢（1676ms）。
    # ------------------------------------------------------------------
    long_idx = [i for i in range(len(pairs)) if lens[i] > LONG_DOC_TOKENS]
    short_idx = sorted((i for i in range(len(pairs)) if lens[i] <= LONG_DOC_TOKENS),
                       key=lambda i: lens[i])

    out = [0.0] * len(pairs)

    def run_batch(idx: list[int]) -> None:
        batch = [pairs[j] for j in idx]
        width = min(max(lens[j] for j in idx), limit)
        with torch.no_grad():
            enc = tok(batch, padding=True, truncation=True, max_length=width,
                      return_tensors="pt").to(dev)
            logits = model(**enc, return_dict=True).logits.view(-1).float()
            sc = torch.sigmoid(logits).cpu().tolist()
        for k, j in enumerate(idx):
            out[j] = sc[k]

    for i in range(0, len(short_idx), MAX_BATCH):
        run_batch(short_idx[i:i + MAX_BATCH])
    for j in long_idx:                      # 长块各自单独一批
        run_batch([j])

    return out, truncated


# ----------------------------------------------------------------------------
class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    top_n: int | None = None
    return_documents: bool = False
    max_length: int | None = None

    @field_validator("documents")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("documents 不能为空")
        return v


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load()
    yield


app = FastAPI(title="Local Rerank Service (transformers)", version="1.1",
              lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {
        "ok": STATE["model"] is not None,
        "model_path": MODEL_PATH,
        "device": STATE["device"],
        "dtype": str(STATE["dtype"]),
        "max_length_effective": STATE["max_pos"],
        "load_s": STATE["load_s"],
        "warmup_ms": STATE["warmup_ms"],
        "requests": STATE["requests"],
        "degraded": STATE["degraded"],
        "docs_total": STATE["docs_total"],
        "truncated_docs_total": STATE["truncated_total"],
        "vram_allocated_mb": (round(torch.cuda.memory_allocated() / 1024**2, 1)
                              if STATE["device"] == "cuda" else None),
    }


@app.post("/rerank")
def rerank(req: RerankRequest) -> dict:
    STATE["requests"] += 1
    t0 = time.time()
    try:
        scores, truncated = _score(req.query, req.documents, req.max_length)
    except Exception as e:                                   # noqa: BLE001
        STATE["degraded"] += 1
        log.error("[rerank] 失败 query=%r docs=%d: %s: %s",
                  req.query[:40], len(req.documents), type(e).__name__, e)
        raise HTTPException(status_code=500, detail={
            "error": f"{type(e).__name__}: {e}",
            "degraded_count": STATE["degraded"],
            "hint": "本地重排失败，调用方必须显式报错，不要静默退回未重排结果",
        }) from e

    STATE["docs_total"] += len(req.documents)
    STATE["truncated_total"] += truncated
    elapsed = (time.time() - t0) * 1000

    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    results = [{"index": i, "relevance_score": scores[i]} for i in order]
    if req.top_n:
        results = results[: req.top_n]

    log.info("[rerank] docs=%d top_n=%s 用时 %.1f ms 分数[%.4f..%.4f] 截断=%d",
             len(req.documents), req.top_n, elapsed, min(scores), max(scores),
             truncated)
    if truncated:
        log.warning("[rerank] %d 篇超过上限被截断", truncated)

    out: dict[str, Any] = {
        "results": results,
        "latency_ms": round(elapsed, 2),
        "docs": len(req.documents),
        "truncated": truncated,
        "model": os.path.basename(MODEL_PATH),
        "normalized": True,          # sigmoid → [0,1]
    }
    if req.return_documents:
        out["documents"] = req.documents
    return out


@app.get("/")
def index() -> dict:
    return {"service": "local rerank (transformers)",
            "endpoints": ["/health", "/rerank"]}


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")