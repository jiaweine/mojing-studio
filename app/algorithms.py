from __future__ import annotations

import re
from collections import Counter
from typing import Any


class NarrativeController:
    """Deterministic long-form control layer.

    Models propose story choices; this controller decides how much long-range pressure the current
    chapter carries, which old promises deserve attention, and whether a new design is too similar
    to recent material. It deliberately contains no provider-specific logic.
    """

    def __init__(self, db):
        self.db = db

    def phase(self, pid: str, chapter_no: int | None = None) -> dict[str, Any]:
        project = self.db.get_project(pid)
        current = int(chapter_no or self.db.next_chapter_number(pid))
        target = max(current, int(project.get("target_chapters") or current))
        progress = max(0.0, min(1.0, (current - 1) / max(1, target)))
        if current <= 12:
            name, purpose = "opening", "建立主角欲望、核心异常与追读承诺"
        elif progress < 0.18:
            name, purpose = "expansion", "扩大世界与对手压力，同时验证核心卖点"
        elif progress < 0.68:
            name, purpose = "development", "持续升级代价、关系和主线，不让中段原地踏步"
        elif progress < 0.88:
            name, purpose = "convergence", "减少无关新坑，推动长期承诺进入兑现窗口"
        else:
            name, purpose = "endgame", "收束核心因果、人物终局选择与主要承诺"
        return {
            "phase": name,
            "purpose": purpose,
            "chapter": current,
            "target_chapters": target,
            "progress": round(progress, 4),
        }

    def planning_brief(self, pid: str, chapter_no: int, request: str = "") -> dict[str, Any]:
        project = self.db.get_project(pid)
        phase = self.phase(pid, chapter_no)
        scheduler = self.db.thread_scheduler_state(pid, limit=80)
        if not isinstance(scheduler, dict):
            scheduler = {}
        priority = []
        for key in ("overdue", "due_soon", "stale", "open_threads"):
            rows = scheduler.get(key) if isinstance(scheduler.get(key), list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or row.get("promise") or "").strip()
                if not name:
                    continue
                item = {
                    "name": name[:180],
                    "status": str(row.get("status") or "open"),
                    "due_chapter": row.get("due_chapter"),
                    "opened_chapter": row.get("opened_chapter"),
                    "latest_state": str(row.get("latest_state") or "")[:500],
                    "source": key,
                }
                if not any(x["name"] == item["name"] for x in priority):
                    priority.append(item)
                if len(priority) >= 8:
                    break
            if len(priority) >= 8:
                break

        progress = phase["progress"]
        max_new = 1 if progress >= 0.75 else 2 if chapter_no >= 80 else 3
        if phase["phase"] == "endgame":
            max_new = 0
        return {
            "phase": phase,
            "must_consider": priority,
            "max_new_promises": max_new,
            "target_words": int(project.get("target_words_per_chapter") or 2500),
            "author_request": str(request or "")[:3000],
            "rolling_horizon": {
                "precise_chapters": 3,
                "detailed_window": 20,
                "cluster_window": 100,
                "far_future": "只锁不可逆目标与终局约束",
            },
        }

    @staticmethod
    def planning_directive(brief: dict[str, Any]) -> str:
        phase = brief.get("phase") or {}
        threads = brief.get("must_consider") or []
        lines = [
            f"当前阶段：{phase.get('purpose') or phase.get('phase') or '持续推进'}。",
            f"本章最多新增 {int(brief.get('max_new_promises') or 0)} 个需要未来兑现的新承诺。",
        ]
        if threads:
            names = "、".join(str(x.get("name") or "") for x in threads[:4] if isinstance(x, dict))
            if names:
                lines.append(f"优先考虑这些长期承诺：{names}。不要求机械回收，但必须有意识地推进、延迟或明确暂缓。")
        lines.append("每个主要场景都必须让人物、信息、关系、资源或危险状态至少有一项真实变化。")
        return "\n".join(lines)

    def book_health(self, pid: str, request: str = "") -> dict[str, Any]:
        next_chapter = self.db.next_chapter_number(pid)
        phase = self.phase(pid, next_chapter)
        scheduler = self.db.thread_scheduler_state(pid, limit=120)
        if not isinstance(scheduler, dict):
            scheduler = {}
        open_rows = self.db.open_threads(pid, limit=120)
        overdue = scheduler.get("overdue") if isinstance(scheduler.get("overdue"), list) else []
        stale = scheduler.get("stale") if isinstance(scheduler.get("stale"), list) else []
        due_soon = scheduler.get("due_soon") if isinstance(scheduler.get("due_soon"), list) else []

        signatures = self.db.recent_signatures(pid, limit=36)
        fields = ("setting", "conflict_shape", "payoff_shape", "dominant_emotion", "resolution_method")
        repeated: dict[str, list[str]] = {}
        for field in fields:
            values = [str(s.get(field) or "").strip() for s in signatures[:18] if str(s.get(field) or "").strip()]
            counts = Counter(values)
            hot = [value for value, count in counts.items() if count >= 3]
            if hot:
                repeated[field] = hot[:5]

        return {
            "phase": phase,
            "thread_debt": {
                "open": len(open_rows),
                "overdue": overdue[:20],
                "due_soon": due_soon[:20],
                "stale": stale[:20],
            },
            "signature_fatigue": {"repeated_patterns": repeated, "sample_size": len(signatures)},
            "request": str(request or "")[:3000],
        }

    @staticmethod
    def novelty_risk(contract: dict[str, Any], recent_signatures: list[dict[str, Any]]) -> dict[str, Any]:
        if not recent_signatures:
            return {"level": "low", "score": 0.0, "matches": [], "advice": []}
        probe = {
            "setting": str(contract.get("setting") or contract.get("location") or "").strip(),
            "conflict_shape": str(contract.get("conflict_shape") or contract.get("opposition") or "").strip(),
            "payoff_shape": str(contract.get("payoff_shape") or contract.get("reveal_or_payoff") or "").strip(),
            "dominant_emotion": str(contract.get("dominant_emotion") or contract.get("emotional_shift") or "").strip(),
            "resolution_method": str(contract.get("resolution_method") or "；".join(map(str, contract.get("character_choices") or []))).strip(),
        }
        score = 0.0
        matches: list[dict[str, Any]] = []
        for field, value in probe.items():
            if not value:
                continue
            norm = NarrativeController._norm(value)
            hit_chapters = []
            for sig in recent_signatures[:30]:
                other = NarrativeController._norm(sig.get(field))
                if not other:
                    continue
                overlap = NarrativeController._similarity(norm, other)
                if overlap >= 0.72:
                    hit_chapters.append(int(sig.get("chapter") or 0))
                    score += 8.0 if field in {"conflict_shape", "resolution_method"} else 5.0
            if hit_chapters:
                matches.append({"field": field, "chapters": hit_chapters[:8]})
        score = round(min(100.0, score), 2)
        level = "high" if score >= 32 else "medium" if score >= 16 else "low"
        advice = []
        if level == "high":
            advice.append("至少改变冲突来源、人物解决方式或场景功能中的两项，不能只换地点和名字。")
        elif level == "medium":
            advice.append("保留必要母题，但让人物付出不同代价或获得不同类型的信息。")
        return {"level": level, "score": score, "matches": matches, "advice": advice}

    @staticmethod
    def _norm(value: Any) -> str:
        return re.sub(r"[^\w\u3400-\u9fff]+", "", str(value or "")).lower()

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        if a in b or b in a:
            return min(len(a), len(b)) / max(1, max(len(a), len(b))) * 0.4 + 0.6
        grams_a = {a[i:i+2] for i in range(max(1, len(a)-1))}
        grams_b = {b[i:i+2] for i in range(max(1, len(b)-1))}
        return len(grams_a & grams_b) / max(1, len(grams_a | grams_b))


