"""
Teach 站点学习进度存储

数据文件: data/teach_progress.db  (SQLite, WAL 模式)
表:
  checkins         — 每日签到记录
  study_time       — 每日学习时长（秒）
  chapter_progress — 章节进度 + 三套题得分
  quiz_attempts    — 答题历史明细

单连接策略：SQLite 写是串行的，用一个模块级连接 + threading.Lock 即可。
WAL 模式允许并发读，心跳 UPSERT 走单条 SQL，不需要连接池。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_ROOT     = Path(__file__).parent
_DATA_DIR = _ROOT / "data"
_DATA_DIR.mkdir(exist_ok=True)

_DB_PATH = _DATA_DIR / "teach_progress.db"
_lock    = threading.Lock()
_conn: sqlite3.Connection | None = None

# 章节顺序（用于解锁判断）
CHAPTER_ORDER = [
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

CHAPTER_TITLES = {
    "beauty_consultant_training_courseware":  "全流程销售培训课件",
    "beauty_consultant_advanced_training":    "核心技能详解手册",
    "ingredient_knowledge_base":              "成分知识库",
    "intensive_training_full_content":        "问诊技能全套训练",
    "4skin_specialist_consultation":          "四大肤质专项发问",
    "12_questions_full_professional_script":  "12问诊专业话术",
    "12_wenzhen_learning_assessment":         "12问诊学习与考核",
    "brand_culture_sharing_system":           "企业文化分享体系",
    "price_objection_and_culture_scenarios":  "价格异议与15大场景",
}

# 防作弊：内存记录每个用户最近一次心跳时间（进程级）
_last_heartbeat: dict[str, float] = {}
_hb_lock = threading.Lock()

HEARTBEAT_MIN_INTERVAL = 25  # 秒，两次心跳间隔小于此值丢弃


# ── 连接管理 ──────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    """返回模块级单连接，首次调用时初始化数据库。"""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _init_schema(_conn)
        _conn.commit()
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS checkins (
            username TEXT NOT NULL,
            date     TEXT NOT NULL,
            ts       TEXT NOT NULL,
            PRIMARY KEY (username, date)
        );
        CREATE INDEX IF NOT EXISTS idx_checkin_user ON checkins(username);

        CREATE TABLE IF NOT EXISTS study_time (
            username TEXT NOT NULL,
            date     TEXT NOT NULL,
            seconds  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (username, date)
        );

        CREATE TABLE IF NOT EXISTS chapter_progress (
            username     TEXT NOT NULL,
            chapter      TEXT NOT NULL,
            read_done    INTEGER DEFAULT 0,
            quiz1_score  INTEGER,
            quiz2_score  INTEGER,
            quiz3_score  INTEGER,
            unlocked_at  TEXT,
            completed_at TEXT,
            PRIMARY KEY (username, chapter)
        );

        CREATE TABLE IF NOT EXISTS quiz_attempts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT NOT NULL,
            chapter    TEXT NOT NULL,
            quiz_index INTEGER NOT NULL,
            answers    TEXT NOT NULL,
            score      INTEGER NOT NULL,
            details    TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_attempts_user_chapter
            ON quiz_attempts(username, chapter);
    """)


# ── 签到 ─────────────────────────────────────────────────────────────────────

def checkin(username: str) -> dict:
    """
    当天签到一次（幂等）。
    返回 {today_signed, streak, total_days}。
    """
    today = date.today().isoformat()
    now   = datetime.now(timezone.utc).isoformat()
    conn  = _get_conn()

    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO checkins(username, date, ts) VALUES(?,?,?)",
            (username, today, now),
        )
        conn.commit()

        rows = conn.execute(
            "SELECT date FROM checkins WHERE username=? ORDER BY date DESC",
            (username,),
        ).fetchall()

    dates = [r["date"] for r in rows]
    streak = _calc_streak(dates)
    return {
        "today_signed": True,
        "streak":       streak,
        "total_days":   len(dates),
    }


def get_checkin_stats(username: str) -> dict:
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT date FROM checkins WHERE username=? ORDER BY date DESC",
            (username,),
        ).fetchall()

    today  = date.today().isoformat()
    dates  = [r["date"] for r in rows]
    streak = _calc_streak(dates)

    # 最近 30 天有签到的日期列表
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=29)).isoformat()
    last_30 = [d for d in dates if d >= cutoff]

    return {
        "today_signed": today in dates,
        "streak":       streak,
        "total_days":   len(dates),
        "last_30_days": last_30,
    }


def _calc_streak(sorted_dates_desc: list[str]) -> int:
    """从今天往回算连续签到天数。"""
    if not sorted_dates_desc:
        return 0
    from datetime import timedelta
    today = date.today()
    streak = 0
    for i, ds in enumerate(sorted_dates_desc):
        expected = (today - timedelta(days=i)).isoformat()
        if ds == expected:
            streak += 1
        else:
            break
    return streak


