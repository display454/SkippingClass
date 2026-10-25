"""课程分析引擎：讲义 + 课堂录音 -> 结构化考点。

关键设计：
1. 每页讲义有全局页码（多份讲义时连续编号），模型只输出整数页码，
   再由程序映射回「哪份讲义的哪一页」，避免模型自己拼接字符串出错。
2. 内容过长时按「讲义块 + 对应时段的录音」分块抽取，再程序化合并去重，
   不做二次 LLM 汇总，省一次调用也避免信息衰减。
3. 模型输出一律经过 normalize，字段缺失、类型不对、页码越界都会被纠正，
   不会让后续渲染崩掉。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .llm import LLMClient, LLMError

CHUNK_LIMIT = 32000        # 字符数阈值，超过则分块
MAX_KP_PER_CHUNK = 26

SYSTEM_PROMPT = """你是一位一流大学里最懂学生痛点的资深助教。
你的任务是对照课堂讲义和课堂录音转写，提炼出这门课真正重要的内容。

你的判断标准：
- 老师在录音里反复强调、特意停下来解释、或者明说「这个会考」的，是最高优先级
- 讲义上独立成节、有定义/公式/步骤的，是核心知识点
- 只被一句话带过、或者纯粹是举例铺垫的，是次要内容

你的输出必须是严格合法的 JSON 对象，不要输出任何解释文字，不要用 markdown 代码块包裹。"""

USER_TEMPLATE = """## 课程讲义（按页标注，页码用于回溯截图）

{materials}

## 课堂录音转写（带时间戳）

{transcript}

## 任务

结合上面两份材料，输出一份课程分析 JSON，结构如下：

{{
  "topic": "本节课主题，不超过 20 字",
  "summary": "全课概述，200 字以内，说清楚讲了什么、重点在哪",
  "chapters": [
    {{"title": "章节名", "pages": [页码整数], "gist": "这一节讲了什么，60 字内"}}
  ],
  "key_points": [
    {{
      "title": "知识点标题，不超过 20 字",
      "detail": "完整讲解。要把老师讲清楚的地方讲清楚，忠实于原始内容，不要泛泛而谈",
      "type": "概念 / 原理 / 方法 / 公式 / 案例 / 结论 之一",
      "importance": 1到5的整数，5=必考核心，3=需要理解，1=了解即可,
      "pages": [相关的讲义页码整数],
      "timestamp": "录音中讲到它的时间，格式 00:12:30；找不到就填空字符串",
      "exam_hint": "这个点可能怎么考，一句话"
    }}
  ],
  "exam_focus": [
    {{"item": "考点名称", "why": "为什么判断它是考点", "how": "可能的题型与问法", "pages": [页码], "importance": 1到5}}
  ],
  "pitfalls": [
    {{"point": "容易错/容易混淆的地方", "pages": [页码]}}
  ],
  "emphasis": [
    {{"quote": "老师明确强调的要点或近似原话", "timestamp": "00:00:00", "note": "为什么重要"}}
  ],
  "extensions": [
    {{"topic": "延伸主题", "content": "帮助理解这个知识点的背景、类比或延伸。必须标明这是补充而非课上内容", "pages": [页码]}}
  ]
}}