class ChapterDesignGuard:
    """Hard structural gate before expensive prose generation."""

    @classmethod
    def evaluate(cls, contract: dict[str, Any], brief: dict[str, Any] | None = None) -> dict[str, Any]:
        brief = brief or {}
        score = 100.0
        severe: list[str] = []
        warnings: list[str] = []

        def require(key: str, label: str, penalty: float = 10.0):
            nonlocal score
            value = contract.get(key)
            empty = value is None or value == "" or value == [] or value == {}
            if empty:
                severe.append(f"缺少{label}")
                score -= penalty

        require("function", "本章功能", 9)
        require("opening_hook", "开场不稳定因素", 8)
        require("goal", "人物本章目标", 10)
        require("opposition", "有效阻力", 10)
        require("reveal_or_payoff", "兑现或新信息", 9)
        require("ending_hook", "章尾行动条件", 8)

        turns = [x for x in (contract.get("turning_points") or []) if str(x).strip()]
        if len(turns) < 2:
            severe.append("有效转折不足，正文容易只发生一件事")
            score -= 14
        choices = [x for x in (contract.get("character_choices") or []) if str(x).strip()]
        if not choices:
            severe.append("缺少人物主动选择，主角会被剧情拖着走")
            score -= 18

        scenes = [x for x in (contract.get("scenes") or []) if isinstance(x, dict)]
        if not scenes:
            severe.append("没有可执行的场景设计")
            score -= 15
        else:
            stagnant = [s for s in scenes if not str(s.get("state_change") or "").strip()]
            if stagnant:
                severe.append(f"有 {len(stagnant)} 个场景没有状态变化")
                score -= min(18, 7 * len(stagnant))
            purposes = [str(s.get("purpose") or "").strip() for s in scenes if str(s.get("purpose") or "").strip()]
            if len(purposes) != len(set(purposes)) and len(purposes) >= 3:
                warnings.append("多个场景功能重复，可能形成换皮拖延")
                score -= 5

        planted = [x for x in (contract.get("threads_to_plant") or []) if str(x).strip()]
        max_new = int(brief.get("max_new_promises") if brief.get("max_new_promises") is not None else 3)
        if len(planted) > max_new:
            severe.append(f"本章新开承诺 {len(planted)} 个，超过当前阶段上限 {max_new} 个")
            score -= min(22, (len(planted) - max_new) * 8)

        must_consider = [x for x in (brief.get("must_consider") or []) if isinstance(x, dict)]
        advanced_text = " ".join(map(str, contract.get("threads_to_advance") or []))
        payoff_text = str(contract.get("reveal_or_payoff") or "")
        if must_consider and not any(str(x.get("name") or "") in (advanced_text + payoff_text) for x in must_consider[:3]):
            warnings.append("高优先级旧承诺没有进入本章推进或兑现考虑")
            score -= 6

        hook = str(contract.get("ending_hook") or "").strip()
        opening = str(contract.get("opening_hook") or "").strip()
        if hook and opening and cls._similar(hook, opening):
            warnings.append("章尾几乎回到开场问题，没有形成新的行动条件")
            score -= 6

        score = round(max(0.0, min(100.0, score)), 2)
        return {"pass": score >= 76 and not severe, "score": score, "severe": severe, "warnings": warnings}

    @staticmethod
    def _similar(a: str, b: str) -> bool:
        a = re.sub(r"\s+", "", a)
        b = re.sub(r"\s+", "", b)
        return bool(a and b and (a in b or b in a))

    @staticmethod
    def candidate_score(guard: dict[str, Any], novelty: dict[str, Any], contract: dict[str, Any]) -> float:
        base = float(guard.get("score") or 0.0)
        novelty_penalty = float(novelty.get("score") or 0.0) * 0.45
        turns = min(4, len(contract.get("turning_points") or [])) * 1.5
        choices = min(3, len(contract.get("character_choices") or [])) * 2.0
        payoff = 4.0 if str(contract.get("reveal_or_payoff") or "").strip() else 0.0
        return round(base - novelty_penalty + turns + choices + payoff, 3)


