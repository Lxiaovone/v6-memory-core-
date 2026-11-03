# -*- coding: utf-8 -*-
"""V6 记忆核 · 终端演示

把三层串起来跑一遍，验证你 clone 下来是能用的：
    memory_manager（记忆门：存/取/衰减/归档）
  + brain_router（双脑：本地 Ollama 优先，失败降级云端）
  + v6_memory_gate.OutputGate（输出门：删指令泄漏、删禁词句、盖时间戳）

用法：
    python demo_cli.py              # 默认按 brain_mode.json 的挡位（auto 会自动降级云端）
    python demo_cli.py --no-cloud   # 强制只走本地，一分钱不花
    python demo_cli.py --stats      # 只看库统计，不进对话

依赖：pip install openai requests python-dotenv
环境：需要本地 Ollama 已启动（默认 http://127.0.0.1:11434/v1），模型名见 .env 的 MODEL_NAME。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import brain_router as br          # noqa: E402
import memory_manager as mm        # noqa: E402
from v6_memory_gate import OutputGate  # noqa: E402

HIST_KEEP = 3          # 带最近几轮（一轮=你说一句+它回一句）
HIST_CHARS = 120       # 单句上限，别把本地上下文撑爆


def _hist_messages(hist: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out = []
    for user, bot in hist[-HIST_KEEP:]:
        out.append(("user", user[:HIST_CHARS]))
        out.append(("assistant", bot[:HIST_CHARS]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cloud", action="store_true", help="只用本地模型，不花钱")
    ap.add_argument("--stats", action="store_true", help="只看库统计")
    args = ap.parse_args()

    if args.no_cloud:
        br.set_mode(br.MODE_LOCAL)
    print("挡位：%s（%s）" % (br.get_mode(), br.MODE_LABELS.get(br.get_mode(), "?")))
    try:
        print("记忆库：", mm.init())
    except Exception as e:
        print("记忆库初始化失败：%s" % e)
        return 1
    if args.stats:
        print(mm.stats())
        return 0

    og = OutputGate()
    hist: list[tuple[str, str]] = []
    print("可用命令：/stats 看库统计  /quit 退出\n")

    while True:
        try:
            text = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/quit", "/exit", "/q"):
            break
        if text == "/stats":
            print(mm.stats())
            continue

        # ① 检索记忆（失败不阻塞对话）
        mem = ""
        try:
            mem = mm.retrieve_memory(text) or ""
        except Exception as e:
            print("   [记忆检索失败，继续] %s" % e)
        clock = og.time_head()
        mem = (mem + clock) if mem.strip() else clock.lstrip()

        # ② 生成（本地优先，auto 挡会降级云端）
        reply = br.generate_reply(text, mem, history=_hist_messages(hist) or None)

        # ③ 输出门（泄漏/禁词/时间戳）
        clean, hit = og.clean(reply)
        if hit:
            print("   （输出门拦截：%r → %r）" % (reply[:40], clean[:40]))
        print("助手> %s" % clean)

        # ④ 落记忆 + 短期上下文
        hist.append((text, clean))
        hist = hist[-HIST_KEEP:]
        try:
            mm.save_memory("%s：%s\n%s：%s" % (mm.OWNER_NAME, text, mm.ASSISTANT_NAME, clean))
        except Exception as e:
            print("   [写记忆失败] %s" % e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
