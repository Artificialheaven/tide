# 梗知识库合并 cnmeme 6684 条词库 + 注入/提示词升级（实施方案）

> 状态：**待实施（计划 2026-11-05 执行）**。本文是已评审的落地方案，含实测数据与分步任务。
> 参考插件：`astrbot_plugin_memewiki`（gitee in1t1a1），本地 clone 在
> `%TEMP%/astrbot_plugin_memewiki`（只读参考，**不要执行其中代码**）。

## Context（为什么做）

第三方 AstrBot 插件 `astrbot_plugin_memewiki` 自带 **6684 条**本地梗词库
（`data/memes.db` 16MB；usage/origin/other 结构化释义），并配套"先接梗别科普"
的注入提示词。我方梗系统（`plugins/deepseek/Core/meme_guide.py` + 独立
`db/meme.db`）只有 **111 条** B站视频 AI 抽取出的精编梗。本次把 6684 条合并入库，
让自动注入与 `meme.lookup` 工具获得全量词库，并学习对方的注入姿态与用法优先排版。

**核心约束（3000 条真实 user 消息 + 真实 `MemeGuide.scan` 实测）**：

| 策略 | 注入触发率 | 说明 |
|---|---|---|
| 不处理直接导入 | **27.0%** 不可接受 | `.jpg`/`OD`/`555`/`啊？`/群友昵称 |
| 先清洗文本（去 CQ 码/@/URL）再扫 | 4.3% | |
| 批量条目严格门控 P2（整句/提问/标点边界） | **0.5%** | 残余：昵称碰撞、技术词 token |
| P3（CJK ≥4 字允许句内子串） | **1.6%** | 新增多为真接梗（V我50、萝莉妈妈） |

词库里有 227 个纯拉丁/数字标题（`.jpg`、`114514`、`2+1`、`nice`），ascii_touch
命中 708 次（632 是 2 字拉丁字段）。**结论：111 条精编梗（与词库同名交集 88 条）
维持现有宽松匹配；`source='cnmeme'` 批量条目自动注入走严格门控；meme.lookup
工具显式查询不过门控（6600+ 全可查）。**

核实修正的三个事实：

1. 所谓 5 条"三列全空"其实都有 112 字 summary（全库 summary 无空值），照常导入、
   content 回退 summary；
2. 门控必须在 scan 截断之前**过取**，否则真梗被垃圾挤出 top-2；
3. 直播场景 `messages.sender_name` 只记每批弹幕首位观众（`scripts/bili_live_ai.py:668`），
   昵称抑制需 raw_messages + messages + users 三源合并（直播每观众有
   `storage.upsert_user`，bili_live_ai.py:659）。

## 不在本次范围

对方的在线漏斗（规则/LLM 闸门、cnmeme.wiki API 回退、检索存档）与梗库 Web
管理页面。API 免 key（`https://cnmeme.wiki/api/v1/public/memes`，UA 标识、
0.35s 限速），后续可在 `engine.py:836` 注入点独立增量。

---

## 实施步骤

### 1. schema v3 迁移 —— `plugins/deepseek/Core/meme_guide.py`

`migrate()` v2 块后追加 v3：memes 增列 `usage/origin/other/tags/slug/source`
（均 `TEXT NOT NULL DEFAULT ''`），`PRAGMA user_version=3`。常量默认值 ADD COLUMN
不重写表；保留 source_bv；**不加索引**（约 6.8k 行全量载入内存，无 SQL 按 source
过滤）。

- `source=''` 精编宽松（现有 111 条）；`source='cnmeme'` 批量严格。
- `load_index` SELECT 增加 `id, name, pinyin, content, source, usage, origin, other`
  （不取 tags/slug，省内存；调用方必须先 migrate，MemeGuide 构造函数已保证）。
- `upsert_meme()` 显式列名不含新列 → ON CONFLICT 也不碰新列，天然不覆盖 cnmeme
  数据，无需改（在代码注释中说明）。
- MemeHit dataclass 加带默认值字段 `source/usage/origin/other`，5 处构造补关键字参数。
- 迁移幂等：v3 库再调 migrate 为 no-op。

### 2. 清洗 + 形态分类纯函数 —— `Core/meme_guide.py`

