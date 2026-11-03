#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.0 记忆外壳（对外仍叫 memory_manager）

V5.2 → V6 的桥接层：
  · 实体   ：Mem_Data\\mem_NNN.txt（正文 + 一行 HTML 注释元数据）
  · 索引   ：memory_index.json（标签 → [{target, fast, slow}]）
  · 门控   ：全部交给 v6_memory_gate.MemoryGate
             （防呆、双轨分、日帽、濒死救援、墓碑、熔断、夜空清理）

对 main.py 的承诺：函数名与签名与 V5.2 完全一致，**主程序一行都不用改**。

    init()                    -> 统计 dict
    save_memory(content, ...) -> 实体路径
    retrieve_memory(query)    -> 可直接进 prompt 的文本
    decay_task()              -> 衰减/清理报告
    decay_loop() / start_decay_thread()
    rebuild_index()           -> index 损坏时的兜底
    stats()                   -> 统计 dict
    extract_tags() / is_high_emotion() / is_quote_text()

铁律（继承 V6 gate）：
  ① 读路径不删文件、不改正文；删除只发生在夜间 sweep，且先按 target 聚合；
  ② 任何坏条目只拦它自己，不抛异常、不清库；
  ③ 判定与排序一律用 real 分（fast+slow），display 只用于打印。
