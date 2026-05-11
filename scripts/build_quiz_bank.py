#!/usr/bin/env python3
"""
解析 data/raw_quiz_text.txt -> data/quiz_bank.json

实际格式：
  第 1 章《全流程销售培训课件》
  📝 第 1 章 · 第 1 套
  Q1 ｜ 单选 题目文字
  A. 选项A
  B. 选项B
  C. 选项C
  D. 选项D
  ✅ 答案：C 📖 解析：解析文字

  Q6 ｜ 情境模拟
  情境描述文字...
  ✅ 参考应答：
  应答文字
  📖 评分要点：
  要点文字
  ❌ 常见错误回答：
  错误文字
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

ROOT   = Path(__file__).parent.parent
INPUT  = ROOT / "data" / "raw_quiz_text.txt"
OUTPUT = ROOT / "data" / "quiz_bank.json"

CHAPTER_KEYS = [
    "beauty_consultant_training_courseware",
    "beauty_consultant_advanced_training",
    "ingredient_knowledge_base",
    "intensive_training_full_content",
    "4skin_specialist_consultation",
    "12_questions_full_professional_script",
    "12_wenzhen_learning_assessment",
    "brand_culture_sharing_system",
    "price_objection_and_culture_scenarios",
]

# 章节头：第 N 章《标题》
RE_CHAPTER  = re.compile(r'^第\s*(\d+)\s*章[《〈【](.+?)[》〉】]')
# 套次头：📝 第 N 章 · 第 M 套
RE_SET      = re.compile(r'📝\s*第\s*\d+\s*章\s*[·•·]\s*第\s*(\d+)\s*套')
# 单选题：Q1 ｜ 单选 题目文字
RE_Q_SINGLE = re.compile(r'^Q(\d+)\s*[｜|]\s*单选\s*(.*)', re.DOTALL)
# 情境题：Q6 ｜ 情境模拟
RE_Q_SCENE  = re.compile(r'^Q(\d+)\s*[｜|]\s*情境模拟')
# 选项
RE_OPTION   = re.compile(r'^([A-D])[.．。]\s*(.*)')
# 单选答案行（答案和解析在同一行）：✅ 答案：C 📖 解析：...
RE_ANS_LINE = re.compile(r'✅\s*答案[：:]\s*([A-Da-d])\s*(?:📖\s*解析[：:]?\s*(.*))?')
# 情境题各块标记
RE_REF_ANS  = re.compile(r'^✅\s*参考应答[：:]?\s*(.*)')
RE_SCORING  = re.compile(r'^📖\s*评分要点[：:]?\s*(.*)')
RE_MISTAKE  = re.compile(r'^❌\s*常见错误回答[：:]?\s*(.*)')


def _collect_block_text(lines: List[str], i: int, stop_patterns: list) -> tuple:
    """从 i 开始收集多行文字，遇到 stop_patterns 任一匹配或新题/章/套时停止。
    返回 (text, new_i)"""
    parts = []
    while i < len(lines):
        l = lines[i].strip()
        if not l:
            i += 1
            continue
        # 遇到新块停止
        if (RE_CHAPTER.match(l) or RE_SET.search(l) or
                RE_Q_SINGLE.match(l) or RE_Q_SCENE.match(l)):
            break
        if any(p.match(l) for p in stop_patterns):
            break
        parts.append(l)
        i += 1
    return ''.join(parts) if len(parts) == 1 else '\n'.join(parts), i


def _split_keywords(text: str) -> List[str]:
    items = text.split('\n')
    return [p.strip() for p in items if p.strip()]


def parse(text: str) -> List[dict]:
    questions: List[dict] = []
    current_chapter_no: Optional[int] = None
    current_chapter_key: Optional[str] = None
    current_title: Optional[str]       = None
    current_set: Optional[int]         = None

    lines = text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        i += 1

        if not line:
            continue

        # ── 章节头 ──────────────────────────────────────────────────────────
        m = RE_CHAPTER.match(line)
        if m:
            current_chapter_no = int(m.group(1))
            current_title      = m.group(2).strip()
            idx = current_chapter_no - 1
            current_chapter_key = CHAPTER_KEYS[idx] if 0 <= idx < len(CHAPTER_KEYS) else f"chapter_{current_chapter_no}"
            current_set = None
            continue

        # ── 套次头 ──────────────────────────────────────────────────────────
        m = RE_SET.search(line)
        if m:
            current_set = int(m.group(1))
            continue

        # ── 单选题 ──────────────────────────────────────────────────────────
        m = RE_Q_SINGLE.match(line)
        if m and current_chapter_key and current_set:
            q_no   = int(m.group(1))
            q_text = m.group(2).strip()

            # 收集多行题目（直到选项或答案行）
            while i < len(lines):
                peek = lines[i].strip()
                if not peek:
                    break
                if (RE_OPTION.match(peek) or RE_ANS_LINE.search(peek) or
                        RE_Q_SINGLE.match(peek) or RE_Q_SCENE.match(peek) or
                        RE_CHAPTER.match(peek) or RE_SET.search(peek)):
                    break
                q_text += ' ' + peek
                i += 1

            # 选项
            options: Dict[str, str] = {}
            while i < len(lines):
                peek = lines[i].strip()
                if not peek:
                    break
                m_opt = RE_OPTION.match(peek)
                if m_opt:
                    options[m_opt.group(1).upper()] = m_opt.group(2).strip()
                    i += 1
                else:
                    break

            # 答案行
            answer      = None
            explanation = ""
            while i < len(lines):
                peek = lines[i].strip()
                if not peek:
                    i += 1
                    continue
                m_ans = RE_ANS_LINE.search(peek)
                if m_ans:
                    answer      = m_ans.group(1).upper()
                    explanation = (m_ans.group(2) or "").strip()
                    i += 1
                    break
                # 遇到新块停止
                if (RE_Q_SINGLE.match(peek) or RE_Q_SCENE.match(peek) or
                        RE_CHAPTER.match(peek) or RE_SET.search(peek)):
                    break
                i += 1

            questions.append({
                "chapter":         current_chapter_key,
                "chapter_title":   current_title,
                "set_no":          current_set,
                "question_no":     q_no,
                "type":            "single_choice",
                "question":        q_text,
                "options":         options if options else None,
                "answer":          answer,
                "explanation":     explanation,
                "scoring_points":  None,
                "common_mistakes": None,
            })
            continue

        # ── 情境题 ──────────────────────────────────────────────────────────
        m = RE_Q_SCENE.match(line)
        if m and current_chapter_key and current_set:
            q_no = int(m.group(1))

            # 情境描述（直到 ✅/📖/❌ 或新题）
            stop = [RE_REF_ANS, RE_SCORING, RE_MISTAKE]
            q_text, i = _collect_block_text(lines, i, stop)

            ref_answer  = ""
            scoring_pts: List[str] = []
            mistakes:    List[str] = []

            while i < len(lines):
                peek = lines[i].strip()
                if not peek:
                    i += 1
                    continue
                if (RE_Q_SINGLE.match(peek) or RE_Q_SCENE.match(peek) or
                        RE_CHAPTER.match(peek) or RE_SET.search(peek)):
                    break

                m_ref = RE_REF_ANS.match(peek)
                if m_ref:
                    inline = m_ref.group(1).strip()
                    i += 1
                    rest, i = _collect_block_text(lines, i, [RE_SCORING, RE_MISTAKE])
                    ref_answer = (inline + '\n' + rest).strip()
                    continue

                m_sc = RE_SCORING.match(peek)
                if m_sc:
                    inline = m_sc.group(1).strip()
                    i += 1
                    rest, i = _collect_block_text(lines, i, [RE_MISTAKE])
                    combined = (inline + '\n' + rest).strip()
                    scoring_pts = _split_keywords(combined)
                    continue

                m_mis = RE_MISTAKE.match(peek)
                if m_mis:
                    inline = m_mis.group(1).strip()
                    i += 1
                    rest, i = _collect_block_text(lines, i, [])
                    combined = (inline + '\n' + rest).strip()
                    mistakes = _split_keywords(combined)
                    continue

                i += 1

            questions.append({
                "chapter":         current_chapter_key,
                "chapter_title":   current_title,
                "set_no":          current_set,
                "question_no":     q_no,
                "type":            "scenario",
                "question":        q_text,
                "options":         None,
                "answer":          ref_answer,
                "explanation":     "",
                "scoring_points":  scoring_pts or None,
                "common_mistakes": mistakes or None,
            })
            continue

    return questions


def validate(questions: List[dict]) -> List[str]:
    warnings = []
    for q in questions:
        loc = f"{q['chapter']} set{q['set_no']} Q{q['question_no']}"
        if q['type'] == 'single_choice':
            if not q.get('options'):
                warnings.append(f"{loc}: 单选题缺少选项")
            if not q.get('answer'):
                warnings.append(f"{loc}: 单选题缺少答案 ✅")
        elif q['type'] == 'scenario':
            if not q.get('scoring_points'):
                warnings.append(f"{loc}: 情境题缺少评分要点")
    return warnings


def stats(questions: List[dict]) -> str:
    chapters: Dict[str, Set[int]] = {}
    for q in questions:
        chapters.setdefault(q['chapter'], set()).add(q['set_no'])
    total_sets = sum(len(v) for v in chapters.values())
    return f"{len(chapters)} 章 / {total_sets} 套 / {len(questions)} 题"


def main():
    if not INPUT.exists() or not INPUT.read_text(encoding='utf-8').strip():
        print("data/raw_quiz_text.txt is empty, please paste quiz text first")
        sys.exit(0)

    text = INPUT.read_text(encoding='utf-8')
    questions = parse(text)

    if not questions:
        print("解析结果为空，请确认格式：")
        print("  章节: 第 N 章《标题》")
        print("  套次: 📝 第 N 章 · 第 M 套")
        print("  单选: Q1 ｜ 单选 题目")
        print("  情境: Q6 ｜ 情境模拟")
        sys.exit(1)

    for w in validate(questions):
        print(f"⚠  {w}")

    OUTPUT.write_text(json.dumps(questions, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"✓ {stats(questions)}")
    print(f"  -> {OUTPUT}")


if __name__ == '__main__':
    main()