```python
_CQ_RE      = re.compile(r'\[CQ:[^\]]*\]')
_URL_RE     = re.compile(r'https?://[^\s，。！？、；）」』】]+')
_MENTION_RE = re.compile(r'@[0-9A-Za-z_一-鿿]+')

def clean_inject_text(text) -> str: ...
    # 仅注入路径使用；CQ 码/URL/@提及替换为空格（保持两侧边界不粘连）

def classify_hit_shape(text, h) -> str: ...
    # → 'whole' | 'question' | 'bounded' | 'embedded' | 'ascii_touch' | 'none'
```

- `@` 清洗用受限字符类（`@\S+` 会吞掉 `@蓝哥明天` 的正文）；未解析 @QQ 数字串
  还会被 ascii_touch 挡住。**故意不清洗 `[回复 谁：摘录]`**（真实引用，摘录里有
  真梗应命中；上线观察误报，必要时只对摘录降一档）。
- 定位 span 顺序：`h.witness`（去记号 NFKD、大小写不敏感）→ `h.field` 原文 →
  CJK 回退 normalize_cn 查找。直播多行文本（`名字：内容\n…`）只在含 span 的那一行
  内判定，`\n` 视为空白边界。
- 形态判定顺序：
  1. `whole`：整行 `normalize_cn` 后等于字段；
  2. `question`：疑问词命中（`是什么梗/啥是梗/是啥梗/什么梗/啥梗/什么意思/啥意思/
     啥玩意/什么玩意/什么东东/啥东西`），且命中位置前 head（剥一次
     `这个梗|那个梗|这梗|那梗|这个|那个|梗|请问` 尾缀、去边缘标点）归一长度
     1–12 且等于字段；
  3. 左/右邻字符 `c.isascii() and c.isalnum()` → `ascii_touch`；
  4. 邻字符 `c.isalnum()`（CJK 汉字 isalnum=True）→ `embedded`；
  5. 两侧为标点/空白/句界（含全角标点、`@`、`：`、换行）→ `bounded`；
  6. 找不到 span → `none`（按最严处理）。

### 3. 注入门控与渲染 —— `Core/meme_guide.py`

`injection_block(text, cfg, store, *, block_fields=None)`：

1. `cleaned = clean_inject_text(text)`；
2. **过取** `scan(cleaned, fuzzy=True, limit=max(meme_inject_max*4, 10))`；
3. 门控矩阵（受 `meme_strict_gate_on=True` 控制，关掉完全回旧行为）：

   | 形态 | 精编 source='' | 批量 source='cnmeme' |
   |---|---|---|
   | whole / question | 放 | 放 |
   | bounded | 放 | 仅字段含 CJK |
   | embedded | 放（2 字也放） | 仅归一长度 ≥4 且含 CJK |
   | ascii_touch / none | **拒（所有 source）** | 拒 |

4. 昵称抑制：`block_norm = {normalize_cn(n) for n in block_fields if len>=2}`，
   字段归一后命中则丢弃；**question 形态豁免**（明确问"X是什么梗"仍答）；
5. 保持 scan 置信度顺序截到 `meme_inject_max`，**不足不补位、不做第二轮宽松**；
   全空返回 `''`（零 token）。

渲染（预算沿用 `meme_entry_max_chars=600`：usage ≤400 + origin ≤150，先切 usage，
剩余预算再给 origin）：

- 注入行有 usage（学 cnmeme `_entry_text` 用法在前）：
  ```
  · {name}{note}：{usage_cut}
  　出处：{origin_cut}
  ```
  回退序 usage → content → origin → `（暂无释义）`；只有 content 时维持现行单行格式。
- `lookup_text`（工具路径，**无门控**，6600+ 全可查），每条：
  ```
  【{name}】{note}
  用法：{usage ≤600}
  出处：{origin ≤450}
  扩展：{other ≤400}
  ```
  三段皆空回退 content；总 cap `tool_result_max_chars=2000` 不变。tags/slug 不进
  提示词（tags 是分类标签，会引导模型贴标签；slug 依赖在线 API，本期无）。
  查无结果文案吸收对方 `_miss_hint`："资料里没有就老实说不知道，不要把不相干的
  梗拼在一起"。

### 4. 提示词与工具描述