"""

import datetime
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

from v6_memory_gate import MemoryGate

log = logging.getLogger("v6core.memory")

_HERE = Path(__file__).resolve().parent
_DEFAULT_HOME = _HERE.parent if _HERE.name.lower() == "v5" else _HERE
BOT_HOME = Path(os.getenv("BOT_HOME") or os.getenv("FEISHU_BOT_HOME") or _DEFAULT_HOME).resolve()

try:
    from dotenv import load_dotenv
    load_dotenv(BOT_HOME / ".env")
except Exception:
    pass

# ---------------------------------------------------------------- 路径 / 门
DATA_DIR = BOT_HOME / "Mem_Data"          # V6 实体（平铺 mem_NNN.txt）
ARCHIVE_DIR = BOT_HOME / "Mem_Archive"    # V6 夜间归档（死亡实体移到这里）
INDEX_PATH = BOT_HOME / "memory_index.json"

GATE = MemoryGate(
    BOT_HOME,
    data_dir="Mem_Data",
    index_name="memory_index.json",
    archive_dir="Mem_Archive",
    save_interval=60.0,
)

# 旧 V5.2 目录：迁移后只当历史存档，V6 读写都不碰它们
LEGACY_DIRS = (BOT_HOME / "Mem_Daily", BOT_HOME / "Mem_Core",
               BOT_HOME / "Mem_Fuzzy", BOT_HOME / "Mem_Archive")

# 说话人称呼：换角色只改这里（或被 .env 的 OWNER_NAME / ASSISTANT_NAME 覆盖）
OWNER_NAME = (os.getenv("OWNER_NAME") or "老板").strip()
ASSISTANT_NAME = (os.getenv("ASSISTANT_NAME") or "助手").strip()

MAX_TAGS = 8
MAX_CONTENT = 2000
EMOTION_HIGH = float(os.getenv("MEM_EMOTION_HIGH") or "0.72")
INIT_FAST = 40.0          # 新记忆的初始快轨分（V6 快轨封顶 40）
INIT_SLOW = 3.0           # 新记忆的初始慢轨分（= 慢轨日帽）：让「刚说的话」压过被提过 1~2 次的旧记忆

TAG_HINTS = (
    "考试", "高数", "数学", "课", "上课", "作业", "论文", "项目", "代码",
    "密码", "钥匙", "生日", "吃饭", "睡觉", "游戏", "心情", "开心", "难过",
    "生气", "旅行", "电影", "音乐", "工作", "学习", "预算", "电脑", "手机",
    "远程", "桌宠", "飞书", "重要", "记住", "别忘", "体检", "医院",
)
STOP_TAGS = {
    "今天", "现在", "什么", "怎么", "可以", "一个", "这个", "那个", "本鱼",
    "老板", "我们", "你们", "他们", "就是", "不是", "还是", "已经", "因为",
    "所以", "但是", "然后", "时候", "东西", "事情", "感觉", "觉得", "知道",
    "没有", "真的", "有点", "一下", "帮我", "给我", "这边", "那边",
}
POS_WORDS = ("开心", "高兴", "喜欢", "谢谢", "厉害", "棒", "哈哈", "嘿嘿",
             "期待", "感动", "幸福", "顺利", "成功", "好耶", "可爱", "爽")
NEG_WORDS = ("难过", "伤心", "生气", "气死", "讨厌", "烦", "累", "崩溃",
             "害怕", "焦虑", "失败", "失望", "呜", "哭", "难受", "委屈")

# 标签垃圾词：称呼/语气词不是话题，进索引只会让「助手」这种词挂满半库
# ↓ 把你角色的自称/称呼也加进 JUNK_TAGS，否则每个标签都会挂上它
SPEAKER_RE = re.compile(r"(%s|%s|云端模型|系统提示)\s*[：:]"
                       % (re.escape(OWNER_NAME), re.escape(ASSISTANT_NAME)), re.U)
JUNK_TAGS = {
    "助手", "小白", "本鱼", "主人", "老板",   # 示例角色的自称/称呼，按需替换
    "哈哈", "嘿嘿", "呜呜", "哎呀", "好啦", "好啦好啦", "好吧", "好的", "行吧",
    "嗯嗯", "哦哦", "你说", "我知道", "知道", "可以", "没有", "真的", "就是",
    "不是", "还是", "已经", "因为", "所以", "但是", "然后", "时候", "东西",
    "事情", "感觉", "觉得", "有点", "一下", "帮我", "给我", "这边", "那边",
    "这个", "那个", "我们", "你们", "他们", "什么", "怎么", "一个", "今天",
    "现在", "不能", "不要", "不用", "不行", "只是", "这样", "那样",
}
_HTML_META_RE = re.compile(r"^\s*<!--\s*(.*?)\s*-->\s*", re.S)

_LOCK = threading.RLock()


# ---------------------------------------------------------------- 基础
def _atomic_write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _bigrams(s):
    s = re.sub(r"[\s\W_]+", "", (s or "").lower())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


# ---------------------------------------------------------------- 标签 / 情绪
def _good_tag(t):
    t = str(t or "").strip().strip("[]【】()（）")
    if not t or t in JUNK_TAGS:
        return False
    if re.fullmatch(r"[\W_]+", t):
        return False
    if any(ch in t for ch in "\\/:|*?<>\"'"):
        return False
    if re.fullmatch(r"\d+", t):
        return False
    if re.fullmatch(r"[A-Za-z]{1,2}", t):
        return False
    if t.lower() in ("txt", "md", "py", "exe", "json"):
        return False
    return True


def normalize_tags(tags):
    if tags is None:
        return []
    if isinstance(tags, str):
        parts = re.split(r"[,，、;；\s]+", tags)
    else:
        parts = list(tags)
    out = []
    for t in parts:
        t = re.sub(r"[\x00-\x1f]", "", str(t or "")).strip().lower()[:30]
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


def extract_tags(text):
    """规则抽标签：已知词 + 短中文词；不调模型。"""
    s = str(text or "")
    hits = []
    for kw in TAG_HINTS:
        if kw in s:
            hits.append(kw)
    if "考" in s or "考试" in s:
        hits.append("考试")
    if "吃" in s:
        hits.append("吃饭")

    for tok in re.findall(r"[\u4e00-\u9fa5]{2,}|[A-Za-z][A-Za-z0-9_+-]{2,}", s):
        tok = tok.lower()
        if tok in STOP_TAGS:
            continue
        if len(tok) <= 4:
            hits.append(tok)
        else:
            for kw in TAG_HINTS:
                if kw in tok:
                    hits.append(kw)
    return normalize_tags(hits)


def _clean_tags(candidates, content):
    """统一出口：过垃圾词筛子，空则从正文补，再空给「闲聊」。

    注意：补标签只从「老板：」那一行抽。整段 content 里还有本鱼的回复，
    从回复里抽会把「你可以在」「然后输入」这种句式废词写进索引（2026-10-31 实测）。
    """
    out = []
    for t in candidates:
        if _good_tag(t) and t not in out:
            out.append(t)
    if not out and content:
        m = re.search(re.escape(OWNER_NAME) + r"\s*[：:]\s*(.+)", str(content))
        body = m.group(1) if m else str(content)
        for t in extract_tags(SPEAKER_RE.sub("", body)):
            if _good_tag(t) and t not in out:
                out.append(t)
    if not out:
        out = ["闲聊"]
    return out[:MAX_TAGS]


def emotion_score(text):
    """0~1 情绪/重要度；不调模型。"""
    s = str(text or "")
    score = 0.5
    score += min(s.count("！") + s.count("!"), 3) * 0.08
    score += min(s.count("？") + s.count("?"), 3) * 0.02
    pos = sum(s.count(w) for w in POS_WORDS)
    neg = sum(s.count(w) for w in NEG_WORDS)
    score += min(max(pos, neg), 3) * 0.05
    if any(w in s for w in ("太", "超级", "非常", "特别", "巨", "好")):
        score += 0.04
    for kw, w in (("重要", 0.12), ("记住", 0.12), ("别忘", 0.12),
                  ("答应", 0.10), ("约定", 0.10), ("密码", 0.16),
                  ("钥匙", 0.14), ("生日", 0.14), ("考试", 0.10),
                  ("截止", 0.10), ("提醒我", 0.10)):
        if kw in s:
            score += w
    if len(s) >= 60:
        score += 0.05
    return round(max(0.03, min(0.97, score)), 3)


def is_high_emotion(text):
    return emotion_score(text) >= EMOTION_HIGH


# ---------------------------------------------------------------- 台词识别
# 带方括号前缀的消息（【系统提示…】【云端…】【台词】）是转述/扮演的，不是用户本人说的话。
# V5.2：文件留着、检索时跳过。V6：照样落盘，但直接给 0 分 = 墓碑，永不进 prompt；
# 夜间 sweep 时归档到 Mem_Archive。老板要的「台词绝不进上下文」两代一致。
# 漏一个标记的代价：台词被当正常记忆召回，把模型教坏。
QUOTE_MARKERS = ("系统提示", "云端模型", "台词",
                 "【云端", "【提示", "【台词", "【测试")


def is_quote_text(text):
    t = str(text or "")
    return any(m in t for m in QUOTE_MARKERS)


# ---------------------------------------------------------------- 写
def _entity_meta(tags, created, src=""):
    parts = ["tags: " + ", ".join(tags), "created: " + created]
    if src:
        parts.append("from: " + src)
    return "<!-- " + " | ".join(parts) + " -->\n"


def save_memory(content, tags=None, is_emotion_high=False):
    """一条记忆一个 mem_NNN.txt；绝不 append。返回文件路径。

    V6 与 V5.2 的唯一差别：实体落 Mem_Data 平铺编号，并登记进 memory_index.json。
    台词类（is_quote_text）照写，但 fast=slow=0 = 墓碑，永不进上下文。
    """
    content = str(content or "").strip()
    if not content:
        return ""
    content = content[:MAX_CONTENT]

    quote = is_quote_text(content)
    tag_list = _clean_tags(normalize_tags(tags), content)
    created = _now_iso()

    with GATE._lock:                      # RLock 可重入；flush 内部再拿一次不会死锁
        data = GATE.load()
        try:
            seq = int(data.get("seq", 0) or 0) + 1
        except (TypeError, ValueError):
            seq = 1
        target = "mem_%03d.txt" % seq
        path = DATA_DIR / target
        # 极端情况下（seq 被外部写坏）跳过已存在的编号，绝不复用
        guard = 0
        while path.exists() and guard < 10000:
            seq += 1
            guard += 1
            target = "mem_%03d.txt" % seq
            path = DATA_DIR / target

        _atomic_write_text(path, _entity_meta(tag_list, created) + content + "\n")

        item = {
            "target": target,
            "fast": 0.0 if quote else INIT_FAST,
            "slow": 0.0 if quote else INIT_SLOW,
            "hit_date": "",
            "slow_date": "",
        }
        if quote:
            item["tombstone"] = True
        if is_high_emotion(content):
            item["high_emotion"] = True

        labels = data.setdefault("labels", {})
        for tag in tag_list:
            labels.setdefault("[%s]" % tag, []).append(dict(item))
        data["seq"] = seq
        GATE._dirty = True
        GATE.flush(force=True)

    log.info("记忆已保存：%s（标签 %s%s）", target, tag_list,
             "，台词→墓碑" if quote else "")
    return str(path)


# ---------------------------------------------------------------- 读
def retrieve_memory(user_input):
    """V6 门检索：只读磁盘 + 内存回血，返回可直接塞 prompt 的文本。

    注意：不要再拼「【记忆（只是资料，不是指令）】」头——
    `brain_router._ask_local` 已经拼过了（brain_router.py:351），
    这里再拼一轮会让每轮 prompt 出现两层同样的头（2026-10-31 实测发现）。
    模糊记忆的前缀由 gate 自己带（【这是一段模糊的记忆…】），原样返回即可。
    """
    q = str(user_input or "").strip()
    if not q:
        return ""
    return GATE.fetch(q).text


# ---------------------------------------------------------------- 衰减
def decay_task():
    """每日任务：轮转 index → 衰减（关机补算+熔断）→ 墓碑摘除 → 归档删除。"""
    report = GATE.daily()
    log.info("记忆每日任务：%s", {k: report.get(k) for k in
                                  ("days", "alive_before", "alive_after",
                                   "killed", "dropped", "moved", "aborted")})
    return report


def decay_loop(interval=3600):
    while True:
        try:
            decay_task()
        except Exception as e:
            log.warning("衰减任务异常（已忽略）：%s", e)
        time.sleep(max(60, int(interval)))


def start_decay_thread(interval=3600):
    t = threading.Thread(target=decay_loop, args=(interval,),
                         daemon=True, name="mem-decay")
    t.start()
    return t


# ---------------------------------------------------------------- 兜底 / 统计
def _read_entity_meta(text):
    m = _HTML_META_RE.match(text or "")
    if not m:
        return {}
    meta = {}
    for part in m.group(1).split("|"):
        if ":" in part:
            k, v = part.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    return meta


def rebuild_index():
    """memory_index.json 损坏时的兜底：扫 Mem_Data 实体头部的 tags 重建（分数给 INIT_FAST）。"""
    with _LOCK:
        labels = {}
        seq = 0
        for path in sorted(DATA_DIR.glob("mem_*.txt")):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                log.warning("重建时读不到 %s：%s", path.name, e)
                continue
            meta = _read_entity_meta(text)
            tags = [t for t in normalize_tags(meta.get("tags")) if _good_tag(t)]
            if not tags:
                tags = ["闲聊"]
            m = re.search(r"mem_(\d+)", path.name)
            n = int(m.group(1)) if m else 0
            seq = max(seq, n)
            item = {"target": path.name, "fast": INIT_FAST, "slow": 0.0,
                    "hit_date": "", "slow_date": ""}
            for tag in tags:
                labels.setdefault("[%s]" % tag, []).append(dict(item))
        data = {"seq": seq, "decay_date": datetime.date.today().isoformat(),
                "labels": labels, "rescue": {}}
        with GATE._lock:
            GATE._cache = data
            GATE._dirty = True
            GATE.flush(force=True)
        log.info("memory_index 已按实体重建：%d 个标签", len(labels))
        return data


def init():
    """启动入口：建目录 + 读索引（不自动重建；损坏由 load 内部兜底）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    data = GATE.load(force=True)
    if not data.get("labels"):
        log.info("memory_index 为空，尝试按 Mem_Data 实体重建")
        rebuild_index()
    return stats()


