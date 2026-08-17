from __future__ import annotations

import json
from typing import Any


class ContextBuilder:
    """Layered long-book context assembly.

    The builder keeps authoritative state, author working context, recent prose and retrieved old
    evidence separate. This prevents an author's idea from becoming a story fact and prevents a
    historical rewrite from leaking future character knowledge into the rewritten chapter.
    """

    def __init__(self, db):
        self.db = db

    def recent_author_context(
        self,
        pid: str,
        *,
        limit: int = 10,
        char_budget: int = 5000,
        exclude_latest_user: str | None = None,
    ) -> str:
        rows = self.db.recent_messages(pid, limit=max(limit * 2, limit))
        out: list[str] = []
        used = 0
        skipped_current = False
        for row in rows:
            role = str(row.get("role") or "")
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            if role == "user" and exclude_latest_user and not skipped_current and content == exclude_latest_user.strip():
                skipped_current = True
                continue
            label = "作者" if role == "user" else "编辑"
            line = f"{label}：{content}"
            if used + len(line) > char_budget:
                remaining = char_budget - used
                if remaining > 80:
                    out.append(line[:remaining])
                break
            out.append(line)
            used += len(line)
            if len(out) >= limit:
                break
        return "\n".join(out)

    def build(self, pid: str, query: str, recent_chapters: int = 6) -> str:
        project = self.db.get_project(pid)
        dashboard = self.db.project_dashboard(pid)
        latest_no = int(dashboard.get("current_chapter") or 0)
        start = max(1, latest_no - max(1, int(recent_chapters)) + 1)
        recent = self.db.chapters_range(pid, start, latest_no) if latest_no else []
        facts = self._relevant_facts(pid, query, chapter=latest_no or 1)
        entities = self._relevant_entities(pid, query, chapter=latest_no or 1)
        threads = self._relevant_threads(pid, query, chapter=latest_no or 1)
        summaries = self.db.summaries_for_context(pid)
        history = self.db.search_memory(pid, query, limit=10) if query.strip() else []
        author_ctx = self.recent_author_context(pid, limit=10, char_budget=5000, exclude_latest_user=query)
        return self._render(
            project=project,
            current_chapter=latest_no,
            recent=recent,
            facts=facts,
            entities=entities,
            threads=threads,
            summaries=summaries,
            history=history,
            author_context=author_ctx,
            historical_mode=False,
        )

    def build_at(self, pid: str, chapter_no: int, query: str, *, before: int = 6, after: int = 5) -> str:
        project = self.db.get_project(pid)
        chapter_no = max(1, int(chapter_no))
        recent = self.db.chapters_range(pid, max(1, chapter_no - before), chapter_no)
        downstream = self.db.chapters_range(pid, chapter_no + 1, chapter_no + max(0, after))
        facts = self._relevant_facts(pid, query, chapter=chapter_no)
        entities = self._relevant_entities(pid, query, chapter=chapter_no)
        threads = self._relevant_threads(pid, query, chapter=chapter_no)
        summaries = self.db.summaries_before_chapter(pid, chapter_no)
        history_all = self.db.search_memory(pid, query, limit=18) if query.strip() else []
        past_history: list[dict[str, Any]] = []
        future_constraints: list[dict[str, Any]] = []
        for item in history_all:
            ordinal = self._ordinal_from_memory(pid, item)
            if ordinal is None or ordinal <= chapter_no:
                past_history.append(item)
            else:
                future_constraints.append(item)
        author_ctx = self.recent_author_context(pid, limit=10, char_budget=5000, exclude_latest_user=query)
        base = self._render(
            project=project,
            current_chapter=chapter_no,
            recent=recent,
            facts=facts,
            entities=entities,
            threads=threads,
            summaries=summaries,
            history=past_history,
            author_context=author_ctx,
            historical_mode=True,
        )
        if downstream or future_constraints:
            blocks = [base, "\n# 后文兼容约束（只用于避免改坏后续，不能当作本章人物已知信息）"]
            for row in downstream[:after]:
                blocks.append(f"[后续第{row['ordinal']}章] {row['title']}\n{str(row['content'])[:5000]}")
            for item in future_constraints[:6]:
                blocks.append(f"[后续检索证据] {str(item.get('text') or '')[:3500]}")
            return "\n\n".join(blocks)
        return base

    def compact_if_needed(self, pid: str) -> list[tuple[str, int, int]]:
        dashboard = self.db.project_dashboard(pid)
        current = int(dashboard.get("current_chapter") or 0)
        if current <= 0:
            return []
        ranges: list[tuple[str, int, int]] = []
        definitions = (
            ("arc10", 10),
            ("arc50", 50),
            ("volume100", 100),
            ("era500", 500),
            ("era1000", 1000),
        )
        for level, span in definitions:
            if current % span != 0:
                continue
            start = current - span + 1
            if not self.db.summaries_in_range(pid, level, start, current):
                ranges.append((level, start, current))
        return ranges

    def rollups_covering_chapter(self, pid: str, chapter_no: int) -> list[tuple[str, int, int]]:
        chapter_no = max(1, int(chapter_no))
        current = int(self.db.project_dashboard(pid).get("current_chapter") or chapter_no)
        ranges: list[tuple[str, int, int]] = []
        for level, span in (("arc10", 10), ("arc50", 50), ("volume100", 100), ("era500", 500), ("era1000", 1000)):
            start = ((chapter_no - 1) // span) * span + 1
            end = start + span - 1
            if end <= current:
                ranges.append((level, start, end))
        return ranges

    def _relevant_facts(self, pid: str, query: str, chapter: int) -> list[dict[str, Any]]:
        search = self.db.search_canon(pid, query, limit=40) if query.strip() else []
        # Foundational facts are deliberately kept even when hundreds of newer active
        # facts exist. This prevents old world rules, identities and hard constraints
        # from silently aging out on generic requests such as “继续下一章”.
        anchors = self.db.canon_anchors(pid, limit=60)
        current = self.db.canon_at_chapter(pid, chapter, limit=240)
        return self._merge([*search, *anchors], current, key=lambda x: (x.get("subject"), x.get("predicate")), cap=180)

    def _relevant_entities(self, pid: str, query: str, chapter: int) -> list[dict[str, Any]]:
        search = self.db.search_entities(pid, query, limit=24) if query.strip() else []
        current = self.db.entity_states_at_chapter(pid, chapter, limit=80)
        return self._merge(search, current, key=lambda x: x.get("entity"), cap=60)

    def _relevant_threads(self, pid: str, query: str, chapter: int) -> list[dict[str, Any]]:
        search = self.db.search_threads(pid, query, limit=30) if query.strip() else []
        current = self.db.threads_at_chapter(pid, chapter, limit=100)
        return self._merge(search, current, key=lambda x: x.get("id") or x.get("name"), cap=80)

    @staticmethod
    def _merge(primary: list[dict[str, Any]], secondary: list[dict[str, Any]], *, key, cap: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen = set()
        for row in [*primary, *secondary]:
            if not isinstance(row, dict):
                continue
            marker = key(row)
            if marker in seen:
                continue
            seen.add(marker)
            out.append(row)
            if len(out) >= cap:
                break
        return out

    @staticmethod
    def _render(
        *,
        project: dict[str, Any],
        current_chapter: int,
        recent: list[dict[str, Any]],
        facts: list[dict[str, Any]],
        entities: list[dict[str, Any]],
        threads: list[dict[str, Any]],
        summaries: list[dict[str, Any]],
        history: list[dict[str, Any]],
        author_context: str,
        historical_mode: bool,
    ) -> str:
        blocks = [
            "# 作品身份",
            json.dumps({k: project.get(k) for k in ("title", "genre", "premise", "target_chapters", "audience", "style_notes")}, ensure_ascii=False),
            f"# 当前时间点\n{'历史重写：' if historical_mode else ''}第 {current_chapter} 章附近",
        ]
        if author_context:
            blocks.append("# 作者近期工作上下文（用于承接讨论，不代表正文事实）\n" + author_context)
        if facts:
            blocks.append("# 当前有效设定\n" + json.dumps(facts[:180], ensure_ascii=False, default=str))
        if entities:
            blocks.append("# 人物状态\n" + json.dumps(entities[:60], ensure_ascii=False, default=str))
        if threads:
            blocks.append("# 线索与承诺\n" + json.dumps(threads[:80], ensure_ascii=False, default=str))
        if summaries:
            blocks.append("# 长期剧情摘要\n" + "\n".join(
                f"[{x.get('level')} {x.get('start_chapter')}-{x.get('end_chapter')}] {x.get('content')}" for x in summaries[-24:]
            ))
        if history:
            blocks.append("# 按本轮问题召回的旧剧情证据\n" + "\n\n".join(
                f"[{x.get('kind')}:{x.get('ref_id')} score={x.get('score')}] {str(x.get('text') or '')[:5000]}" for x in history[:10]
            ))
        if recent:
            blocks.append("# 最近正文\n" + "\n\n".join(
                f"[第{x.get('ordinal')}章 {x.get('title')}]\n{str(x.get('content') or '')[:9000]}" for x in recent[-8:]
            ))
        return "\n\n".join(blocks)

    def _ordinal_from_memory(self, pid: str, item: dict[str, Any]) -> int | None:
        if item.get("kind") != "chapter":
            return None
        try:
            artifact = self.db.get_artifact(pid, str(item.get("ref_id")))
            return int(artifact.get("ordinal")) if artifact.get("ordinal") is not None else None
        except Exception:
            return None