- `Core/prompts.py:124-127` 默认 `_MEME_HEADER_DEFAULT` 改写（融合对方 `_dict_hint`
  与 5 条长期规则；**只在有命中时出现，零命中零 token，不做每轮常驻**）。草稿：

  ```
  【梗指南】以下是群友本轮消息里提到的网络梗内部资料，只供你理解语境、自然接梗，
  禁止原样复述或整段照抄：
  · 对方没明确问「什么梗/啥意思」时，照着「用法」用自己的口气接一句就行，
    别主动科普典故、别念出处，也别说「我查了一下」；
  · 对方明确问含义时才用一两句随口解释；资料里没有或拿不准的，别说死、别编、
    别把两个不相干的梗拼一起；
  · 短句口语，别用「首先/其次/希望对你有帮助」这类客服腔；
    近似匹配可能认错，以群友实际用词为准。
  {content}
  ```

  key 注册描述（prompts.py:261-266）同步"词条按用法/出处分段"；render 逻辑不变，
  用户已有的 meme_header 自定义继续优先（改默认对未自定义用户立即生效）。
- `Core/builtin_tools.py:1279-1284` meme_lookup 描述微调：本地词库 6600+ 条、
  query 尽量短直接给词、查不到如实说不知道不要编造；`prompts.py:396` 工具目录行
  同步量级。dispatch（:2345-2352）与映射（:89）不动。

### 5. 配置 —— `Core/config.py`

DEFAULT_CONFIG（:272 区块后）加：

```python
"meme_strict_gate_on": True,   # 批量词库严格形态门控 + 昵称抑制；False=全部条目回旧宽松行为
```

登记 `_BOOL_KEYS`；**不进 GROUP_OVERRIDABLE**（与 meme_inject_max /
meme_entry_max_chars 同为全局，免动 :1669 工具摘除逻辑）。

### 6. 昵称抑制接线 —— `Core/storage.py` + `Core/engine.py`

- 新增 `storage.recent_speaker_names(db_path, group_id, limit=300) -> set[str]`
  （紧邻现有 `group_roster` :1152）。三源短查询合并，strip 后去长度 <2：
  - `SELECT sender_name FROM raw_messages WHERE group_id=? AND sender_name<>''
    ORDER BY id DESC LIMIT ?`（QQ 闸门前全量语料，deepseek.py:231-247 `_record_raw`）；
  - 同形对 `messages`（触发轮；直播只有批次首位观众）；
  - `SELECT names FROM users WHERE last_group_id=? ORDER BY last_seen DESC LIMIT ?`
    （names 为 JSON 数组，每条取前 2 个名字；覆盖直播批次内全部观众）。
- `engine.py:836-843` 改为：strict 开启时取集合，传入
  `injection_block(..., block_fields=names)`；异常退回空集合、本轮跳过。
  gid/storage 在该作用域已在用（:811）。每轮 3 个小查询成本可忽略。直播脚本零改动
  受益（同 engine、同 users 表）。

### 7. 导入脚本 `scripts/meme_import_cnmeme.py`（新建）

参数：`--src`（默认 `plugins/deepseek/db/cnmeme_memes.db`）、`--db`（默认
`plugins/deepseek/db/meme.db`）、`--dry-run`、`--limit N`、`--no-backup`、
`--with-pinyin`。

- 路径处理：`sys.path.insert(0, <repo>/plugins/deepseek)` 后
  `from Core import meme_guide`，**复用 migrate/connect，不复制 DDL**
  （Core/__init__.py 为空，meme_guide 仅依赖 .prompts，venv 已验证可直接导入）。
- 纯函数 `map_row(row, *, with_pinyin=False) -> dict | None`（单测锚点）：
  - title→name（strip 后为空返回 None）；usage/origin/other/tags/slug 原样 strip；
  - `content = clip(usage,180) or clip(other,180) or clip(summary,180) or
    clip(origin,180)`——summary 是"来源/用法"混合整段，排在 other 之后；
  - 5 条三列空走 summary 回退（计 `summary_only`，不跳过）；47 条 usage 空照常导入；
  - **tags 绝不当别名导入**（分类标签 + 339 个 ≤2 字标题双重误伤源）。
- 拼音：默认不生成（venv 无 pypinyin；dssq/ylg 类首字母缩写全拼无效，收益仅限整句
  拼音查询）。`--with-pinyin` 时脚本内**惰性 import pypinyin**，未装给明确
  `pip install pypinyin` 提示；不加进 `plugins/deepseek/requirements.txt`，运行时零依赖。
