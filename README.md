# V6 记忆核（Memory Gate）· 本地 7B 的长记忆 + 双脑路由 + 输出门

> 给「本地小模型 + 云端兜底」这类私人助手用的记忆层。**不依赖向量数据库**，全部落成可读可改的文本文件；
> 一条记忆的生死由分数决定（快轨热度 + 慢轨沉淀），死了就归档，不删内容。

- 依赖：`openai`、`requests`、`python-dotenv`（可选）
- 必需：本地 Ollama（或任何 OpenAI 兼容端点）
- 三个文件即全部核心，**不认识飞书、不认识任何 IM**——接什么平台由你自己写适配

---

## 一、三个模块

| 文件 | 职责 |
|---|---|
| `v6_memory_gate.py` | **记忆门**：分数/回血/衰减/熔断/归档（`MemoryGate`）+ **输出门**（`OutputGate`） |
| `memory_manager.py` | **外壳**：`init / save_memory / retrieve_memory / decay_task / start_decay_thread / stats`，签名稳定，换实现不动上层 |
| `brain_router.py` | **双脑**：本地优先 → 失败降级云端；日额度、连续失败自闭、模式切换（auto/local/cloud） |

配套：`demo_cli.py`（终端对话演示）、`persona.example.md`（人设外置）、`.env.example`。

---

## 二、快速开始

```bash
pip install openai requests python-dotenv

# 1) 配置
copy .env.example .env          # Windows；Linux/macOS 用 cp
copy persona.example.md persona.md
#    .env 里填 MODEL_NAME（你 ollama list 里的名字），要云端兜底再填 CLOUD_API_KEY

# 2) 跑起来
python demo_cli.py --no-cloud   # 全本地，不花钱
python demo_cli.py              # auto 挡：本地挂了会自动问云端

# 3) 自测（23 条，含熔断/归档/输出门）
python v6_memory_gate.py
```

第一次运行会在当前目录生成：

```
Mem_Data\mem_001.txt ...       # 记忆实体（正文，纯文本）
memory_index.json              # 唯一真源：标签 → [{target, fast, slow}]
Mem_Archive\                   # 死亡实体归档（内容保留，不删）
memory_index.YYYYMMDD.json     # 每日滚动备份（留 7 份）
```

---

## 三、数据流

```
用户这句话
   │
   ├─ retrieve_memory(text)                 ← 标签匹配 + 打分排序
   │     └─ 命中 → 回血（fast+10 封顶 40 / slow+1）+ 取正文
   │
   ├─ brain_router.generate_reply(text, memory, history)
   │     ├─ ping 本地 → 通 → 本地 7B（人设 + 记忆 + 最近 3 轮）
   │     └─ 不通/报错 → 云端（日额度闸 + 连续失败 3 次当天自闭）
   │
   ├─ OutputGate.clean(reply)               ← 删指令泄漏 / 删禁词句 / 盖时间戳
   │
   └─ save_memory("老板：…\n助手：…")        ← 抽标签 → 写实体 → 更新索引
```

**三段式发信**（`brain_router._ask_local`）：`system(人设)` → `system(记忆块 + 时间戳)` → 最近几轮 → 本次输入。
人设在前、记忆在尾，保住 KV cache 前缀；时间戳放最后，模型才知道"现在几点"。

---

## 四、分数模型（这套东西的灵魂）

```
rank = 0.6 × min(slow,100)/100 + 0.4 × min(fast,40)/40
```

| 参数 | 值 | 含义 |
|---|---|---|
| `INIT_FAST` / `INIT_SLOW` | 40 / 3 | 新记忆初始分：刚说的话要能立刻被想起 |
| `FAST_CAP` | 40 | 快轨封顶（短期热度） |
| `HIT_FAST` / `HIT_SLOW` | +10 / +1 | 每次被召回的回血 |
| `RESCUE_PER_LABEL` | 1 | 同一标签一天只回血一次，防止自己刷自己 |
| `MAX_KILL_RATIO` | 0.30 | 熔断：一次衰减干掉超过 30% 活记忆就重算，避免参数写错一夜清库 |
| `SAVE_MIN_INTERVAL` | 60s | 落盘节流：回血只改内存，最多 60 秒写一次盘 |

