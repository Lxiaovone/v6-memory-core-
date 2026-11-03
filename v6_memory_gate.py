#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.0 MemoryGate —— 纯代码层的数据分流门（零模型算力）。

铁律
----
1. 读写分离：fetch() 只读磁盘，绝不删除、绝不改 TXT；回血只改内存，节流落盘。
   物理删除只发生在夜间 sweep()，且必须先按 target 聚合，确认全库无引用。
2. 内部/外部分数分离：
       real    = fast + slow          内部真值，fast 封顶 40，slow 无上限，可为 0
       display = clamp(real, 0, 100)  外部展示/喂模型
   判定（拦截/模糊）与排序一律用 real；display 只用于打印和日志，不参与门控。
3. 低段必须减法：fast * 0.8 永远到不了 0（会停在 1e-9），墓碑永远不触发。
   所以 fast <= 5 之后改减法，< 0.5 硬截断为 0。
4. 任何单个坏条目只拦它自己：不抛异常、不清库、不整库 fail。

参数全部集中在下面「参数区」，方便老板一个人调。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("v6.gate")

# ============================ 参数区 ============================
FAST_CAP: float = 40.0          # 快轨上限（短期热度，封顶就不涨）
DISPLAY_CAP: float = 100.0      # 外部分数上限
FUZZY_HIGH: float = 25.0        # real < 25 → 模糊/濒死区（下界是 0 开区间，不是 1）
HIT_FAST: float = 10.0          # 每次命中 +10（快轨）
HIT_SLOW: float = 1.0           # 每次命中 +1（慢轨，长期沉淀）
DAILY_FAST_CAP: float = 20.0    # 单条目每日快轨回血上限（防通胀第一道闸）
SLOW_DAILY_CAP: float = 3.0     # 单条目每日慢轨上限（防通胀第二道闸）
SLOW_DEDUP_SEC: float = 1800.0  # 慢轨去重窗口：30 分钟内同一记忆只 +1 次
RESCUE_FLOOR: float = 40.0      # 濒死救援：一次抬到 real = 40
RESCUE_PER_ITEM: int = 1        # 单条目每日救援次数
RESCUE_PER_LABEL: int = 1       # 单标签每日救援次数（老板要求：同一标签一天只回血一次）

FAST_DECAY_RATE: float = 0.8    # 快轨乘法段
FAST_SUB_FLOOR: float = 5.0     # 快轨转入减法段的分界
FAST_SUB_STEP: float = 2.0      # 快轨减法令
SLOW_DECAY_STEP: float = 1.0    # 慢轨每天 -1
ZERO_EPS: float = 0.5           # 归零硬截断
MAX_CATCHUP_DAYS: int = 14      # 关机最多补算 14 天
MAX_KILL_RATIO: float = 0.30    # 熔断：一次衰减干掉 >30% 活记忆就中止

FUZZY_PREFIX: str = "【这是一段模糊的记忆，请用不确定的语气回答】"
MAX_MEM_CHARS: int = 1200
SAVE_MIN_INTERVAL: float = 60.0 # 落盘节流（秒）
ROTATE_KEEP: int = 7

TARGET_RE = re.compile(r"^mem_\d{3,9}\.txt$")
_WS_RE = re.compile(r"\s+")
_HTML_HEADER_RE = re.compile(r"^\s*<!--.*?-->\s*", re.S)

STATE_DEAD = "dead"
STATE_FUZZY = "fuzzy"
STATE_NORMAL = "normal"


# ============================ 内部/外部分离 ============================
@dataclass(frozen=True)
class Score:
    """一条记忆的双轨分数。

    real 是唯一真值（无上限），display 是唯一的对外出口（[0,100]）。
    所有门控/排序必须用 real —— 用 display 排序会让 100 分以上的记忆全部并列。
    """

    fast: float = 0.0
    slow: float = 0.0

    @property
    def real(self) -> float:
        return float(self.fast) + float(self.slow)

    @property
    def display(self) -> float:
        # 注意：只用 min(real, 100) 是错的，slow 被压到 0 之前可能出现负值，
        # 负分喂给模型会变成「-3.0 分」这种脏数据，所以必须同时 clamp 下界。
        return max(0.0, min(self.real, DISPLAY_CAP))

    @property
    def state(self) -> str:
        r = self.real
        if r <= 0.0:
            return STATE_DEAD
        if r < FUZZY_HIGH:          # (0, 25) 全闭，不留 (0,1) 盲区
            return STATE_FUZZY
        return STATE_NORMAL

    @property
    def rank(self) -> float:
        """排序键：两轨各自归一化后加权。

        别直接拿 real 排序 —— slow 无上限，一条被反复提的旧记忆每年 +1000，
        会永久钉在 top_1，把新记忆全挤掉；也别拿 display 排序 —— 100 分以上全部并列。
        """
        return (0.6 * min(self.slow, DISPLAY_CAP) / DISPLAY_CAP
                + 0.4 * min(self.fast, FAST_CAP) / FAST_CAP)

    def as_tuple(self) -> tuple[float, float]:
        return (round(float(self.fast), 2), round(float(self.slow), 2))