硬性要求：
1. key_points 输出 {min_kp} 到 {max_kp} 条，要覆盖全课，不要只挑开头部分
2. pages 里的数字必须来自上面实际出现的页码，没有任何依据就填 []
3. timestamp 必须能在录音转写里找到对应位置，找不到就留空，绝对不要编造
4. exam_focus 输出 3 到 8 条，按重要度降序
5. 只输出 JSON"""


class CourseContext:
    """把多份讲义摊平成全局页码，并提供全局页到原始页的映射。"""

    def __init__(self, materials: list[dict], transcript_text: str):
        self.materials = materials
        self.transcript_text = transcript_text
        self.entries: list[dict] = []

        g = 0
        for mi, mat in enumerate(materials):
            for page in mat.get("pages", []):
                g += 1
                self.entries.append({
                    "g": g,
                    "m": mi,
                    "p": int(page.get("index", 0)),
                    "name": mat.get("filename", f"讲义{mi + 1}"),
                    "text": page.get("text", ""),
                })

    @property
    def valid_pages(self) -> set[int]:
        return {e["g"] for e in self.entries}

    def resolve(self, g_pages: list[int]) -> list[dict]:
        """全局页 -> [{material_index, page, name}]"""
        by_g = {e["g"]: e for e in self.entries}
        out = []
        for g in g_pages:
            e = by_g.get(int(g))
            if e:
                out.append({"m": e["m"], "page": e["p"], "name": e["name"]})
        return out

    def materials_block(self) -> str:
        parts: list[str] = []
        if len(self.materials) > 1:
            head = "；".join(
                f"讲义{m + 1}《{mat.get('filename', '')}》第 1 页起" for m, mat in enumerate(self.materials)
            )
            parts.append(f"（本课共 {len(self.materials)} 份讲义，页码连续编号：{head}）\n")

        for e in self.entries:
            text = (e["text"] or "").strip()
            if not text:
                parts.append(f"【第{e['g']}页】（本页无可提取文字，可能是图片版式）")
                continue
            label = f"【第{e['g']}页】"
            if len(self.materials) > 1:
                label = f"【第{e['g']}页｜《{e['name']}》第{e['p']}页】"
            parts.append(f"{label}\n{text}")

        return "\n\n".join(parts)

    def chunk(self, n: int) -> list[tuple[str, str]]:
        """把讲义和录音都切成 n 段，按比例配对（时间上大致对应）。"""
        entries = self.entries or []
        per = max(1, len(entries) // n + (1 if len(entries) % n else 0))
        mat_chunks: list[str] = []
        for i in range(0, max(1, len(entries)), per):
            block = entries[i:i + per]
            mat_chunks.append("\n\n".join(
                f"【第{e['g']}页】\n{(e['text'] or '').strip()}"
                for e in block
            ))

        lines = self.transcript_text.splitlines()
        per_line = max(1, len(lines) // n + (1 if len(lines) % n else 0))
        tr_chunks = [
            "\n".join(lines[i:i + per_line])
            for i in range(0, max(1, len(lines)), per_line)
        ]

        while len(mat_chunks) < len(tr_chunks):
            mat_chunks.append("")
        while len(tr_chunks) < len(mat_chunks):
            tr_chunks.append("")

        return list(zip(mat_chunks, tr_chunks))


def analyze(materials: list[dict], transcript_text: str, client: LLMClient,
            on_progress: Callable[[str, float], None] | None = None) -> dict:
    ctx = CourseContext(materials, transcript_text)
    total_chars = len(ctx.materials_block()) + len(transcript_text)

    def tick(msg: str, pct: float) -> None:
        if on_progress:
            on_progress(msg, pct)

    if total_chars <= CHUNK_LIMIT:
        tick("正在通读讲义与录音", 40)
        data = _single_call(ctx, ctx.materials_block(), transcript_text, client)
        tick("整理考点", 82)
        return _normalize(data, ctx, single=True)

    # 超长内容：分块抽取再合并
    parts = max(2, min(4, (total_chars // CHUNK_LIMIT) + 1))
    chunks = ctx.chunk(parts)
    results: list[dict] = []

    for i, (mat_block, tr_block) in enumerate(chunks):
        tick(f"分段分析 {i + 1}/{len(chunks)}", 30 + 55 * (i / len(chunks)))
        try:
            results.append(_single_call(ctx, mat_block, tr_block, client, partial=True))
        except LLMError:
            if i == 0:
                raise
            continue

    tick("合并分段结果", 88)
    return _normalize(_merge_results(results), ctx, single=False)


def _single_call(ctx: CourseContext, mat_block: str, tr_block: str,
                 client: LLMClient, partial: bool = False) -> dict:
    prompt = USER_TEMPLATE.format(
        materials=mat_block or "（本次分析段落无对应讲义内容）",
        transcript=tr_block or "（本次分析段落无对应录音内容）",
        min_kp=8 if partial else 15,
        max_kp=MAX_KP_PER_CHUNK,
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    return client.chat_json(messages, max_tokens=8000)


# ---------------- 结果规整 ----------------

def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _as_text(v: Any, limit: int = 4000) -> str:
    if v is None:
        return ""
    s = v if isinstance(v, str) else str(v)
    return s.strip()[:limit]


def _as_int(v: Any, default: int = 3, lo: int = 1, hi: int = 5) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _as_pages(v: Any, valid: set[int]) -> list[int]:
    out: list[int] = []
    for item in _as_list(v):
        try:
            n = int(float(item))
        except (TypeError, ValueError):
            continue
        if n in valid and n not in out:
            out.append(n)
    return out


_TS = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")


def _as_time(v: Any) -> str:
    s = _as_text(v, 20)
    m = _TS.search(s)
    if not m:
        return ""
    a, b, c = m.group(1), m.group(2), m.group(3)
    if c is None:
        return f"{int(a):02d}:{int(b):02d}"
    return f"{int(a):02d}:{int(b):02d}:{int(c):02d}"


def _normalize(data: dict, ctx: CourseContext, single: bool) -> dict:
    valid = ctx.valid_pages

    key_points = []
    for kp in _as_list(data.get("key_points")):
        if not isinstance(kp, dict):
            continue
        title = _as_text(kp.get("title"), 80)
        detail = _as_text(kp.get("detail"), 3000)
        if not title and not detail:
            continue
        key_points.append({
            "title": title or detail[:20],
            "detail": detail,
            "type": _as_text(kp.get("type"), 12) or "要点",
            "importance": _as_int(kp.get("importance"), 3),
            "pages": _as_pages(kp.get("pages"), valid),
            "timestamp": _as_time(kp.get("timestamp")),
            "exam_hint": _as_text(kp.get("exam_hint"), 300),
        })

    key_points.sort(key=lambda x: (-x["importance"], x["timestamp"] or "99:99:99"))

    exam_focus = []
    for ef in _as_list(data.get("exam_focus")):
        if not isinstance(ef, dict):
            continue
        item = _as_text(ef.get("item"), 100)
        if not item:
            continue
        exam_focus.append({
            "item": item,
            "why": _as_text(ef.get("why"), 500),
            "how": _as_text(ef.get("how"), 500),
            "pages": _as_pages(ef.get("pages"), valid),
            "importance": _as_int(ef.get("importance"), 4),
        })
    exam_focus.sort(key=lambda x: -x["importance"])

    pitfalls = []
    for p in _as_list(data.get("pitfalls")):
        if isinstance(p, dict):
            text = _as_text(p.get("point"), 500)
            if text:
                pitfalls.append({"point": text, "pages": _as_pages(p.get("pages"), valid)})
        elif isinstance(p, str) and p.strip():
            pitfalls.append({"point": p.strip()[:500], "pages": []})

    emphasis = []
    for e in _as_list(data.get("emphasis")):
        if isinstance(e, dict):
            quote = _as_text(e.get("quote"), 800)
            if quote:
                emphasis.append({
                    "quote": quote,
                    "timestamp": _as_time(e.get("timestamp")),
                    "note": _as_text(e.get("note"), 300),
                })

    chapters = []
    for c in _as_list(data.get("chapters")):
        if not isinstance(c, dict):
            continue
        title = _as_text(c.get("title"), 80)
        if title:
            chapters.append({
                "title": title,
                "pages": _as_pages(c.get("pages"), valid),
                "gist": _as_text(c.get("gist"), 300),
            })

    extensions = []
    for x in _as_list(data.get("extensions")):
        if not isinstance(x, dict):
            continue
        topic = _as_text(x.get("topic"), 80)
        content = _as_text(x.get("content"), 1500)
        if topic and content:
            extensions.append({
                "topic": topic,
                "content": content,
                "pages": _as_pages(x.get("pages"), valid),
            })

    return {
        "topic": _as_text(data.get("topic"), 60) or "本次课程",
        "summary": _as_text(data.get("summary"), 1200),
        "chapters": chapters,
        "key_points": key_points,
        "exam_focus": exam_focus[:10],
        "pitfalls": pitfalls[:12],
        "emphasis": emphasis[:12],
        "extensions": extensions[:10],
        "stats": {
            "key_points": len(key_points),
            "pages": len(ctx.entries),
            "materials": len(ctx.materials),
            "chunked": not single,
        },
    }


def _merge_results(results: list[dict]) -> dict:
    """把分块结果合并：按标题去重，重要度取大，页码取并集。"""
    def norm_title(t: str) -> str:
        return re.sub(r"[\s，。、,.\-—_()（）]", "", t or "")

    merged_kp: dict[str, dict] = {}
    for res in results:
        for kp in res.get("key_points") or []:
            if not isinstance(kp, dict):
                continue
            key = norm_title(kp.get("title", ""))[:24]
            if not key:
                continue
            old = merged_kp.get(key)
            if not old:
                merged_kp[key] = dict(kp)
                continue
            old["importance"] = max(int(old.get("importance", 3)), int(kp.get("importance", 3)))
            old["pages"] = list(dict.fromkeys((old.get("pages") or []) + (kp.get("pages") or [])))
            if len(str(kp.get("detail", ""))) > len(str(old.get("detail", ""))):
                old["detail"] = kp["detail"]
            if not old.get("timestamp") and kp.get("timestamp"):
                old["timestamp"] = kp["timestamp"]

    def pick(field: str, key: str) -> list:
        seen: set[str] = set()
        out: list = []
        for res in results:
            for item in res.get(field) or []:
                if not isinstance(item, dict):
                    continue
                k = norm_title(item.get(key, ""))[:24]
                if k and k not in seen:
                    seen.add(k)
                    out.append(item)
        return out

    first = results[0] if results else {}
    topics = [r.get("topic", "") for r in results if r.get("topic")]

    return {
        "topic": first.get("topic", "") or "本次课程",
        "summary": first.get("summary", ""),
        "chapters": pick("chapters", "title"),
        "key_points": list(merged_kp.values()),
        "exam_focus": pick("exam_focus", "item"),
        "pitfalls": pick("pitfalls", "point"),
        "emphasis": pick("emphasis", "quote"),
        "extensions": pick("extensions", "topic"),
        "_topics": topics,
    }