# ── 学习时长 ──────────────────────────────────────────────────────────────────

def heartbeat(username: str, chapter: str) -> dict | None:
    """
    记录一次心跳（+30 秒）。
    若距上次心跳 < HEARTBEAT_MIN_INTERVAL 秒则丢弃，返回 None。
    返回 {today_seconds}。
    """
    now_ts = time.time()
    with _hb_lock:
        last = _last_heartbeat.get(username, 0)
        if now_ts - last < HEARTBEAT_MIN_INTERVAL:
            return None
        _last_heartbeat[username] = now_ts

    today = date.today().isoformat()
    conn  = _get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO study_time(username, date, seconds) VALUES(?,?,30)
            ON CONFLICT(username, date) DO UPDATE SET seconds = seconds + 30
            """,
            (username, today),
        )
        conn.commit()
        row = conn.execute(
            "SELECT seconds FROM study_time WHERE username=? AND date=?",
            (username, today),
        ).fetchone()

    return {"today_seconds": row["seconds"] if row else 30}


def get_study_stats(username: str) -> dict:
    today = date.today().isoformat()
    conn  = _get_conn()
    with _lock:
        today_row = conn.execute(
            "SELECT seconds FROM study_time WHERE username=? AND date=?",
            (username, today),
        ).fetchone()
        total_row = conn.execute(
            "SELECT SUM(seconds) AS s FROM study_time WHERE username=?",
            (username,),
        ).fetchone()

    today_s = today_row["seconds"] if today_row else 0
    total_s = total_row["s"] or 0
    return {
        "today_seconds": today_s,
        "today_minutes": round(today_s / 60, 1),
        "total_seconds": total_s,
    }


# ── 章节进度 ──────────────────────────────────────────────────────────────────

def _ensure_chapter_row(conn: sqlite3.Connection, username: str, chapter: str) -> None:
    """确保 chapter_progress 里存在该行（第1章自动解锁）。"""
    now = datetime.now(timezone.utc).isoformat()
    if chapter == CHAPTER_ORDER[0]:
        conn.execute(
            """
            INSERT OR IGNORE INTO chapter_progress(username, chapter, unlocked_at)
            VALUES(?,?,?)
            """,
            (username, chapter, now),
        )
    else:
        conn.execute(
            "INSERT OR IGNORE INTO chapter_progress(username, chapter) VALUES(?,?)",
            (username, chapter),
        )


def get_progress(username: str) -> dict:
    """返回 /me/stats 里的 progress 字段。"""
    conn = _get_conn()
    with _lock:
        # 确保第1章行存在
        _ensure_chapter_row(conn, CHAPTER_ORDER[0], username) if False else None
        _ensure_first_chapter(conn, username)
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM chapter_progress WHERE username=?",
            (username,),
        ).fetchall()

    progress_map = {r["chapter"]: r for r in rows}

    chapters = []
    completed_count = 0
    current_chapter = CHAPTER_ORDER[0]

    for key in CHAPTER_ORDER:
        row = progress_map.get(key)
        unlocked    = bool(row and row["unlocked_at"])
        completed   = bool(row and row["completed_at"])
        read_done   = bool(row and row["read_done"])
        q1 = row["quiz1_score"] if row else None
        q2 = row["quiz2_score"] if row else None
        q3 = row["quiz3_score"] if row else None

        chapters.append({
            "key":         key,
            "title":       CHAPTER_TITLES.get(key, key),
            "unlocked":    unlocked,
            "read_done":   read_done,
            "quiz1_score": q1,
            "quiz2_score": q2,
            "quiz3_score": q3,
            "completed":   completed,
        })

        if completed:
            completed_count += 1
        if unlocked and not completed:
            current_chapter = key

    return {
        "current_chapter":  current_chapter,
        "completed_count":  completed_count,
        "total_count":      len(CHAPTER_ORDER),
        "chapters":         chapters,
    }


def _ensure_first_chapter(conn: sqlite3.Connection, username: str) -> None:
    """第1章对所有用户默认解锁。"""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO chapter_progress(username, chapter, unlocked_at)
        VALUES(?,?,?)
        """,
        (username, CHAPTER_ORDER[0], now),
    )


def mark_read(username: str, chapter: str) -> bool:
    """将某章标记为已读（可选功能，前端可调用）。"""
    if chapter not in CHAPTER_ORDER:
        return False
    conn = _get_conn()
    with _lock:
        _ensure_first_chapter(conn, username)
        conn.execute(
            """
            INSERT INTO chapter_progress(username, chapter, read_done)
            VALUES(?,?,1)
            ON CONFLICT(username, chapter) DO UPDATE SET read_done=1
            """,
            (username, chapter),
        )
        conn.commit()
    return True


