"""梗指南：独立 meme.db + 多字段容错匹配（梗名/别名/拼音）。

同一条查询路径服务两处：
- engine 每轮前台真人消息自动扫描，命中把梗释义注入 system（PHI 之前）；
- 内置工具 meme_lookup（builtin_tools.dispatch），AI 主动查梗。

匹配字段：中文全名/中文别名（子串）、拼音/拼音写法（拉丁词，无声调、
空格可有可无）。
容错规则：
- 3 字中文字段允许任意 1 个汉字被替换（编辑距离 1，仅替换不增删）；
- 长梗名只说了前半截也算命中（如「专是专」→「专是专，本是本」）：
  去标点后取名字前缀，至少 3 字且覆盖名字一半以上；只认前缀不认后缀；
- 拼音允许 1 个音节差异（音节序列编辑距离 1），或整串 1~2 个字母笔误。
同一个「字窗/拉丁段」模糊命中多个不同梗时按歧义弃用该段。

梗库内容由独立生成脚本维护（upsert_meme），运行时只读；
内存索引按 meme.db 文件 mtime/size 自动刷新。
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass

from . import prompts as prompts_mod

# ── 调参常量 ──────────────────────────────────────────────────────
FUZZY_CN_LEN = 3              # 只对三字梗做汉字容错
LETTER_FUZZY_MAX = 2         # 拼音整串字母笔误上限
LETTER_FUZZY_MIN_LEN = 4     # 过短拼音串不做字母级容错（防误报）
MIN_CN_FIELD = 2             # 中文匹配字段最短长度（1 字别名太容易误伤）
PARTIAL_CN_MIN_FIELD = 5     # 只对 ≥5 字的中文字段做「只说了前半截」匹配
PARTIAL_CN_MIN_WINDOW = 3    # 至少说出 3 个字才猜
PARTIAL_CN_RATIO = 0.5       # 且前缀至少覆盖名字的一半
MIN_PY_RUN = 3               # 触发拼音匹配的最短字母数
DEFAULT_INJECT_MAX = 2       # 每轮自动注入梗条数上限
DEFAULT_ENTRY_CHARS = 600    # 单条梗释义注入字符上限

_CJK_RE = re.compile(r'[一-鿿]')
_LATIN_RUN_RE = re.compile(r'[A-Za-züÜ]+')
# 连续拉丁词（中间只隔空格/连字符）按整体也算一次，如 "nei juan"
_LATIN_GROUP_RE = re.compile(r"[A-Za-züÜ]+(?:[\s\-']+[A-Za-züÜ]+)*")


# ── DB：独立 meme.db（WAL + PRAGMA user_version 迁移）────────────

def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


_DDL_V1 = [
    """CREATE TABLE IF NOT EXISTS memes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL UNIQUE,
        pinyin     TEXT NOT NULL DEFAULT '',
        content    TEXT NOT NULL DEFAULT '',
        enabled    INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS meme_aliases (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        meme_id INTEGER NOT NULL REFERENCES memes(id) ON DELETE CASCADE,
        alias   TEXT NOT NULL,
        kind    TEXT NOT NULL DEFAULT 'cn',   -- cn=中文别名/缩写；py=拼音/英文等拉丁写法
        UNIQUE(meme_id, alias, kind)
    )""",
    'CREATE INDEX IF NOT EXISTS idx_meme_alias_kind ON meme_aliases(kind, alias)',
]


def migrate(db_path: str) -> None:
    conn = connect(db_path)
    try:
        ver = conn.execute('PRAGMA user_version').fetchone()[0]
        if ver == 0:
            for ddl in _DDL_V1:
                conn.execute(ddl)
            ver = 1
            conn.execute('PRAGMA user_version=1')
            conn.commit()
        if ver < 2:
            # 来源视频 BV：生成脚本增量导入时据此判断「已入库」并停止
            conn.execute("ALTER TABLE memes ADD COLUMN source_bv TEXT NOT NULL DEFAULT ''")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_memes_bv ON memes(source_bv)')
            conn.execute('PRAGMA user_version=2')
            conn.commit()
    finally:
        conn.close()


def _has_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s or ''))


def normalize_cn(s) -> str:
    """中文匹配归一：去掉标点/空格/符号并小写。

    只用于「名字里带标点」的精确比对和半截名匹配，例如
    「专是专，本是本」与消息里的「专是专 本是本」视为同一个名字。
    """
    return ''.join(c for c in str(s or '') if c.isalnum()).lower()


def upsert_meme(db_path: str, name: str, content: str, pinyin: str = '',
                aliases=None, enabled: bool = True, source_bv: str = '') -> int:
    """新增或按主名更新一条梗；别名整表重写（含 CJK 的归 cn，其余归 py）。

    供梗库生成脚本使用。source_bv 记录来源视频 BV（增量导入判重用）。返回梗 id。
    """
    name = str(name or '').strip()
    if not name:
        raise ValueError('梗名不能为空')
    migrate(db_path)
    now = time.time()
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO memes(name, pinyin, content, enabled, created_at, updated_at, source_bv)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   pinyin=excluded.pinyin, content=excluded.content,
                   enabled=excluded.enabled, source_bv=excluded.source_bv,
                   updated_at=excluded.updated_at""",
            (name, str(pinyin or ''), str(content or ''),
             1 if enabled else 0, now, now, str(source_bv or '').strip()))
        mid = conn.execute(
            'SELECT id FROM memes WHERE name=?', (name,)).fetchone()[0]
        conn.execute('DELETE FROM meme_aliases WHERE meme_id=?', (mid,))
        seen = {name}
        for alias in aliases or []:
            alias = str(alias or '').strip()
            if not alias or alias in seen:
                continue
            seen.add(alias)
            kind = 'cn' if _has_cjk(alias) else 'py'
            conn.execute(
                'INSERT OR IGNORE INTO meme_aliases(meme_id, alias, kind)'
                ' VALUES (?, ?, ?)', (mid, alias, kind))
        conn.commit()
        return mid
    finally:
        conn.close()


