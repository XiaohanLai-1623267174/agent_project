"""[零 API] `.env` 的唯一解析点 —— 替代全库 50 处硬编码路径的 load_dotenv。

为什么要有这个文件（§19 / llm_roles.py 文件头同款诊断）：
    历史上的写法是：几十个脚本各自 load_dotenv，路径写法还不统一，
    于是同一个 key 在不同脚本里有的读得到、有的读不到 —— 靠「记得写一样」维持的结构必然出错。
    "同一个东西在多处各写一遍、靠记得写一样来维持" —— 结构上必然出错。

本模块的做法：**一个顺序确定的加载链 + 变量级合并**。
    python-dotenv 默认 override=False，实测是【变量粒度】的：
    后加载的文件不会覆盖已存在的变量，但会补上缺失的变量。
    所以按顺序加载多份 .env 可以得到【并集】，且优先级 = 加载顺序。

用法（唯一推荐写法）：
    from env_bootstrap import load_env
    load_env()                      # 幂等，可重复调用
    # 想核对读了什么：
    load_env(verbose=True)          # 打印每个变量来自哪份文件

★ 必须在【导入 llm_roles / langchain_project 之前】调用 ——
   它们在模块级就 os.getenv，晚调用无效。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from dotenv import dotenv_values, load_dotenv
except ImportError:                                  # pragma: no cover
    raise SystemExit("需要 python-dotenv：pip install python-dotenv")

BENCH = Path(__file__).resolve().parent

# 加载顺序 = 优先级（先加载的赢）。用 DSH_ENV_FILES 可覆盖（os.pathsep 分隔）。
_DEFAULT_ENV_FILES = [
    # 服务端代码目录下的 .env（不进版本库；部署时把真实密钥放这里）
    BENCH / ".env",
]

_LOADED = False
_REPORT: list[tuple[str, list[str], list[str]]] = []   # (文件, 新设变量, 已存在被跳过)


def env_files() -> list[Path]:
    """本次实际使用的 .env 文件清单（尊重 DSH_ENV_FILES 覆盖）。"""
    override = os.getenv("DSH_ENV_FILES")
    if override:
        return [Path(p) for p in override.split(os.pathsep) if p.strip()]
    return list(_DEFAULT_ENV_FILES)


def load_env(verbose: bool = False) -> bool:
    """按固定顺序加载所有存在的 .env。幂等。返回是否加载了至少一份。"""
    global _LOADED
    if _LOADED:
        if verbose:
            report()
        return bool(_REPORT)

    for path in env_files():
        if not path.exists():
            _REPORT.append((str(path), [], ["<文件不存在>"]))
            continue
        try:
            want = dotenv_values(path)              # 只解析，不写环境
        except Exception as exc:                    # noqa: BLE001
            _REPORT.append((str(path), [], ["<解析失败 %s>" % type(exc).__name__]))
            continue
        before = set(os.environ)
        load_dotenv(path, override=False)          # 变量级：不覆盖已有值
        after = set(os.environ)
        skipped = [k for k in (want or {}) if k in before]
        _REPORT.append((str(path), sorted(after - before), sorted(skipped)))

    _LOADED = True
    if verbose:
        report()
    return any(new for _, new, _ in _REPORT)


def report() -> None:
    """打印"每个变量来自哪份 .env"—— 用于核对，不打印任何 key 的值。"""
    print("[env] .env 解析报告（只列变量名，不打印值）", flush=True)
    for path, new, skipped in _REPORT:
        if skipped == ["<文件不存在>"]:
            print("   · %s  —— 不存在，跳过" % path, flush=True)
            continue
        print("   · %s" % path, flush=True)
        print("       新设: %s" % (", ".join(new) if new else "（无）"), flush=True)
        if skipped:
            print("       被跳过(已存在): %s" % ", ".join(skipped), flush=True)
    missing = [k for k in ("QW_API_KEY", "MS_API_KEY") if not os.getenv(k)]
    if missing:
        print("   ⚠️ 仍缺失: %s" % ", ".join(missing), flush=True)


def resolved_names() -> dict[str, bool]:
    """关心的变量是否已解析（不含值）。"""
    return {k: bool(os.getenv(k)) for k in
            ("QW_API_KEY", "SENSENOVA_API_KEY", "MS_API_KEY", "MS_API_KEY_2")}


# import 即生效 —— 但推荐显式调用以便控制时机
if os.getenv("ENV_BOOTSTRAP_NO_AUTO") != "1":
    load_env()


if __name__ == "__main__":
    # 作为脚本运行：python env_bootstrap.py  → 打印解析报告，供核对
    load_env(verbose=True)
    print("\n[env] 关心的变量：")
    for k, ok in resolved_names().items():
        print("   %-20s %s" % (k, "✅ 有" if ok else "🔴 无"))
    print("\n[env] 加载顺序（先者优先）：")
    for p in env_files():
        print("   %s  %s" % ("✅" if p.exists() else "·", p))
    sys.exit(0)