def decay_step(fast: float, slow: float) -> tuple[float, float]:
    """衰减一天。低段必须减法，否则墓碑永不触发（V6 已踩两次的同一个坑）。"""
    fast = max(0.0, float(fast))
    slow = max(0.0, float(slow))
    if fast > FAST_SUB_FLOOR:
        fast *= FAST_DECAY_RATE
    else:
        fast -= FAST_SUB_STEP
    if fast < ZERO_EPS:
        fast = 0.0
    slow = max(0.0, slow - SLOW_DECAY_STEP)
    if fast + slow < ZERO_EPS:      # 双轨同时见底 → 一起归零
        fast = slow = 0.0
    return round(fast, 2), round(slow, 2)


def missed_days(last_iso: Any, today: Any = None, cap: int = MAX_CATCHUP_DAYS) -> int:
    """关机补算天数（封顶 14，避免出差回来全失忆）。"""
    if isinstance(today, str):
        try:
            today = date.fromisoformat(today[:10])
        except ValueError:
            today = date.today()
    today = today or date.today()
    if not last_iso:
        return 1
    try:
        last = date.fromisoformat(str(last_iso)[:10])
    except ValueError:
        return 1
    return max(0, min((today - last).days, cap))


def _bigrams(text: str) -> set[str]:
    s = _WS_RE.sub("", str(text or "").lower())
    if len(s) <= 1:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _newest_first(item: Any) -> int:
    """排序兜底键：target 编号越大 = 写得越新，返回负号让它排在前面。

    为什么需要：新记忆 fast 一写就是 40（快轨封顶），旧记忆 fast 也 ≥40，
    rank 全并列 0.4；不加这一层时「刚说的话」会被旧记忆挤掉（2026-10-31 实测）。
    """
    m = re.search(r"mem_(\d+)", str((item or {}).get("target", "")))
    return -int(m.group(1)) if m else 0


def label_weight(label: str, q_low: str, q_grams: set[str]) -> int:
    """标签与问题的匹配权重：2 = 整标签/整词命中，1 = 碎片近似，0 = 不相关。

    2026-10-31 修：以前任何 token 命中直接给 2 分，于是「晚安，本鱼」只因"本鱼"两字就召回了
    「[和他聊咋样 本鱼]」这条完全无关的记忆（实测）。现在整标签原样出现才算强匹配；
    碎片必须占到标签总长的一半以上，才降级给弱匹配。
    单字标签（[课]）保留强匹配：V5.2 遗留格式，丢了就成死标签。
    """
    lab = str(label or "").strip()
    if not lab or lab in ("未分类", "未归档"):
        return 0
    tokens = [t for t in re.split(r"[\[\]【】()（）\s]+", lab) if t]
    if not tokens:
        return 0
    content = "".join(tokens)
    if len(content) >= 2 and content.lower() in q_low:
        return 2                                   # 整标签原样出现在问句里
    if len(tokens) == 1 and tokens[0].lower() in q_low:
        return 2                                   # 单字 / 整词标签命中
    covered = sum(len(t) for t in tokens if t.lower() in q_low)
    if covered and covered / max(1, len(content)) >= 0.5:
        return 1                                   # 碎片至少占标签一半
    tg = _bigrams(lab)
    if not tg:
        return 0
    inter = q_grams & tg
    if len(inter) >= 2 and len(inter) / len(tg) >= 0.5:
        return 1
    return 0
    if not tg:
        return 0
    inter = q_grams & tg
    if len(inter) >= 2 and len(inter) / len(tg) >= 0.5:
        return 1
    return 0


@dataclass
class GateBundle:
    """门的一次放行结果。text 已经是可直接塞进 prompt 的成品。"""

    text: str = ""
    fuzzy: bool = False
    blocked: int = 0
    targets: tuple[str, ...] = ()
    display: tuple[float, ...] = ()