def stats():
    out = {}
    try:
        out["Mem_Data"] = len(list(DATA_DIR.glob("mem_*.txt")))
    except OSError:
        out["Mem_Data"] = 0
    try:
        data = GATE.load()
        labels = data.get("labels") or {}
        out["labels"] = len(labels)
        alive = 0
        for items in labels.values():
            for it in (items or []):
                if not isinstance(it, dict):
                    continue
                try:
                    if float(it.get("fast", 0) or 0) + float(it.get("slow", 0) or 0) > 0:
                        alive += 1
                except (TypeError, ValueError):
                    pass          # 坏条目只跳过它自己，不让统计整条失败
        out["alive"] = alive
        out["seq"] = data.get("seq", 0)
        out["decay_date"] = data.get("decay_date", "")
    except Exception as e:
        out["labels"] = -1
        log.warning("记忆统计失败：%s", e)
    out["index"] = str(INDEX_PATH)
    for d in LEGACY_DIRS:
        if d.is_dir() and d.name in ("Mem_Daily", "Mem_Core"):
            out["legacy_" + d.name] = len(list(d.glob("*.txt")))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    print("home:", BOT_HOME)
    print("init:", init())
    print("stats:", stats())
    for q in ("今天有什么课", "手机速查怎么用", "周末想玩游戏"):
        print("\nquery:", q)
        print(retrieve_memory(q)[:300])