def count_memes(db_path: str, enabled_only: bool = True) -> int:
    where = 'WHERE enabled=1' if enabled_only else ''
    conn = connect(db_path)
    try:
        return conn.execute(f'SELECT COUNT(*) FROM memes {where}').fetchone()[0]
    finally:
        conn.close()


def bv_exists(db_path: str, bvid: str) -> bool:
    """该 BV 的梗是否已入库（生成脚本增量拉取的停止条件）。"""
    bvid = str(bvid or '').strip()
    if not bvid:
        return False
    conn = connect(db_path)
    try:
        return conn.execute(
            'SELECT 1 FROM memes WHERE source_bv=? LIMIT 1', (bvid,)
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def meme_name_exists(db_path: str, name: str) -> bool:
    """同名梗是否已入库（AI 总结出的梗已存在时跳过，不覆盖人工/旧内容）。"""
    name = str(name or '').strip()
    if not name:
        return False
    conn = connect(db_path)
    try:
        return conn.execute(
            'SELECT 1 FROM memes WHERE name=? LIMIT 1', (name,)
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def load_index(db_path: str) -> list[dict]:
    """启用中的梗 + 别名 → 运行时匹配索引（短开短关连接）。"""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            'SELECT id, name, pinyin, content FROM memes WHERE enabled=1'
        ).fetchall()
        if not rows:
            return []
        al_rows = conn.execute(
            'SELECT meme_id, alias, kind FROM meme_aliases').fetchall()
    finally:
        conn.close()
    amap: dict = {}
    for r in al_rows:
        amap.setdefault(r['meme_id'], []).append((r['alias'], r['kind']))
    out = []
    for r in rows:
        aliases = amap.get(r['id'], [])
        cn = [r['name']] + [a for a, k in aliases if k == 'cn']
        cn = list(dict.fromkeys(c for c in cn if len(c) >= MIN_CN_FIELD))
        cn_norm = [normalize_cn(c) for c in cn]
        py = []
        for field in [r['pinyin']] + [a for a, k in aliases if k == 'py']:
            letters = letters_only(field)
            if len(letters) >= MIN_PY_RUN:
                py.append({'raw': field, 'letters': letters,
                           'syls': pinyin_syllables(field)})
        if not cn and not py:
            continue
        out.append({'id': r['id'], 'name': r['name'], 'content': r['content'] or '',
                    'cn': cn, 'cn_norm': cn_norm, 'py': py})
    return out


# ── 拼音归一与音节切分 ───────────────────────────────────────────

# 声母（zh/ch/sh 优先）+ 韵母（长韵母在前，供正则分支按序贪心）
_FINALS = (
    'iang', 'iong', 'uang', 'ueng',
    'ian', 'iao', 'iou', 'uai', 'uei', 'uan', 'uen', 'van',
    'ang', 'eng', 'ing', 'ong',
    'ai', 'ei', 'ui', 'ao', 'ou', 'iu', 'ie', 'ue', 've', 'er',
    'an', 'en', 'in', 'un', 'vn',
    'ia', 'ua', 'uo', 'io', 'ng',
    'a', 'o', 'e', 'i', 'u', 'v',
)
_SYL_RE = re.compile(
    r'(?:zh|ch|sh|[bpmfdtnlgkhjqxrzcsyw])?(?:' + '|'.join(_FINALS) + r')')


def letters_only(s) -> str:
    """拼音归一：去声调、小写、ü/v 折成 u、只留 a-z（空格也去掉）。"""
    s = unicodedata.normalize('NFKD', str(s or '')).lower()
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.replace('ü', 'u').replace('v', 'u')
    return re.sub(r'[^a-z]+', '', s)


def split_syllables(letters: str):
    """无声调无空格拼音串 → 音节列表；无法整串合法切分时返回 None。"""
    out, pos = [], 0
    while pos < len(letters):
        m = _SYL_RE.match(letters, pos)
        if not m or m.end() == pos:
            return None
        out.append(letters[pos:m.end()].replace('v', 'u'))
        pos = m.end()
    return out or None


def pinyin_syllables(field) -> list | None:
    """库里的拼音字段（空格分隔音节或连写、可带声调）→ 音节列表；切不开 None。"""
    s = unicodedata.normalize('NFKD', str(field or '')).lower()
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.replace('ü', 'u').replace('v', 'u')
    parts = [p for p in re.split(r'[^a-z]+', s) if p]
    if not parts:
        return None
    syls = []
    for p in parts:
        sp = split_syllables(p)
        if sp is None:
            return None
        syls.extend(sp)
    return syls


# ── 距离 ─────────────────────────────────────────────────────────

def edit_distance(a, b, cap: int | None = None) -> int:
    """序列编辑距离；超过 cap 提前返回 cap+1（输入都很短）。"""
    if cap is None:
        cap = max(len(a), len(b))
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_best = i
        for j, cb in enumerate(b, 1):
            v = min(prev[j] + 1, cur[j - 1] + 1,
                    prev[j - 1] + (ca != cb))
            cur.append(v)
            if v < row_best:
                row_best = v
        if row_best > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _is_cjk(ch: str) -> bool:
    return bool(_CJK_RE.fullmatch(ch))


@dataclass
class MemeHit:
    meme_id: int
    name: str
    content: str
    field: str            # 实际命中的字段值（主名/别名/拼音原文）
    via: str              # name | alias_cn | pinyin
    exact: bool
    witness: str = ''     # 消息里实际出现的片段
    distance: int = 0
    fuzzy_kind: str = ''  # char | partial | syllable | letter


_VIA_RANK = {'name': 0, 'alias_cn': 1, 'pinyin': 2}


def _hit_key(h: MemeHit):
    """排序：精确在前 → 主名优先于别名/拼音 → 距离小 → 字段长（更具体）→ id。"""
    return (0 if h.exact else 1, _VIA_RANK.get(h.via, 3), h.distance,
            -len(h.field), h.meme_id)


def add_hit(hits: dict, h: MemeHit) -> None:
    """同梗已有更强命中（精确/距离更小）时不覆盖。"""
    old = hits.get(h.meme_id)
    if old is None or _hit_key(h) < _hit_key(old):
        hits[h.meme_id] = h


# 三字字段按下标对建前缀映射：距离 1（替换）必在某两个位置上相同
_PAIR_POS = ((0, 1), (0, 2), (1, 2))


def _strip_marks(s: str) -> str:
    """NFKD 去组合记号（è→e、ǚ→u），保留原字符种类（CJK 不受影响）。"""
    s = unicodedata.normalize('NFKD', str(s or ''))
    return ''.join(c for c in s if not unicodedata.combining(c))


def latin_queries(text: str) -> list[tuple[str, str]]:
    """消息文本 → [(原文片段, 归一字母串)]：整组词组 + 组内每个单词，去重保序。

    先去声调记号再切词，避免预组合字符（è/ǚ）把一个拼音词切碎。
    witness 用去记号后的片段（仅用于近似提示文案）。
    """
    out, seen = [], set()
    flat = _strip_marks(text)
    for group in _LATIN_GROUP_RE.findall(flat):
        compact = letters_only(group)
        candidates = ([(group.strip(), compact)] if len(compact) >= MIN_PY_RUN
                      else [])
        candidates += [(r, letters_only(r)) for r in _LATIN_RUN_RE.findall(group)]
        for witness, letters in candidates:
            if len(letters) >= MIN_PY_RUN and letters not in seen:
                seen.add(letters)
                out.append((witness, letters))
    return out


class MemeGuide:
    def __init__(self, db_path: str, log=None):
        self.db_path = db_path
        self.log = log or (lambda msg, level='info': None)
        migrate(db_path)
        self._index: list[dict] = []
        self._pair_maps = ({}, {}, {})
        self._sig = None
        self._reload()

    # ── 索引（按文件 mtime/size 自动刷新，生成脚本写库后无需重启）──
    def _signature(self):
        # WAL 模式下提交可能只写 -wal、主库文件不动，两个文件的状态都要算
        sig = []
        for p in (self.db_path, self.db_path + '-wal', self.db_path + '-shm'):
            try:
                st = os.stat(p)
            except OSError:
                sig.append(None)
            else:
                sig.append((st.st_mtime_ns, st.st_size))
        return tuple(sig)

    def _reload(self) -> None:
        try:
            self._index = (load_index(self.db_path)
                           if os.path.exists(self.db_path) else [])
        except Exception as e:
            self.log(f'梗指南索引加载失败（本轮跳过）: {e}', 'warn')
            self._index = []
        self._pair_maps = ({}, {}, {})
        for m in self._index:
            for f in set(m['cn']):
                if len(f) != FUZZY_CN_LEN:
                    continue
                for k, (i, j) in enumerate(_PAIR_POS):
                    self._pair_maps[k].setdefault(
                        (f[i], f[j]), []).append((m, f))
        self._sig = self._signature()

    def _ensure_index(self) -> list[dict]:
        if self._signature() != self._sig:
            self._reload()
        return self._index

    def invalidate(self) -> None:
        self._sig = None

    # ── 核心查询（自动注入与工具共用）────────────────────────────
    def scan(self, text: str, *, fuzzy: bool = True,
             limit: int = DEFAULT_INJECT_MAX) -> list[MemeHit]:
        """扫描一段文本里出现的梗，按置信度排序后截断到 limit。"""
        text = str(text or '')
        if not text.strip():
            return []
        idx = self._ensure_index()
        if not idx:
            return []
        hits: dict[int, MemeHit] = {}

        # 1) 精确：中文子串（主名/中文别名；名字带标点时忽略消息里的标点差异）
        norm_text = normalize_cn(text)
        exact_cn_fields = set()
        for m in idx:
            for f, nf in zip(m['cn'], m['cn_norm']):
                if f in text or (nf != f and nf and nf in norm_text):
                    if len(f) == FUZZY_CN_LEN:
                        exact_cn_fields.add(f)
                    add_hit(hits, MemeHit(
                        m['id'], m['name'], m['content'], f,
                        'name' if f == m['name'] else 'alias_cn', True,
                        witness=f))
                    break

        # 2) 精确：拼音拉丁段（整组词组或单词；归一后字母串全等，声调/空格/ü 不敏感）
        queries = latin_queries(text)
        exact_py_runs = set()
        if queries:
            for m in idx:
                for witness, letters in queries:
                    if len(letters) < MIN_PY_RUN:
                        continue
                    for py in m['py']:
                        if letters == py['letters']:
                            exact_py_runs.add(letters)
                            add_hit(hits, MemeHit(
                                m['id'], m['name'], m['content'], py['raw'],
                                'pinyin', True, witness=witness))
                            break

        if fuzzy:
            self._scan_fuzzy_cn(text, hits, exact_cn_fields)
            self._scan_partial_cn(norm_text, hits)
            self._scan_fuzzy_py(queries, hits, exact_py_runs)

        return sorted(hits.values(), key=_hit_key)[:max(1, limit)]

    def _scan_fuzzy_cn(self, text: str, hits: dict, exact_fields: set) -> None:
        """三字中文字段错 1 字（替换）。同一 3 字窗歧义命中多梗则弃用。"""
        for i in range(len(text) - (FUZZY_CN_LEN - 1)):
            win = text[i:i + FUZZY_CN_LEN]
            if not all(_is_cjk(c) for c in win):
                continue
            if win in exact_fields:
                continue
            cand = {}
            for k, (a, b) in enumerate(_PAIR_POS):
                for m, f in self._pair_maps[k].get((win[a], win[b]), []):
                    if m['id'] in hits:
                        continue
                    if edit_distance(win, f, cap=1) == 1:
                        cand.setdefault(m['id'], (m, f))
            if len(cand) != 1:
                continue                    # 0 命中或窗口级歧义
            (m, f) = next(iter(cand.values()))
            add_hit(hits, MemeHit(
                m['id'], m['name'], m['content'], f,
                'name' if f == m['name'] else 'alias_cn', False,
                witness=win, distance=1, fuzzy_kind='char'))

    def _scan_partial_cn(self, norm_text: str, hits: dict) -> None:
        """长梗名只说了前半截时命中（「专是专」→「专是专，本是本」）。

        只认去标点后的前缀：至少 PARTIAL_CN_MIN_WINDOW 字、覆盖名字
        PARTIAL_CN_RATIO 以上，且至少含 2 个汉字（挡住纯拉丁前缀）。
        后缀不参与（「本是本」在「我本是本分人」里太容易误报）；
        同一前缀对应多个梗时按歧义弃用。
        """
        if not norm_text:
            return
        cands = []                              # (meme, field, witness)
        witness_ids: dict[str, set] = {}
        for m in self._index:
            for f, nf in zip(m['cn'], m['cn_norm']):
                if len(nf) < PARTIAL_CN_MIN_FIELD:
                    continue
                min_len = max(PARTIAL_CN_MIN_WINDOW,
                              math.ceil(len(nf) * PARTIAL_CN_RATIO))
                # 取消息里出现的最长前缀，比短前缀更具体
                for k in range(len(nf) - 1, min_len - 1, -1):
                    pre = nf[:k]
                    if pre in norm_text:
                        if sum(1 for c in pre if _is_cjk(c)) >= 2:
                            cands.append((m, f, pre))
                            witness_ids.setdefault(pre, set()).add(m['id'])
                        break
        for m, f, pre in cands:
            if m['id'] in hits or len(witness_ids[pre]) != 1:
                continue
            add_hit(hits, MemeHit(
                m['id'], m['name'], m['content'], f,
                'name' if f == m['name'] else 'alias_cn', False,
                witness=pre, distance=2, fuzzy_kind='partial'))

    def _scan_fuzzy_py(self, queries: list, hits: dict, exact_runs: set) -> None:
        """拼音容错：音节距离 1 优先，其次整串字母笔误 ≤2；同段多梗并列则弃用。"""
        idx = self._index
        for witness, letters in queries:
            if len(letters) < MIN_PY_RUN or letters in exact_runs:
                continue
            syls = split_syllables(letters)
            scored = []
            for m in idx:
                if m['id'] in hits:
                    continue
                for py in m['py']:
                    kind, dist = '', 99
                    # 音节级：双方至少 2 个音节（单音节缺/多一个太宽泛，如 nei→nei juan），
                    # 且字母串都 ≥4、长度比 ≥0.6（挡住 iju 这类碎片误配）
                    if (syls and py['syls'] and len(syls) >= 2
                            and len(py['syls']) >= 2
                            and len(letters) >= LETTER_FUZZY_MIN_LEN
                            and len(py['letters']) >= LETTER_FUZZY_MIN_LEN
                            and min(len(letters), len(py['letters']))
                            / max(len(letters), len(py['letters'])) >= 0.6):
                        ds = edit_distance(syls, py['syls'], cap=1)
                        if ds == 1:
                            kind, dist = 'syllable', 1
                    if not kind and (
                            len(letters) >= LETTER_FUZZY_MIN_LEN
                            and len(py['letters']) >= LETTER_FUZZY_MIN_LEN
                            and min(len(letters), len(py['letters']))
                            / max(len(letters), len(py['letters'])) >= 0.6):
                        dl = edit_distance(letters, py['letters'],
                                           cap=LETTER_FUZZY_MAX)
                        if 1 <= dl <= LETTER_FUZZY_MAX:
                            kind, dist = 'letter', dl
                    if kind:
                        scored.append((dist, -len(py['letters']),
                                       m['id'], m, py, kind, witness))
            if not scored:
                continue
            scored.sort(key=lambda x: (x[0], x[1], x[2]))
            best = (scored[0][0], scored[0][1])
            best_ids = {x[2] for x in scored if (x[0], x[1]) == best}
            if len(best_ids) != 1:
                continue                    # 段内歧义，不猜
            _, _, _, m, py, kind, wit = scored[0]
            add_hit(hits, MemeHit(
                m['id'], m['name'], m['content'], py['raw'], 'pinyin', False,
                witness=wit, distance=best[0], fuzzy_kind=kind))

    # ── 渲染 ─────────────────────────────────────────────────────
    @staticmethod
    def _fuzzy_note(h: MemeHit) -> str:
        if h.fuzzy_kind == 'partial':
            return (f'（近似匹配：消息里只出现了「{h.field}」的'
                    f'前半截「{h.witness}」）')
        kind_cn = {'char': '近形错字', 'syllable': '拼音差一个音节',
                   'letter': '拼音差一两个字母'}.get(h.fuzzy_kind, '近似')
        return f'（近似匹配：消息里的「{h.witness}」与「{h.field}」{kind_cn}）'

    def injection_block(self, text: str, cfg: dict | None = None,
                        store=None) -> str:
        """本轮消息 → 一条 system 梗指南块；无命中返回 ''。"""
        cfg = cfg or {}
        limit = int(cfg.get('meme_inject_max', DEFAULT_INJECT_MAX))
        entry_cap = int(cfg.get('meme_entry_max_chars', DEFAULT_ENTRY_CHARS))
        hits = self.scan(text, fuzzy=True, limit=max(1, limit))
        if not hits:
            return ''
        lines = []
        for h in hits:
            content = (h.content or '').strip()
            if len(content) > entry_cap:
                content = content[:entry_cap] + '…'
            note = '' if h.exact else self._fuzzy_note(h)
            lines.append(f'· {h.name}{note}：{content or "（暂无释义）"}')
        return prompts_mod.render(
            'meme_header', store, content='\n'.join(lines))

    def lookup_text(self, query: str, cfg: dict | None = None) -> str:
        """meme_lookup 工具的文本结果；与自动注入走同一个 scan。"""
        q = str(query or '').strip()
        if not q:
            return '错误: query 为空'
        if not self._ensure_index():
            return '梗指南库当前为空（还没有录入任何梗）。'
        hits = self.scan(q, fuzzy=True, limit=5)
        if not hits:
            return (f'没查到与「{q[:30]}」匹配的梗。支持中文全名/别名/拼音；'
                    '三字梗允许错一个字，拼音允许错一个音节或一两个字母。')
        blocks = []
        for h in hits:
            note = '' if h.exact else self._fuzzy_note(h)
            blocks.append(
                f'【{h.name}】{note}\n{(h.content or "").strip() or "（暂无释义）"}')
        out = '\n\n'.join(blocks)
        cap = int((cfg or {}).get('tool_result_max_chars', 2000))
        if len(out) > cap:
            out = out[:cap] + '\n…(已截断)'
        return out
