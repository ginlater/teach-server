"""
Teach 站点独立账号体系

数据文件: data/teach_users.json
格式: {"用户名": {"key": "密码明文", "enabled": true, "note": ""}, ...}

与主站 quota_store 完全隔离，日志写 data/teach_oplogs.json。
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import date, datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent
_DATA_DIR = _ROOT / "data"
_DATA_DIR.mkdir(exist_ok=True)

_USERS_FILE  = _DATA_DIR / "teach_users.json"
_LOG_FILE    = _DATA_DIR / "teach_oplogs.json"
_CRED_LOG    = _DATA_DIR / "teach_credentials.json"  # 账号密码台账（管理员用）
_lock        = threading.Lock()


# ── 用户读写 ─────────────────────────────────────────────────────────────────

def _read_users() -> dict:
    if not _USERS_FILE.exists():
        return {}
    try:
        return json.loads(_USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_users(users: dict) -> None:
    tmp = _USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_USERS_FILE)


# ── 公开接口 ─────────────────────────────────────────────────────────────────

def verify_user(username: str, key: str) -> bool:
    """常量时间校验，enabled=false 直接拒绝。不检查过期（登录时单独判断）。"""
    with _lock:
        users = _read_users()
    user = users.get(username)
    if not user:
        return False
    if user.get("enabled") is False:
        return False
    expected = user.get("key", "")
    if not expected:
        return False
    return secrets.compare_digest(expected, key)


def is_user_active(username: str) -> tuple[bool, str]:
    """
    检查账号是否处于有效期内。
    返回 (True, "") 表示有效；返回 (False, reason) 表示无效。
    admin 账号跳过过期检查。
    """
    with _lock:
        users = _read_users()
    user = users.get(username, {})
    if not user:
        return False, "账号不存在"
    if user.get("enabled") is False:
        return False, "账号已禁用"
    if user.get("role") == "admin":
        return True, ""
    expires_on = user.get("expires_on")
    if expires_on:
        try:
            exp_date = date.fromisoformat(expires_on)
            if date.today() > exp_date:
                return False, f"账号已于 {expires_on} 到期，请联系管理员续期"
        except ValueError:
            pass
    return True, ""


def get_user_info(username: str) -> dict:
    """返回完整用户信息（含 role / allowed_chapters / allowed_quizzes）。"""
    with _lock:
        users = _read_users()
    return users.get(username, {})


def list_users() -> list[dict]:
    with _lock:
        users = _read_users()
    return [
        {
            "username": u,
            "enabled":  v.get("enabled", True),
            "role":     v.get("role", "normal"),
            "note":     v.get("note", ""),
        }
        for u, v in users.items()
    ]


def add_user(
    username: str,
    key: str,
    note: str = "",
    role: str = "normal",
    expires_on: str | None = None,
    allowed_chapters: list | None = None,
    allowed_quizzes: dict | None = None,
) -> bool:
    """
    添加或更新用户，同时写入账号密码台账。

    expires_on: ISO 日期字符串 "YYYY-MM-DD"，不传则永不过期。
    allowed_chapters: 允许访问的章节 key 列表；不传则可访问全部章节。
    allowed_quizzes:  每章允许做的题套号列表，如 {"ch_key": [1,2]}；不传则全套可做。
    """
    if not username or not key:
        return False
    with _lock:
        users = _read_users()
        entry: dict = {"key": key, "enabled": True, "note": note}
        if role != "normal":
            entry["role"] = role
        if expires_on is not None:
            entry["expires_on"] = expires_on
        if allowed_chapters is not None:
            entry["allowed_chapters"] = allowed_chapters
        if allowed_quizzes is not None:
            entry["allowed_quizzes"] = allowed_quizzes
        users[username] = entry
        _write_users(users)
        _append_cred_log(username, key, note, "add")
    return True


def delete_user(username: str) -> bool:
    with _lock:
        users = _read_users()
        if username not in users:
            return False
        key = users[username].get("key", "")
        note = users[username].get("note", "")
        del users[username]
        _write_users(users)
        _append_cred_log(username, key, note, "delete")
    return True


def update_user_fields(
    username: str,
    expires_on: str,
    enabled: bool,
    allowed_chapters,  # list[str] | None; None 表示不限制章节
) -> bool:
    """更新账号的 expires_on / enabled / allowed_chapters。"""
    with _lock:
        users = _read_users()
        if username not in users:
            return False
        u = users[username]
        if expires_on:
            u["expires_on"] = expires_on
        else:
            u.pop("expires_on", None)
        u["enabled"] = enabled
        if allowed_chapters is None:
            u.pop("allowed_chapters", None)
        else:
            u["allowed_chapters"] = allowed_chapters
        _write_users(users)
    return True


def set_enabled(username: str, enabled: bool) -> bool:
    with _lock:
        users = _read_users()
        if username not in users:
            return False
        users[username]["enabled"] = enabled
        _write_users(users)
    return True


# ── 单设备登录会话 ────────────────────────────────────────────────────────────

def set_session_id(username: str, session_id: str) -> None:
    """登录时调用：把最新 session_id 写入用户记录，旧设备 token 自然失效。"""
    with _lock:
        users = _read_users()
        if username not in users:
            return
        users[username]["session_id"] = session_id
        _write_users(users)


def get_session_id(username: str) -> str:
    with _lock:
        users = _read_users()
    return users.get(username, {}).get("session_id", "")


# ── 账号密码台账（管理员可查） ────────────────────────────────────────────────

def _append_cred_log(username: str, key: str, note: str, action: str) -> None:
    """每次增删账号时写一条记录到台账，供管理员查阅账号密码。"""
    entry = {
        "ts":       datetime.now(timezone.utc).isoformat(),
        "username": username,
        "password": key,
        "note":     note,
        "action":   action,   # add / delete
    }
    logs: list = []
    if _CRED_LOG.exists():
        try:
            logs = json.loads(_CRED_LOG.read_text(encoding="utf-8"))
        except Exception:
            logs = []
    logs.append(entry)
    _CRED_LOG.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")


def get_credentials() -> list[dict]:
    """返回账号密码台账（含历史操作）。"""
    if not _CRED_LOG.exists():
        return []
    try:
        return json.loads(_CRED_LOG.read_text(encoding="utf-8"))
    except Exception:
        return []


def get_current_users_with_passwords() -> list[dict]:
    """返回当前所有有效账号及密码。"""
    with _lock:
        users = _read_users()
    return [
        {
            "username": u,
            "password": v.get("key", ""),
            "enabled":  v.get("enabled", True),
            "note":     v.get("note", ""),
        }
        for u, v in users.items()
    ]


# ── 操作日志 ─────────────────────────────────────────────────────────────────

def append_log(username: str, log_type: str, ip: str | None) -> None:
    entry = {
        "ts":       datetime.now(timezone.utc).isoformat(),
        "username": username,
        "type":     log_type,
        "ip":       ip or "",
    }
    with _lock:
        logs: list = []
        if _LOG_FILE.exists():
            try:
                logs = json.loads(_LOG_FILE.read_text(encoding="utf-8"))
            except Exception:
                logs = []
        logs.append(entry)
        _LOG_FILE.write_text(json.dumps(logs, ensure_ascii=False), encoding="utf-8")


def get_logs(limit: int = 200) -> list:
    if not _LOG_FILE.exists():
        return []
    try:
        logs = json.loads(_LOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return sorted(logs, key=lambda r: r.get("ts", ""), reverse=True)[:limit]