class ProseDiagnostics:
    """Fast deterministic checks for common machine-written failure modes."""

    CLICHES = (
        "他知道", "她知道", "这一刻", "不知为何", "仿佛在告诉", "命运的齿轮", "空气仿佛凝固",
        "嘴角勾起一抹", "眼中闪过一丝", "深吸一口气", "缓缓开口", "淡淡地说道",
    )

    @classmethod
    def analyze(cls, text: str) -> dict[str, Any]:
        text = str(text or "")
        severe: list[str] = []
        warnings: list[str] = []
        if not text.strip():
            return {"pass": False, "score": 0, "severe": ["正文为空"], "warnings": [], "metrics": {}}

        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        sentences = [s.strip() for s in re.split(r"[。！？!?]", text) if s.strip()]
        units = len(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9]+", text))
        dialogue_chars = sum(len(m) for m in re.findall(r"[“\"]([^”\"]{1,300})[”\"]", text))
        dialogue_ratio = dialogue_chars / max(1, len(text))

        if paragraphs and max(map(len, paragraphs)) > 1400:
            warnings.append("存在超长段落，手机阅读节奏容易发闷")
        if len(paragraphs) <= 3 and units > 1200:
            severe.append("正文段落过少，缺乏可读的场景呼吸")
        if dialogue_ratio > 0.58:
            warnings.append("对话占比过高，可能缺少动作、空间和潜台词承载")

        cliché_hits = {phrase: text.count(phrase) for phrase in cls.CLICHES if text.count(phrase) >= 2}
        if sum(cliché_hits.values()) >= 6:
            warnings.append("套语重复偏多，容易产生模板化文风")

        starts = []
        for sentence in sentences[:180]:
            compact = re.sub(r"\s+", "", sentence)
            if len(compact) >= 4:
                starts.append(compact[:4])
        repeated_starts = {k: v for k, v in Counter(starts).items() if v >= 4}
        if repeated_starts:
            warnings.append("句子起手重复偏多，语气节奏显得机械")

        # Long exact repeated fragments are a much stronger signal than repeated common words.
        compact = re.sub(r"\s+", "", text)
        windows = [compact[i:i+18] for i in range(0, max(0, len(compact) - 17), 18)]
        repeated_windows = [w for w, n in Counter(windows).items() if n >= 3 and len(set(w)) >= 6]
        if repeated_windows:
            severe.append("存在明显的重复句块，需要重写而不是继续扩写")

        score = 100 - len(severe) * 24 - len(warnings) * 7
        return {
            "pass": not severe and score >= 72,
            "score": max(0, score),
            "severe": severe,
            "warnings": warnings,
            "metrics": {
                "story_units": units,
                "paragraphs": len(paragraphs),
                "sentences": len(sentences),
                "dialogue_ratio": round(dialogue_ratio, 3),
                "cliche_hits": cliché_hits,
                "repeated_sentence_starts": repeated_starts,
            },
        }


