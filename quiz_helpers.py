"""题库加载、平铺转换、去答案、打分。"""

import json
import threading
from pathlib import Path

_QUIZ_BANK_PATH = Path(__file__).parent / "data" / "quiz_bank.json"
_quiz_bank_cache: dict | None = None
_quiz_bank_lock = threading.Lock()


def _flat_list_to_bank(questions: list) -> dict:
    """把 build_quiz_bank.py 生成的平铺列表转成 _get_quiz 需要的字典格式。"""
    bank: dict = {}
    for q in questions:
        ch = q.get("chapter", "")
        if not ch:
            continue
        if ch not in bank:
            bank[ch] = {"title": q.get("chapter_title", ch), "quizzes": []}
        set_no = q.get("set_no", 1)
        quiz_set = next((s for s in bank[ch]["quizzes"] if s["quiz_index"] == set_no), None)
        if quiz_set is None:
            quiz_set = {"quiz_index": set_no, "questions": []}
            bank[ch]["quizzes"].append(quiz_set)
        qtype = q.get("type", "single_choice")
        converted: dict = {"type": qtype, "question": q.get("question", "")}
        if qtype == "single_choice":
            converted["options"] = q.get("options") or {}
            converted["answer"] = q.get("answer", "")
            converted["explanation"] = q.get("explanation", "")
        else:
            converted["reference_answer"] = q.get("answer", "")
            converted["scoring_keywords"] = q.get("scoring_points") or []
        quiz_set["questions"].append(converted)
    return bank


def load_quiz_bank() -> dict:
    global _quiz_bank_cache
    with _quiz_bank_lock:
        if _quiz_bank_cache is None:
            if not _QUIZ_BANK_PATH.exists():
                _quiz_bank_cache = {}
            else:
                raw = json.loads(_QUIZ_BANK_PATH.read_text(encoding="utf-8"))
                _quiz_bank_cache = _flat_list_to_bank(raw) if isinstance(raw, list) else raw
    return _quiz_bank_cache


def reload_quiz_bank() -> dict:
    global _quiz_bank_cache
    with _quiz_bank_lock:
        _quiz_bank_cache = None
    return load_quiz_bank()


def get_quiz(chapter: str, quiz_index: int) -> list | None:
    bank = load_quiz_bank()
    chapter_data = bank.get(chapter)
    if not chapter_data:
        return None
    for q in chapter_data.get("quizzes", []):
        if q.get("quiz_index") == quiz_index:
            return q.get("questions", [])
    return None


def strip_answers(questions: list) -> list:
    safe = []
    for q in questions:
        item = {k: v for k, v in q.items()
                if k not in ("answer", "explanation", "reference_answer", "scoring_keywords")}
        safe.append(item)
    return safe


def score_quiz(questions: list, answers: list) -> tuple[int, list]:
    """打分：单选每题 15 分；情境题关键词命中 × 5（≤25）。"""
    details = []
    total = 0

    for i, q in enumerate(questions):
        user_ans = answers[i] if i < len(answers) else ""
        qtype = q.get("type", "single_choice")

        if qtype == "single_choice":
            correct_ans = q.get("answer", "")
            is_correct = (str(user_ans).strip().upper() == str(correct_ans).strip().upper())
            pts = 15 if is_correct else 0
            total += pts
            details.append({
                "type": "single_choice",
                "correct": is_correct,
                "user": user_ans,
                "answer": correct_ans,
                "explanation": q.get("explanation", ""),
                "points": pts,
            })

        elif qtype == "scenario":
            keywords = q.get("scoring_keywords", [])
            text = str(user_ans)
            hits = sum(1 for kw in keywords if kw in text)
            pts = min(hits * 5, 25)
            total += pts
            details.append({
                "type": "scenario",
                "correct": pts >= 15,
                "user": user_ans,
                "reference_answer": q.get("reference_answer", ""),
                "keywords_hit": [kw for kw in keywords if kw in text],
                "points": pts,
                "max_points": 25,
            })

    return total, details
