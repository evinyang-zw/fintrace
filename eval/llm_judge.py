"""LLM correctness judge (W3), plus the cache that makes reruns cheap.

Separation of duties matters here: this judge answers *one* question — "is the
answer correct?" — and nothing else. Citation support is measured by
``eval.citation`` against the actual spans, so the two numbers cannot quietly
disagree.

The rubric is versioned. A cached grade is only reused when the question, the
answer text, the model and the rubric version all match; changing the rubric
without bumping ``RUBRIC_VERSION`` would feed old verdicts to a new ruler.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from agent.llm import LLMClient, LLMError, Message, build_llm
from eval.schema import Answer, Grade, Question
from pipelines.config import Settings

RUBRIC_VERSION = "w3.0"

_SYSTEM = """你是 FinTrace 的评分员。给你一个问题、判分要点（key_points）、
标准数值（如果有）和一个候选答案，请判断答案的正确性。

只输出一个 JSON 对象，格式：
{"correctness": 0|1|2, "reason": "不超过 40 字"}

评分标准：
- 2 = 结论正确，关键要点齐全，没有错误断言
- 1 = 结论正确但要点不全，或有一处非致命错误（如漏了单位/期间说明）
- 0 = 结论错误、关键数字错误、答非所问，或对可答题拒答

注意：
- 不要因为措辞、格式、引用句柄（[K1]）扣分；引用由另一个组件单独评。
- 数字按数值判断，允许四舍五入；但单位错误（百万/十亿）算错。
- 如果答案给出了正确数字但结论与数字矛盾（例如说"增长"而实际下降），记 0。
"""

_REPAIR = '上一条不是合法 JSON。只输出 {"correctness": 0|1|2, "reason": "..."}，不要其他文字。'

_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


class LLMJudge:
    name = "llm-v1"

    def __init__(
        self,
        settings: Settings,
        *,
        llm: LLMClient | None = None,
        cache_dir: Path | None = None,
        rubric_version: str = RUBRIC_VERSION,
    ) -> None:
        self._settings = settings
        self._llm = llm or build_llm(settings)
        self._cache_dir = cache_dir or (settings.data_dir / "judge_cache")
        self.rubric_version = rubric_version

    async def grade(self, question: Question, answer: Answer) -> Grade:
        if question.must_refuse:
            if answer.refused:
                return Grade(correctness=None, citation_support=1.0, notes="refused as required")
            return Grade(
                correctness=0,
                citation_support=0.0,
                hallucination=True,
                notes="answered an unanswerable question",
            )
        if answer.refused:
            return Grade(correctness=0, citation_support=0.0, notes="false refusal")

        key = self._cache_key(question, answer)
        cached = self._read_cache(key)
        if cached is not None:
            return cached

        messages = [
            Message("system", _SYSTEM),
            Message("user", self._payload(question, answer)),
        ]
        try:
            result = await self._llm.complete(messages, max_tokens=300, json_mode=True)
            verdict = self._parse(result.text)
            if verdict is None:
                result = await self._llm.complete(
                    [*messages, Message("user", _REPAIR)], max_tokens=200, json_mode=True
                )
                verdict = self._parse(result.text)
        except LLMError as exc:
            return Grade(correctness=None, notes=f"judge_error: {exc}")

        if verdict is None:
            return Grade(correctness=None, notes="judge_error: unparseable verdict")
        grade = Grade(correctness=verdict[0], notes=f"judge: {verdict[1]}")
        self._write_cache(key, grade)
        return grade

    # -- prompt -----------------------------------------------------------

    def _payload(self, question: Question, answer: Answer) -> str:
        parts = [
            f"问题：{question.question}",
            f"类别：{question.category}",
        ]
        if question.period_spec:
            parts.append("财年口径：" + json.dumps(question.period_spec, ensure_ascii=False))
        if question.key_points:
            parts.append("判分要点：\n" + "\n".join(f"- {point}" for point in question.key_points))
        if question.gold is not None and question.gold.value is not None:
            unit = question.gold.unit or ""
            tolerance = question.gold.tolerance
            parts.append(f"标准数值：{question.gold.value:g} {unit}（容差 {tolerance:g}）")
        if question.rubric:
            parts.append("评分细则：\n" + json.dumps(question.rubric, ensure_ascii=False, indent=2))
        parts.append("候选答案：\n" + answer.text.strip())
        return "\n\n".join(parts)

    @staticmethod
    def _parse(text: str) -> tuple[int, str] | None:
        cleaned = _FENCE.sub("", text.strip())
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        raw = payload.get("correctness")
        try:
            score = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if score not in {0, 1, 2}:
            return None
        reason = str(payload.get("reason", "")).strip().replace("\n", " ")[:200]
        return score, reason

    # -- cache ------------------------------------------------------------

    def _cache_key(self, question: Question, answer: Answer) -> str:
        blob = json.dumps(
            {
                "question": question.id,
                "answer": answer.text.strip(),
                "model": self._settings.llm_model,
                "rubric": self.rubric_version,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]

    def _cache_path(self, key: str) -> Path:
        return self._cache_dir / f"{key}.json"

    def _read_cache(self, key: str) -> Grade | None:
        path = self._cache_path(key)
        if not path.is_file():
            return None
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return Grade.model_validate(raw)
        except Exception:
            return None

    def _write_cache(self, key: str, grade: Grade) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(key).write_text(
            json.dumps(grade.model_dump(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