# ============================ 分流门 ============================
class MemoryGate:
    def __init__(
        self,
        root: str | os.PathLike,
        *,
        data_dir: str = "Mem_Data",
        index_name: str = "memory_index.json",
        archive_dir: str = "Mem_Archive",
        top_k: int = 2,
        max_chars: int = MAX_MEM_CHARS,
        save_interval: float = SAVE_MIN_INTERVAL,
    ) -> None:
        self.root = Path(root)
        self.data_dir = self.root / data_dir
        self.archive_dir = self.root / archive_dir
        self.index_path = self.root / index_name
        self.top_k = int(top_k)
        self.max_chars = int(max_chars)
        self.save_interval = float(save_interval)

        self._lock = threading.RLock()
        self._cache: dict[str, Any] | None = None
        self._cache_mtime: float = -1.0
        self._dirty = False
        self._last_flush = 0.0
        self._index_broken = False
        self._save_fail = 0
        self._target_ok: set[str] = set()

    # ---------------- 索引读写 ----------------
    @staticmethod
    def _blank() -> dict[str, Any]:
        return {"seq": 0, "decay_date": "", "labels": {}, "rescue": {}}

    def load(self, force: bool = False) -> dict[str, Any]:
        """读 index：mtime 缓存 + 结构兜底。解析失败 → 备份坏文件 → 空库继续（不崩）。"""
        with self._lock:
            try:
                mtime = self.index_path.stat().st_mtime
            except OSError:
                mtime = -1.0
            if not force and self._cache is not None and mtime == self._cache_mtime:
                return self._cache
            if self._dirty and self._cache is not None and mtime != self._cache_mtime:
                log.warning("外部进程改写了 index，本次内存里的回血将被丢弃（建议单写者）")

            data = self._blank()
            raw: Any = None
            try:
                raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                log.error("index 解析失败，按空库继续（原文件先备份）：%s", exc)
                self._index_broken = True
            except OSError as exc:
                log.error("index 读不到：%s", exc)

            if isinstance(raw, dict):
                labels = raw.get("labels")
                if isinstance(labels, dict):
                    for label, items in labels.items():
                        # 兼容 V5/V6 早期「一个标签一条」的单值写法
                        if isinstance(items, dict):
                            items = [items]
                        data["labels"][str(label)] = list(items) if isinstance(items, list) else []
                elif labels is not None:
                    log.error("labels 不是 dict，已丢弃")
                if isinstance(raw.get("seq"), int):
                    data["seq"] = raw["seq"]
                if raw.get("decay_date"):
                    data["decay_date"] = str(raw["decay_date"])[:10]
                if isinstance(raw.get("rescue"), dict):
                    data["rescue"] = raw["rescue"]
            elif raw is not None:
                log.error("index 根节点不是 dict，已丢弃")

            # 就地刷新：不换对象。换了对象的话，任何外部持有的 data 引用会变成
            # 写在一份游离副本上，回血静默丢失（实测踩过一次）。
            if self._cache is None:
                self._cache = data
            else:
                self._cache.clear()
                self._cache.update(data)
            self._cache_mtime = mtime
            return self._cache

    def _quarantine_broken(self) -> None:
        try:
            dst = self.index_path.with_name(
                f"{self.index_path.name}.bad-{datetime.now():%Y%m%d-%H%M%S}"
            )
            shutil.copy2(self.index_path, dst)
            log.error("损坏的 index 已备份为 %s", dst.name)
        except OSError as exc:
            log.error("备份损坏 index 失败：%s", exc)
        self._index_broken = False

    def flush(self, force: bool = False) -> bool:
        """原子写：tmp → fsync → replace。重试 2×0.05s（不是 6×0.5s，别在主线程上卡 3 秒）。"""
        with self._lock:
            if not self._dirty and not force:
                return False
            now = time.time()
            if not force and now - self._last_flush < self.save_interval:
                return False
            if self._cache is None:
                return False
            if self._index_broken:
                self._quarantine_broken()
            payload = json.dumps(self._cache, ensure_ascii=False, separators=(",", ":"))
            tmp = self.index_path.with_name(self.index_path.name + ".tmp")
            try:
                self.index_path.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                for attempt in range(3):
                    try:
                        os.replace(tmp, self.index_path)
                        break
                    except PermissionError:
                        if attempt == 2:
                            raise
                        time.sleep(0.05)
            except OSError as exc:
                self._save_fail += 1
                log.error("index 落盘失败（第 %d 次）：%s", self._save_fail, exc)
                return False
            self._dirty = False
            self._last_flush = now
            self._save_fail = 0
            try:
                self._cache_mtime = self.index_path.stat().st_mtime
            except OSError:
                self._cache_mtime = -1.0
            return True

    # ---------------- 条目校验（模块一·防呆） ----------------
    @staticmethod
    def valid_target(target: Any) -> str | None:
        """文件名白名单。Path().name 相等是防 '../' 路径穿越的关键一行。"""
        t = str(target or "").strip()
        if not t or Path(t).name != t or t in (".", ".."):
            return None
        return t if TARGET_RE.match(t) else None

    def inspect(self, item: Any) -> tuple[Score | None, str]:
        if not isinstance(item, dict):
            return None, "条目不是 dict"
        target = item.get("target")
        if not isinstance(target, str) or target not in self._target_ok:
            target = self.valid_target(target)
            if target is None:
                return None, "target 非法/缺失"
            self._target_ok.add(target)          # 合规文件名进缓存，省掉每条的正则
        try:
            fast = float(item.get("fast", 0.0) or 0.0)
            slow = float(item.get("slow", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None, "分数不是数字"
        if fast != fast or slow != slow or abs(fast) > 1e6 or abs(slow) > 1e6:
            return None, "分数 NaN/越界"
        return Score(max(0.0, fast), max(0.0, slow)), ""

    def read_body(self, target: str) -> str | None:
        path = self.data_dir / target
        try:
            if not path.is_file():
                return None
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:                      # 夜间删除竞态 → 当 ghost 处理
            log.warning("读记忆失败 %s：%s", target, exc)
            return None
        body = _HTML_HEADER_RE.sub("", raw)
        body = _WS_RE.sub(" ", body).strip()
        return body or None

    # ---------------- 回血（模块一·4，含防通胀） ----------------
    def _rescue_allowed(self, label: str, item: dict, today: str) -> bool:
        if int(item.get("rescue_n", 0) or 0) >= RESCUE_PER_ITEM and item.get("rescue_date") == today:
            return False
        day = (self._cache or {}).setdefault("rescue", {}).setdefault(today, {})
        return int(day.get(label, 0) or 0) < RESCUE_PER_LABEL

    def _mark_rescue(self, label: str, item: dict, today: str) -> None:
        item["rescue_date"] = today
        item["rescue_n"] = int(item.get("rescue_n", 0) or 0) + 1
        day = (self._cache or {}).setdefault("rescue", {}).setdefault(today, {})
        day[label] = int(day.get(label, 0) or 0) + 1

    def _slow_gain(self, item: dict, today: str, now: float) -> float:
        """慢轨 +1：30 分钟去重窗口 + 每日封顶 3。没有这两道闸，20 条消息能 +20。"""
        if item.get("slow_date") != today:
            item["slow_date"] = today
            item["slow_pts"] = 0.0
        used = float(item.get("slow_pts", 0.0) or 0.0)
        if used >= SLOW_DAILY_CAP:
            return 0.0
        if now - float(item.get("slow_ts", 0.0) or 0.0) < SLOW_DEDUP_SEC:
            return 0.0
        item["slow_ts"] = round(now, 3)
        item["slow_pts"] = round(used + HIT_SLOW, 2)
        return HIT_SLOW

    def reinforce(self, label: str, item: dict, score: Score, today: str,
                  now: float | None = None) -> dict[str, Any]:
        """命中回血。只改内存，返回本次增量。

        规则（防通胀四条）：
          ① real < 25 且当天没救过 → 直接抬到 real=40，不吃日帽，幂等；
          ② 普通命中 → fast +10，受「单条每日 +20」与「fast 封顶 40」双重压制；
          ③ 慢轨 +1 有去重窗口与每日上限；
          ④ 救援只加 fast，不重置 slow（长期沉淀不能被一次救援重算）。
        """
        now = time.time() if now is None else now
        fast, slow = float(score.fast), float(score.slow)
        real = fast + slow
        out: dict[str, Any] = {"fast": 0.0, "slow": 0.0, "rescue": False, "real": real}

        if real < FUZZY_HIGH and self._rescue_allowed(label, item, today):
            add = max(0.0, min(RESCUE_FLOOR - real, FAST_CAP - fast))
            if add > 0:
                item["fast"] = round(fast + add, 2)
                self._mark_rescue(label, item, today)
                out.update(fast=add, real=round(fast + add + slow, 2), rescue=True)
                return out

        if item.get("hit_date") != today:
            item["hit_date"] = today
            item["hit_pts"] = 0.0
        used = float(item.get("hit_pts", 0.0) or 0.0)
        room = max(0.0, DAILY_FAST_CAP - used)
        add_fast = min(HIT_FAST, room, max(0.0, FAST_CAP - fast))
        add_slow = self._slow_gain(item, today, now)
        item["fast"] = round(fast + add_fast, 2)
        item["slow"] = round(slow + add_slow, 2)
        item["hit_pts"] = round(used + add_fast, 2)
        out.update(fast=add_fast, slow=add_slow, real=round(fast + add_fast + slow + add_slow, 2))
        return out

    # ---------------- 门控 + 取料（模块一·1/2/3） ----------------
    def fetch(self, query: str, *, top_k: int | None = None,
              max_chars: int | None = None) -> GateBundle:
        """读路径：只读 + 内存回血。返回可直接进 prompt 的文本块。"""
        q = str(query or "").strip()
        if not q:
            return GateBundle()
        top_k = self.top_k if top_k is None else int(top_k)
        max_chars = self.max_chars if max_chars is None else int(max_chars)
        today = date.today().isoformat()

        with self._lock:
            data = self.load()
            labels = data.setdefault("labels", {})
            q_low, q_grams = q.lower(), _bigrams(q)
            cand: list[tuple[int, Score, str, dict]] = []
            blocked = 0

            for label in list(labels):
                weight = label_weight(label, q_low, q_grams)
                if weight <= 0:
                    continue
                items = labels.get(label) or []
                if not isinstance(items, list):
                    labels[label] = items = []
                for item in items:
                    score, _why = self.inspect(item)
                    if score is None:
                        blocked += 1
                        continue
                    if score.state == STATE_DEAD:
                        item["tombstone"] = True         # 只打标记，不删文件
                        blocked += 1
                        continue
                    cand.append((weight, score, label, item))   # 打分阶段零磁盘 IO

            if not cand:
                return GateBundle(blocked=blocked)

            # 排序用归一化 rank，不是裸 real（slow 无上限，裸 real 会让老记忆永久霸榜）
            # 第三键：rank 并列时新记忆优先（本鱼补丁，2026-10-31）
            cand.sort(key=lambda t: (-t[0], -t[1].rank, _newest_first(t[3])))

            # 二阶段：只对「真要用」的候选做存在性校验 + 读取。
            # is_file() 是系统调用；一万条的库里若对每个候选都 stat 一次，单次门就要 80 ms。
            texts: list[str] = []
            fuzzy = False
            used = 0
            targets: list[str] = []
            displays: list[float] = []
            attempts = 0
            seen_targets: set[str] = set()
            max_attempts = min(len(cand), max(8, top_k * 4))
            for _w, score, label, item in cand:
                if len(texts) >= top_k or attempts >= max_attempts:
                    break
                if item["target"] in seen_targets:
                    continue                            # 同一实体挂多个标签，只喂一次
                attempts += 1
                body = self.read_body(item["target"])   # is_file + read_text 合并为一次
                if body is None:
                    item["ghost"] = True
                    blocked += 1
                    continue
                room = max_chars - used
                if room <= 0:
                    break
                seen_targets.add(item["target"])
                body = body[:room]
                texts.append(body)
                used += len(body)
                targets.append(item["target"])
                displays.append(score.display)
                if score.state == STATE_FUZZY:
                    fuzzy = True
                self.reinforce(label, item, score, today)
                self._dirty = True

            if not texts:
                return GateBundle(blocked=blocked)
            self.flush()                                  # 节流：最多 60 秒一次
            head = (FUZZY_PREFIX + "\n") if fuzzy else ""
            return GateBundle(text=head + "\n\n".join(texts), fuzzy=fuzzy,
                              blocked=blocked, targets=tuple(targets),
                              display=tuple(displays))

    def prompt_block(self, query: str) -> str:
        return self.fetch(query).text

    def _load_for_write(self) -> dict[str, Any]:
        """夜间任务专用：内存里有没落盘的回血时优先保住内存版，别被 force 重载冲掉。"""
        with self._lock:
            if self._dirty and self._cache is not None:
                return self._cache
            return self.load(force=True)

    # ---------------- 每日衰减（模块二·2） ----------------
    def decay(self, today: Any = None) -> dict[str, Any]:
        with self._lock:
            data = self._load_for_write()
            today_s = str(today or date.today().isoformat())[:10]
            n = missed_days(data.get("decay_date"), today_s)
            report: dict[str, Any] = {"days": n, "alive_before": 0, "alive_after": 0,
                                      "killed": 0, "aborted": False, "retries": 0}
            if n <= 0:
                return report

            scored: list[tuple[dict, Score]] = []
            for _label, items in (data.get("labels") or {}).items():
                for item in items or []:
                    score, _ = self.inspect(item)
                    if score is None:
                        continue
                    scored.append((item, score))
                    if score.real > 0:
                        report["alive_before"] += 1
            before = report["alive_before"]

            def _plan(days: int) -> tuple[list[tuple[dict, float, float]], int]:
                rows: list[tuple[dict, float, float]] = []
                alive = 0
                for item, score in scored:
                    f, s = score.fast, score.slow
                    for _ in range(days):
                        f, s = decay_step(f, s)
                    rows.append((item, f, s))
                    if f + s > 0:
                        alive += 1
                return rows, alive

            # 熔断要能「退」，但绝不能「卡死」：
            # 一次补算 14 天干掉 77% 时，先按 7 → 3 → 1 天递减重算；
            # 到 1 天还超阈值，说明这批记忆本来就在死亡线上，放行 + 告警 + 备份轮转。
            plan, after = _plan(n)
            tries = 0
            while before and after < before * (1.0 - MAX_KILL_RATIO) and n > 1 and tries < 4:
                n = max(1, n // 2)
                tries += 1
                plan, after = _plan(n)
                log.warning("衰减熔断：本次会清零 %d/%d 条活记忆，改按 %d 天重算",
                            before - after, before, n)
            report.update(days=n, alive_after=after, retries=tries)
            if before and after < before * (1.0 - MAX_KILL_RATIO):
                report["aborted"] = True
                log.error("衰减熔断：按 %d 天仍会清零 %d/%d 条，已强制执行（否则 decay_date "
                          "永不推进，记忆会被永久冻结）", n, before - after, before)

            for item, f, s in plan:
                item["fast"], item["slow"] = f, s
                if f + s <= 0:
                    item["tombstone"] = True
            report["killed"] = before - after
            data["decay_date"] = today_s
            data["rescue"] = {}                            # 只保留当天的救援计数
            self._dirty = True
            self.flush(force=True)
            return report

    # ---------------- 夜间清理（模块二·3） ----------------
    def sweep(self) -> dict[str, Any]:
        """先按 target 聚合，再归档，再删。绝不因为「这个标签死了」就删仍在用的实体。"""
        with self._lock:
            data = self._load_for_write()
            labels = data.setdefault("labels", {})
            touched: set[str] = set()
            dropped = 0
            for label in list(labels):
                keep: list[dict] = []
                for item in labels.get(label) or []:
                    score, _ = self.inspect(item)
                    dead = score is None or score.real <= 0 or bool(item.get("tombstone"))
                    if dead:
                        dropped += 1
                        if isinstance(item, dict) and item.get("target"):
                            touched.add(str(item["target"]))
                    else:
                        keep.append(item)
                if keep:
                    labels[label] = keep
                else:
                    labels.pop(label, None)

            alive = {str(i.get("target")) for items in labels.values()
                     for i in (items or []) if isinstance(i, dict)}
            moved = 0
            for target in touched:
                if not target or target in alive:
                    continue                                # 还有别的标签在用 → 不许删
                src = self.data_dir / target
                if not src.is_file():
                    continue
                try:
                    self.archive_dir.mkdir(parents=True, exist_ok=True)
                    dst = self.archive_dir / target
                    if dst.exists():
                        dst = self.archive_dir / f"{src.stem}.{int(time.time())}.txt"
                    shutil.move(str(src), str(dst))
                    moved += 1
                except OSError as exc:
                    log.warning("归档失败（%s）：%s", target, exc)

            if dropped or moved:
                self._dirty = True
                self.flush(force=True)
            return {"dropped": dropped, "moved": moved, "alive": len(alive)}

    # ---------------- 每日任务入口 ----------------
    def daily(self) -> dict[str, Any]:
        with self._lock:
            self._rotate()
            report = self.decay()
            report.update(self.sweep())
            return report

    def _rotate(self) -> None:
        if not self.index_path.exists():
            return
        dst = self.root / f"memory_index.{date.today():%Y%m%d}.json"
        try:
            if not dst.exists():
                shutil.copy2(self.index_path, dst)
            for old in sorted(self.root.glob("memory_index.2*.json"))[:-ROTATE_KEEP]:
                old.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("index 轮转失败：%s", exc)


# ============================ 后置输出门 ============================
class OutputGate:
    """模块三：纯正则，零模型算力。只做三件事：删泄漏句、删幻觉句、盖时间戳。

    注意：这里用「句级删除」而不是整条替换/片段替换。
    片段替换会拼出「你今天少喝点酒，多喝温水」这种半截话；整条替换会丢掉同一条里对的话。
    """

    LEAK_PATTERNS = (
        re.compile(r"【[^】\n]{0,40}(?:模糊的记忆|不确定的语气|不太确定|别编造细节)[^】\n]{0,60}】"),
        re.compile(r"(?:这是一段模糊的记忆|这条记忆不太确定|可以说得含糊些|别编造细节)"),
        re.compile(r"(?:请|要|应)(?:用|以)[「“\"']?[^」”\"'\n]{0,12}[」”\"']?的?(?:语气|口吻)"),
    )
    FORBIDDEN = re.compile(r"酒|喝醉|醉了|会醉|别醉|小心醉|干杯|碰杯")
    # 本鱼补丁：V6 原版只认「酒」字，会漏掉「别喝太多，会醉的」「小心醉了」这类
    # 不含酒字的劝酒句（14 号文档明确要求覆盖）。加了醉类词，但**不加**「喝太多」——
    # 「你喝太多水了」是正当关心，不能误删。
    FALLBACK = "……唔，我刚才走神了，你再说一次？"
    _SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")
    # 逗号也算分句边界：不然「你今天少喝点酒，早点睡吧。」会被整句删光（实测踩到）
    _CLAUSE_SPLIT = re.compile(r"(?<=[。！？!?；;\n，,、])")
    _CLOCK = ((5, "凌晨"), (8, "早上"), (11, "上午"), (13, "中午"), (17, "下午"), (19, "傍晚"), (23, "晚上"))

    def clean(self, text: str) -> tuple[str, bool]:
        """返回 (清洗后文本, 是否触发过拦截)。"""
        t = str(text or "")
        if not t.strip():
            return t, False
        hit = False

        # ① 先删【】块：括号里只有逗号没有句号，先做句切分会把后面的正话一起带走
        for pat in self.LEAK_PATTERNS:
            t, n = pat.subn("", t)
            hit = hit or n > 0

        # ② 再去分句删幻觉句（「酒」字黑名单，覆盖少喝点酒/别喝太多/酒精）
        #    按逗号级切分，只丢掉含「酒」的那一小句，同句里的正话留住。
        if self.FORBIDDEN.search(t):
            kept = [p for p in self._CLAUSE_SPLIT.split(t)
                    if p.strip() and not self.FORBIDDEN.search(p)]
            hit = True
            t = "".join(kept).lstrip("，,、；; ")
            if not t.strip():
                t = ""

        t = _WS_RE.sub(" ", t).strip(" ，、；:：")
        if not t:
            return self.FALLBACK, True
        return t, hit

    @classmethod
    def clock(cls, now: datetime | None = None) -> str:
        now = now or datetime.now()
        label = "深夜"
        for hour, name in cls._CLOCK:
            if now.hour >= hour:
                label = name
        return f"现在是{label}{now:%H:%M}（{now:%Y-%m-%d}）"

    @classmethod
    def time_head(cls, now: datetime | None = None) -> str:
        """时间戳给 prompt 用。放在 system 尾部而不是开头：开头一变，整段 KV cache 全废。"""
        return f"\n[环境] {cls.clock(now)}"


# ============================ 自测 ============================
def _self_test() -> None:  # pragma: no cover
    import tempfile

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    ok, bad = 0, 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok, bad
        if cond:
            ok += 1
            print(f"  PASS  {name}")
        else:
            bad += 1
            print(f"  FAIL  {name} {extra}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "Mem_Data").mkdir()
        for i in (1, 2, 3, 17):
            (root / "Mem_Data" / f"mem_{i:03d}.txt").write_text(f"第{i}条记忆正文。", encoding="utf-8")

        gate = MemoryGate(root, save_interval=0)
        data = gate.load(force=True)
        data["labels"] = {
            "[地点]红河学院": [{"target": "mem_001.txt", "fast": 2.0, "slow": 3.0}],
            "[地点]红河学院北门": [{"target": "mem_002.txt", "fast": 0.0, "slow": 0.0}],
            "[事件]考试": [{"target": "mem_003.txt", "fast": 40.0, "slow": 0.0}],
            "[人]老板": [{"target": "mem_017.txt", "fast": 30.0, "slow": 300.0}],
            "[坏]格式": [{"target": "mem_999.txt", "fast": 10}],
            "[坏]穿越": [{"target": "../../etc/passwd", "fast": 10}],
        }
        gate._dirty = True

        print("[1] 状态分档")
        S = Score
        check("0 → dead", S(0, 0).state == STATE_DEAD)
        check("0.4 → fuzzy（(0,1) 无盲区）", S(0.4, 0).state == STATE_FUZZY)
        check("24.9 → fuzzy", S(0, 24.9).state == STATE_FUZZY)
        check("25 → normal", S(25, 0).state == STATE_NORMAL)
        check("330 → normal 且 display=100", S(30, 300).state == STATE_NORMAL and S(30, 300).display == 100.0)

        print("[2] 防呆拦截")
        b = gate.fetch("红河学院")
        check("坏 target 被拦不崩", isinstance(b, GateBundle))
        check("穿越路径被拒", gate.valid_target("../../etc/passwd") is None)
        check("实体缺失 → ghost", "mem_999.txt" not in b.targets)

        print("[3] 回血：濒死救援 + 防通胀")
        item = {"target": "mem_001.txt", "fast": 2.0, "slow": 3.0}
        r1 = gate.reinforce("[地点]红河学院", item, gate.inspect(item)[0], "2026-10-30")
        check("5 分被救到 40", r1["rescue"] and abs(item["fast"] + item["slow"] - 40.0) < 0.01,
              f'{item}')
        r2 = gate.reinforce("[地点]红河学院", item, gate.inspect(item)[0], "2026-10-30")
        check("同日同标签不再第二次救援", not r2["rescue"])
        item2 = {"target": "mem_003.txt", "fast": 0.0, "slow": 25.0}   # 已离开濒死区
        total = 0.0
        for _ in range(5):
            total += gate.reinforce("[事件]考试", item2, gate.inspect(item2)[0], "2026-10-30")["fast"]
        check("连召 5 次 fast 增量停在日帽 20", abs(total - 20.0) < 0.01, f"add={total}")
        check("fast 不越 40 上限", item2["fast"] <= 40.0, f'fast={item2["fast"]}')

        print("[4] 衰减必归零 + 墓碑")
        f, s = 40.0, 40.0
        days = 0
        while (f, s) != (0.0, 0.0) and days < 120:
            f, s = decay_step(f, s)
            days += 1
        check("40/40 分最终精确归零（不是 1e-9）", (f, s) == (0.0, 0.0), f"f={f} s={s}")
        print(f"        （40 分记忆归一零用了 {days} 天：fast 先枯，slow 1 分/天收尾）")

        print("[5] 多标签不误删")
        data["labels"]["[地点]红河学院"][0]["fast"] = 30.0
        data["labels"]["[地点]红河学院"][0]["slow"] = 30.0
        data["labels"]["[事件]考试"][0].update(fast=0.0, slow=0.0, tombstone=True)
        gate._dirty = True
        gate.flush(force=True)          # 先落盘，模拟「第二天磁盘上的样子」
        rep = gate.sweep()
        check("仍有活引用的实体没被删", (root / "Mem_Data" / "mem_001.txt").is_file())
        check("死条目被摘掉", "mem_003.txt" not in {i.get("target") for v in data["labels"].values() for i in v})

        print("[5b] 同一实体挂多标签，只喂一次")
        g3 = MemoryGate(root, save_interval=0)
        d3 = g3.load(force=True)
        d3["labels"] = {
            "[地点]红河学院": [{"target": "mem_001.txt", "fast": 30.0, "slow": 10.0}],
            "[地点]红河学院北门": [{"target": "mem_001.txt", "fast": 30.0, "slow": 10.0}],
        }
        b3 = g3.fetch("红河学院在哪", top_k=2)
        check("重复实体只出现一次", len(b3.targets) == 1, str(b3.targets))

        print("[6] 输出门")
        og = OutputGate()
        out, hit = og.clean("【这是一段模糊的记忆，请用不确定的语气回答】我记得你说过要早睡。")
        check("泄漏块被删且正话保留", hit and "模糊的记忆" not in out and "早睡" in out, out)
        out, hit = og.clean("你今天少喝点酒。早点睡吧。")
        check("酒句整句删、别的句保留", hit and "酒" not in out and "早点睡" in out, out)
        out, hit = og.clean("少喝点酒")
        check("全删光走兜底", out == og.FALLBACK, out)
        out, hit = og.clean("你今天少喝点酒，早点睡吧。")
        check("逗号级切除：只丢含酒那一小句", hit and "酒" not in out and "早点睡" in out, out)
        check("时间戳含时段", "现在" in og.clock(datetime(2026, 10, 30, 9, 5)))

        print("[7] 熔断不许死锁（关机 30 天回来）")
        with tempfile.TemporaryDirectory() as td2:
            r2 = Path(td2)
            (r2 / "Mem_Data").mkdir()
            for i in range(1, 11):
                (r2 / "Mem_Data" / f"mem_{i:03d}.txt").write_text("x", encoding="utf-8")
            idx2 = {
                "seq": 10,
                "decay_date": (date.today() - timedelta(days=30)).isoformat(),
                "labels": {"[测试]": [{"target": f"mem_{i:03d}.txt", "fast": 6.0, "slow": 0.0}
                                      for i in range(1, 11)]},
            }
            (r2 / "memory_index.json").write_text(
                json.dumps(idx2, ensure_ascii=False), encoding="utf-8")
            g2 = MemoryGate(r2, save_interval=0)
            rep = g2.decay()
            check("熔断按天递减重算", rep["retries"] >= 1, str(rep))
            check("decay_date 必须推进（否则永久冻结）",
                  g2.load(force=True)["decay_date"] == date.today().isoformat(), str(rep))

    print(f"\n自测：{ok} 通过 / {bad} 失败")
    raise SystemExit(0 if bad == 0 else 1)


if __name__ == "__main__":
    _self_test()