def is_chapter_unlocked(username: str, chapter: str) -> bool:
    """后端鉴权用：判断该用户是否有权访问某章。"""
    if chapter == CHAPTER_ORDER[0]:
        return True
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT unlocked_at FROM chapter_progress WHERE username=? AND chapter=?",
            (username, chapter),
        ).fetchone()
    return bool(row and row["unlocked_at"])


# ── 答题 ─────────────────────────────────────────────────────────────────────

def save_quiz_attempt(
    username:   str,
    chapter:    str,
    quiz_index: int,
    answers:    list,
    score:      int,
    details:    list,
) -> dict:
    """
    保存一次答题结果，更新 chapter_progress，若三套全过则解锁下一章。
    返回 {score, passed, chapter_completed, next_unlocked}。
    """
    if chapter not in CHAPTER_ORDER:
        return {"ok": False, "error": "invalid chapter"}

    passed    = score >= 60
    now       = datetime.now(timezone.utc).isoformat()
    conn      = _get_conn()

    with _lock:
        _ensure_first_chapter(conn, username)

        # 写答题历史
        conn.execute(
            """
            INSERT INTO quiz_attempts(username,chapter,quiz_index,answers,score,details,created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                username, chapter, quiz_index,
                json.dumps(answers, ensure_ascii=False),
                score,
                json.dumps(details, ensure_ascii=False),
                now,
            ),
        )

        # 更新 chapter_progress 中对应的 quiz 分数（只保留最高分）
        col_map = {1: "quiz1_score", 2: "quiz2_score", 3: "quiz3_score"}
        col = col_map.get(quiz_index)
        if col and passed:
            # 确保行存在
            conn.execute(
                "INSERT OR IGNORE INTO chapter_progress(username,chapter) VALUES(?,?)",
                (username, chapter),
            )
            conn.execute(
                f"""
                UPDATE chapter_progress
                SET {col} = MAX(COALESCE({col}, 0), ?)
                WHERE username=? AND chapter=?
                """,
                (score, username, chapter),
            )

        conn.commit()

        # 读最新进度判断是否三套全过
        row = conn.execute(
            "SELECT quiz1_score, quiz2_score, quiz3_score, completed_at FROM chapter_progress "
            "WHERE username=? AND chapter=?",
            (username, chapter),
        ).fetchone()

        chapter_completed = False
        next_unlocked     = None

        if row:
            q1 = row["quiz1_score"] or 0
            q2 = row["quiz2_score"] or 0
            q3 = row["quiz3_score"] or 0
            all_passed = q1 >= 60 and q2 >= 60 and q3 >= 60

            if all_passed and not row["completed_at"]:
                chapter_completed = True
                conn.execute(
                    "UPDATE chapter_progress SET completed_at=? WHERE username=? AND chapter=?",
                    (now, username, chapter),
                )
                # 解锁下一章
                idx = CHAPTER_ORDER.index(chapter)
                if idx + 1 < len(CHAPTER_ORDER):
                    nxt = CHAPTER_ORDER[idx + 1]
                    conn.execute(
                        """
                        INSERT INTO chapter_progress(username, chapter, unlocked_at)
                        VALUES(?,?,?)
                        ON CONFLICT(username, chapter) DO UPDATE
                        SET unlocked_at = COALESCE(unlocked_at, ?)
                        """,
                        (username, nxt, now, now),
                    )
                    next_unlocked = nxt
                conn.commit()
            elif all_passed and row["completed_at"]:
                chapter_completed = True
                idx = CHAPTER_ORDER.index(chapter)
                if idx + 1 < len(CHAPTER_ORDER):
                    next_unlocked = CHAPTER_ORDER[idx + 1]

    return {
        "score":             score,
        "passed":            passed,
        "chapter_completed": chapter_completed,
        "next_unlocked":     next_unlocked,
    }


def get_quiz_history(username: str, chapter: str, limit: int = 10) -> list[dict]:
    """返回某章最近 N 条答题历史。"""
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            """
            SELECT quiz_index, score, details, created_at
            FROM quiz_attempts
            WHERE username=? AND chapter=?
            ORDER BY id DESC LIMIT ?
            """,
            (username, chapter, limit),
        ).fetchall()

    result = []
    for r in rows:
        details = []
        try:
            details = json.loads(r["details"]) if r["details"] else []
        except Exception:
            pass
        result.append({
            "quiz_index": r["quiz_index"],
            "score":      r["score"],
            "details":    details,
            "created_at": r["created_at"],
        })
    return result