- 单事务 `executemany`（6684 行实测 0.1s），SQL：

  ```sql
  INSERT INTO memes(name,pinyin,content,enabled,created_at,updated_at,
                    usage,origin,other,tags,slug,source)
  VALUES (:name,:pinyin,:content,1,:now,:now,
          :usage,:origin,:other,:tags,:slug,'cnmeme')
  ON CONFLICT(name) DO UPDATE SET
    usage  = CASE WHEN usage=''  THEN excluded.usage  ELSE usage  END,
    origin = CASE WHEN origin='' THEN excluded.origin ELSE origin END,
    other  = CASE WHEN other=''  THEN excluded.other  ELSE other  END,
    tags   = CASE WHEN tags=''   THEN excluded.tags   ELSE tags   END,
    slug   = CASE WHEN slug=''   THEN excluded.slug   ELSE slug   END;
  ```

  更新清单**不含 content/pinyin/source_bv/source/enabled**：88 条精编行的释义、拼音、
  别名、BV 全保留，source 仍为 `''` 继续走宽松匹配；重跑幂等（CASE 不再变化，
  必要时 updated_at 也加同形 CASE）。
- 报告 `inserted / enriched / summary_only / skipped_empty_name`；enriched 用导入前
  `SELECT name, usage, origin, other FROM memes` 快照在 Python 侧比对，不依赖 rowcount。
  预期 inserted≈6596、enriched≤88、summary_only=5。
- 备份用 **sqlite3 Online Backup API**（`sqlite3.connect(dst).backup(out)`），
  **不能 shutil.copy2 单文件**（WAL 下会丢 -wal 数据）；备份名 `meme.db.bak`
  （位于 `plugins/*/db/` 整目录 gitignore 内，已 `git check-ignore` 确认）。
- **dry-run 不得调 migrate()**（否则偷改线上 v2 库版本）：只读 src、对 dst 做 v2
  兼容的 SELECT 统计交集，不写任何东西。
- 正式运行：`migrate(dst)`（v2→v3）→ 备份 → 单事务导入。插件端 MemeGuide 文件
  签名热重载（db/-wal/-shm 三文件签名）自动刷索引，无需重启。

执行命令：

```bash
# 0) 放快照（手工一次；源文件 16MB，在 gitignore 目录内不会被提交）
cp "/c/Users/lnovo/AppData/Local/Temp/astrbot_plugin_memewiki/data/memes.db" \
   plugins/deepseek/db/cnmeme_memes.db
# 1) 演练（不写库、不迁移）
.venv/Scripts/python.exe -X utf8 scripts/meme_import_cnmeme.py --dry-run
# 2) 正式（自动备份 + v3 迁移 + 导入）
.venv/Scripts/python.exe -X utf8 scripts/meme_import_cnmeme.py
```

### 8. `scripts/meme_import_bili.py` 防呆

内嵌 DDL/upsert 停在 v2，但 SQL 显式列名在 v3 库可正常读写、不会抹掉新列；它新建
的 v2 库在插件首次启动时由 `MemeGuide()` 惰性升到 v3。**本次只加注释**
（:109、:141 附近："内嵌 DDL 停在 v2；v3 附加列 usage/origin/other/tags/slug/source
由 Core/meme_guide.py 唯一维护，新增列必须保持默认值兼容"），不迁移、不分叉。

---

## 测试 `tests/test_meme_guide_v3.py`（新建）

沿用 `tests/test_deepseek_context.py` 的 `sys.path.insert(... plugins/deepseek)`
+ `from Core import ...` 模式，纯 assert 函数（仓库根运行）。

1. **迁移**：手工建 v1→v2 临时库调 migrate → user_version=3、六列存在、旧数据完好；
   v3 再 migrate 幂等；
2. **clean_inject_text**：`[CQ:image,file=a.jpg]`、URL、`@123456`、`@蓝哥` 被去除
   且不制造字母粘连；正常内容不丢；
3. **classify_hit_shape**：whole；"V我50是什么梗"/"这啥意思啊" question（head≤12、
   剥尾缀）；标点两侧 bounded；CJK 夹句 embedded；`x555`/`tokenx` ascii_touch；
   多行按行判定；
4. **source 分级**（临时库 + injection_block）：精编 2 字 CJK embedded 仍注入；
   cnmeme 2 字 CJK embedded 拒、≥4 字 CJK（'V我50'）注入；cnmeme 纯拉丁
   bounded（'nice，'）拒、whole（'nice'）/question 放；ascii_touch 双方拒；
   过取后不足 limit 不补位（构造 3 候选前两个应拒，断言不放宽）；
   `meme_strict_gate_on=False` 回旧行为；