- **衰减**：每日一次，`slow` 先掉，`fast` 掉得快；分数归零 → `sweep()` 归档到 `Mem_Archive`
- **原子写**：`tmp → fsync → os.replace`，断电不会留下半个 JSON
- **单一真源**：`memory_index.json` 是唯一真相，实体文件只存正文 + 一行 `<!-- tags: … created: … -->`

> ⚠️ **踩过的坑**：迁移时把旧分（45~70）直接塞进 `fast`，而 `FAST_CAP=40` →
> `max(0, 40-70)=0` → 回血永远 +0，快轨静默失效。要么把超出部分搬到 `slow`，要么别让 `fast` 超 `FAST_CAP`。

---

## 五、输出门（OutputGate）

一个纯正则的后处理，零模型算力，三条规则：

| 规则 | 动作 |
|---|---|
| `【…模糊的记忆/不确定的语气…】` 这类指令泄漏 | 整块删掉 |
| 禁词句（示例是「酒」：`酒|喝醉|醉了|会醉|干杯`） | **按小句删**（逗号级），正话保留；全删光走兜底话 |
| 时间戳 | 往 prompt 里盖「现在是 X 点」 |

分句是逗号级的：`好的呢，我这就去。` 只丢「好的呢，」，保留「我这就去。」——
只按句号切会把整句删掉，连正话一起没。这条是实战踩出来的。

---

## 六、实测数据（8GB 显存笔记本 / Qwen2.5-7B Q4）

| 项 | 数 |
|---|---|
| 单次检索 `fetch` | 均值 **0.41 ms**（p95 0.44 ms） |
| 落盘一次 | 2.7 ms（且 60 秒才写一次） |
| 每日衰减 / 归档 | 2.8 ms / 3.7 ms |
| 800 轮对话合计 CPU | 328 ms（占一天 0.0004%） |
| 库规模 | 100 文件 ≈ 1000 标签 ≈ 10000 指针，检索仍是亚毫秒级 |

---

## 七、接到你自己的项目里

最小接法（三行）：

```python
import memory_manager as mm
import brain_router as br

mm.init()
mem = mm.retrieve_memory(user_text)                 # ① 取记忆
reply = br.generate_reply(user_text, mem)           # ② 生成（自动降级）
mm.save_memory("老板：%s\n助手：%s" % (user_text, reply))   # ③ 落记忆
```

数据目录默认就是这三个 .py 所在目录；想放到别处就设环境变量 `BOT_HOME`（旧名 `FEISHU_BOT_HOME` 仍兼容）。

要换角色，只改三处：

1. `.env` 的 `OWNER_NAME` / `ASSISTANT_NAME`
2. `persona.md`（人设全文，`brain_router` 启动时读）
3. `memory_manager.py` 里标签垃圾词表（称呼/语气词，别让它们进索引）

---

## 八、已知限制（诚实清单）

1. **跨进程只有进程内锁**：多个进程同时写 `memory_index.json` 会互相覆盖。单进程用它，多进程自己加文件锁。
2. **标签靠 bigram + 词表**：`我今晚要干嘛` 召不回 `要喝点酒`。要根治得接分词器（jieba）。
3. `rebuild_index()` 是全量重扫，会把墓碑/台词"复活"——重建前先备份。
4. 实体写盘没有索引那样的重试：磁盘满时可能留下无指针孤儿。
5. 衰减曲线是经验值（新记忆约 10 天归零），换模型/换场景要重调 `DECAY_*`。

---

## 九、许可

MIT，见 [LICENSE](LICENSE)。架构细节见 [`docs/架构说明.md`](docs/架构说明.md)。