class RevisionController:
    """Turn heterogeneous reviews into a bounded, non-repetitive rewrite order."""

    @staticmethod
    def plan(
        continuity: dict[str, Any],
        quality: dict[str, Any],
        reader: dict[str, Any],
        diagnostics: dict[str, Any],
        length_review: dict[str, Any],
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        history = history or []
        tasks: list[dict[str, Any]] = []

        def add(priority: int, category: str, note: Any):
            text = str(note or "").strip()
            if text and not any(x["note"] == text for x in tasks):
                tasks.append({"priority": priority, "category": category, "note": text[:700]})

        for note in continuity.get("hard_conflicts") or []:
            add(100, "continuity", note)
        for note in continuity.get("repair_instructions") or []:
            add(94, "continuity", note)
        if not length_review.get("ok", True):
            add(90, "length", length_review.get("note"))
        for note in diagnostics.get("severe") or []:
            add(88, "prose", note)
        for note in quality.get("revision_notes") or []:
            add(78, "quality", note)
        if str(reader.get("drop_risk") or "").lower() == "high":
            add(84, "reader", reader.get("one_fix") or reader.get("why") or "强化中段变化与章尾驱动力")
        for note in continuity.get("soft_risks") or []:
            add(72, "continuity", note)
        for note in diagnostics.get("warnings") or []:
            add(60, "prose", note)

        previous = {str(item.get("fingerprint") or "") for item in history[-3:] if isinstance(item, dict)}
        tasks.sort(key=lambda x: (-x["priority"], x["category"]))
        fingerprint = "|".join(x["category"] + ":" + x["note"][:80] for x in tasks[:8])
        return {
            "must_fix": tasks[:10],
            "fingerprint": fingerprint,
            "repeated_plan": bool(fingerprint and fingerprint in previous),
            "instruction": "先修事实与因果，再修人物主动性和兑现，最后处理文字节奏；不要用新增旁白解释旧问题。",
        }

    @staticmethod
    def history_entry(quality: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "quality_total": float(quality.get("total") or 0.0),
            "fingerprint": str(plan.get("fingerprint") or ""),
            "task_count": len(plan.get("must_fix") or []),
        }