5. **昵称抑制**：block_fields 含字段名时 whole/bounded 被压；question 豁免；
6. **lookup_text 无门控**：ascii_touch/embedded 文本工具直查仍返回；分段含
   用法/出处/扩展；2000 cap；无命中走新 miss 文案；
7. **渲染**：有 usage 为"用法在前+出处"且总长 ≤600；usage 空回退 content；
   精编旧单行格式不变；
8. **map_row**（`importlib.util.spec_from_file_location` 按路径加载脚本）：
   content 回退序、summary_only、tags 不进别名、空 title 返回 None；
9. **导入集成**（tmp_path 小库 + 手工造 src sqlite）：inserted/enriched 计数；
   精编碰撞行 content/pinyin/source_bv/source 不被覆盖、仅空列补齐；重跑幂等。

## 验证

```bash
# 单测（有 pytest 的环境）
python -m pytest tests/test_meme_guide_v3.py -q
# 当前 venv 无 pytest：内联执行全部 test_ 函数
.venv/Scripts/python.exe -X utf8 -c "import importlib.util,pathlib; p=pathlib.Path('tests/test_meme_guide_v3.py'); s=importlib.util.spec_from_file_location('t',p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); [getattr(m,n)() for n in dir(m) if n.startswith('test_')]; print('all passed')"
# 导入演练 + 正式
.venv/Scripts/python.exe -X utf8 scripts/meme_import_cnmeme.py --dry-run
.venv/Scripts/python.exe -X utf8 scripts/meme_import_cnmeme.py
# 真实数据回归：导入后对 deepseek.db 最近 3000 条 user 消息重跑注入率，预期 0.5%~1.6%
# 启动 bot 观察：索引条目 ≈ 6795（111 + 6684）；前台轮命中行渲染"用法/出处"；后台/判官轮不注入
```

## 风险与回滚

- **部署两步走**：代码上线即自动 v2→v3（新列全空、无 cnmeme 行，行为与现状完全
  一致）；导入脚本是独立第二步，可灰度。
- ADD COLUMN 常量默认值不重写表，秒级迁移；导入前 Online Backup 留存 `meme.db.bak`。
- **kill switch**：`meme_guide_on=False` 总闸（连工具一起摘）；
  `meme_strict_gate_on=False` 数据保留、仅回旧宽松行为（全局即时生效，无需重启）。
- **观察项**：① `[回复 …：摘录]` 引用文本仍参与扫描，误报明显则对摘录降一档；
  ② 长标题 partial 前缀命中（distance=2、witness≥4 CJK）按 P3 放行，需 3000 语料
  复测注入率；③ 含标点奇葩昵称漏清洗由 block_fields 兜底。

## 关键文件

| 文件 | 改动 |
|---|---|
| `plugins/deepseek/Core/meme_guide.py` | v3 迁移、load_index/MemeHit 字段、清洗/分类纯函数、门控、用法优先渲染 |
| `plugins/deepseek/Core/engine.py` | :836 注入点传 block_fields |
| `plugins/deepseek/Core/storage.py` | 新增 recent_speaker_names（邻 :1152 group_roster） |
| `plugins/deepseek/Core/prompts.py` | :124 默认 header 改写、:261 注册描述 |
| `plugins/deepseek/Core/builtin_tools.py` | :1279 meme_lookup 描述、:396 目录行 |
| `plugins/deepseek/Core/config.py` | meme_strict_gate_on |
| `scripts/meme_import_cnmeme.py` | **新建**导入脚本 |
| `scripts/meme_import_bili.py` | 仅加 v2/v3 防呆注释 |
| `tests/test_meme_guide_v3.py` | **新建**测试 |

## 建议实施顺序

1. meme_guide.py：v3 迁移 + load_index/MemeHit 字段（先不碰门控），跑迁移/加载验证；
2. 清洗与形态分类纯函数 + 对应用例；
3. injection_block 门控/昵称抑制/渲染 + config 开关 + 用例；
4. prompts 默认文案、builtin_tools 描述；
5. storage.recent_speaker_names + engine 接线；
6. 新建导入脚本 + map_row 测试 + dry-run；复制快照；小样本正式导入；3000 语料回归；
7. 全量导入，核对 inserted/enriched/summary_only 报告，备份留存；
8. bili 脚本注释防呆。