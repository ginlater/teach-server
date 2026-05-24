"""
teach.aibeautyfulwomen.com — 独立 FastAPI 服务

端口: 8001（由 nginx 全域名反代到这里）

包含:
  - 静态页面挂载在 /        （teach/teach.html 等）
  - /teach/*               学员侧 API（登录、打卡、心跳、答题、进度）
  - /platform/teach/*      Platform 管理后台跨服务调用（共享 JWT 密钥）
"""

import re
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from auth import create_token, verify_token
import teach_user_store as _teach_store
import teach_progress_store as _progress_store
import quiz_helpers

ROOT = Path(__file__).parent
TEACH_DIR = ROOT / "teach"

app = FastAPI(title="Teach Server")


# ── Pydantic 请求模型 ─────────────────────────────────────────────────────────

class TeachLoginRequest(BaseModel):
    username: str
    password: str


class TeachHeartbeatRequest(BaseModel):
    chapter: str = ""


class TeachQuizSubmitRequest(BaseModel):
    chapter: str
    quiz_index: int
    answers: list


class TeachUserRequest(BaseModel):
    username: str
    password: str
    note: str = ""


class CreateTeachUsersRequest(BaseModel):
    mode: str  # "batch" | "single"
    company_prefix: str | None = None
    count: int | None = None
    username: str | None = None
    expires_on: str
    allowed_chapters: list[str] | None = None


class UpdateTeachUserRequest(BaseModel):
    expires_on: str = ""
    enabled: bool = True
    allowed_chapters: list[str] | None = None


# ── 鉴权辅助 ──────────────────────────────────────────────────────────────────

def _check_single_device(payload: dict) -> JSONResponse | None:
    """
    校验单设备登录：token 里的 sid 必须等于服务端记录的 session_id。
    admin 账号不受限制；用户记录没有 session_id 时也跳过（向后兼容）。
    """
    username = payload.get("phone")
    if not username:
        return None
    info = _teach_store.get_user_info(username)
    if info.get("role") == "admin":
        return None
    saved_sid = info.get("session_id", "")
    if not saved_sid:
        return None
    if payload.get("sid") != saved_sid:
        return JSONResponse(status_code=401, content={
            "ok": False,
            "error": "您的账号已在其他设备登录，已被挤下线",
            "kicked_out": True,
        })
    return None


def _teach_require(request: Request):
    """返回 (username, None) 或 (None, JSONResponse)。"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, JSONResponse(status_code=401, content={"ok": False, "error": "未登录"})
    payload = verify_token(auth[7:])
    if not payload or payload.get("company") != "teach":
        return None, JSONResponse(status_code=401, content={"ok": False, "error": "token 无效或已过期"})
    kicked = _check_single_device(payload)
    if kicked:
        return None, kicked
    username = payload.get("phone")
    active, reason = _teach_store.is_user_active(username)
    if not active:
        return None, JSONResponse(status_code=403, content={"ok": False, "error": reason})
    return username, None


def _teach_token_required(request: Request):
    """轻量校验（不查账号状态），适用于 /teach/users、/teach/credentials 这类管理路由。"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, JSONResponse(status_code=401, content={"error": "未登录"})
    payload = verify_token(auth_header[7:])
    if not payload or payload.get("company") != "teach":
        return None, JSONResponse(status_code=401, content={"error": "token 无效或已过期"})
    kicked = _check_single_device(payload)
    if kicked:
        return None, kicked
    return payload, None


def _require_platform(request: Request):
    """校验 platform 管理员 token（与主站共享 JWT_SECRET）。"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"error": "未登录"}), None
    payload = verify_token(auth[7:])
    if not payload or payload.get("company") != "platform":
        return JSONResponse(status_code=401, content={"error": "token 无效或已过期"}), None
    return None, payload


def _user_chapter_access(username: str, info: dict) -> dict[str, dict]:
    """
    计算用户对每个章节的访问状态：{chapter_key: {'allowed','unlocked','completed'}}。

    - allowed：在 allowed_chapters 列表里（或用户没有 allowed_chapters 设置，视作全开）
    - unlocked：用户「已开通的章节序列」中处于已解锁位置——
      序列中第一个永远解锁，后续每个仅当前一个已 completed 才解锁
    - completed：该章节三套题均通过
    """
    allowed_chapters = info.get("allowed_chapters")
    progress = _progress_store.get_progress(username)
    chapter_map = {c["key"]: c for c in progress["chapters"]}
    completed_set = {k for k, c in chapter_map.items() if c["completed"]}

    if allowed_chapters is None:
        sequence = list(_progress_store.CHAPTER_ORDER)
    else:
        allowed_set = set(allowed_chapters)
        sequence = [k for k in _progress_store.CHAPTER_ORDER if k in allowed_set]

    seq_unlocked: set[str] = set()
    prev_completed = True  # 第一个永远视作"前一个已完成"
    for k in sequence:
        if prev_completed:
            seq_unlocked.add(k)
        prev_completed = k in completed_set

    sequence_set = set(sequence)
    return {
        k: {
            "allowed":   k in sequence_set,
            "unlocked":  k in seq_unlocked,
            "completed": k in completed_set,
        }
        for k in _progress_store.CHAPTER_ORDER
    }


def _teach_chapter_allowed(username: str, chapter: str, quiz_index: int | None = None):
    """
    统一的章节/题目访问权限检查。返回 None 表示允许，返回 JSONResponse 表示拒绝。
    """
    info = _teach_store.get_user_info(username)

    if info.get("role") == "admin":
        return None

    access = _user_chapter_access(username, info)
    state = access.get(chapter)
    if not state or not state["allowed"]:
        return JSONResponse(status_code=403, content={
            "ok": False, "error": "该章节未开通权限，请联系管理员开通"
        })
    if not state["unlocked"]:
        return JSONResponse(status_code=403, content={
            "ok": False, "error": "请先完成前一章节的三套题闯关后再访问该章节"
        })
    return None


def _get_teach_chapters_from_html() -> list[dict]:
    """从 teach.html 中正则提取章节定义，与 quota_store.get_teach_chapters 等价。"""
    teach_html = TEACH_DIR / "teach.html"
    if not teach_html.exists():
        return []
    try:
        content = teach_html.read_text(encoding="utf-8")
    except Exception:
        return []
    tag_re = re.compile(r'<\w+\b[^>]*class="[^"]*\bnav-item\b[^"]*"[^>]*>')
    key_re = re.compile(r'data-key="([^"]+)"')
    title_re = re.compile(r'data-title="([^"]+)"')
    chapters = []
    for m in tag_re.finditer(content):
        tag = m.group(0)
        km = key_re.search(tag)
        tm = title_re.search(tag)
        if km and tm:
            chapters.append({"key": km.group(1), "title": tm.group(1)})
    return chapters


# ── 健康检查 + 首页 ───────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"ok": True, "service": "teach-server"}


@app.get("/")
def index():
    return FileResponse(TEACH_DIR / "teach.html")


# ── /teach/login ──────────────────────────────────────────────────────────────

@app.post("/teach/login")
def teach_login(req: TeachLoginRequest, request: Request):
    if not req.username or not req.password:
        return JSONResponse(status_code=400, content={"error": "账号或密码不能为空"})
    if not _teach_store.verify_user(req.username, req.password):
        ip = request.client.host if request.client else None
        _teach_store.append_log(req.username, "login_fail", ip)
        return JSONResponse(status_code=401, content={"error": "账号或密码错误"})
    active, reason = _teach_store.is_user_active(req.username)
    if not active:
        ip = request.client.host if request.client else None
        _teach_store.append_log(req.username, "login_fail", ip)
        return JSONResponse(status_code=403, content={"error": reason})
    ip = request.client.host if request.client else None
    # admin 不受单设备限制；其他账号每次登录刷新 session_id 把旧设备挤下线
    info = _teach_store.get_user_info(req.username)
    session_id: str | None = None
    if info.get("role") != "admin":
        old_sid = info.get("session_id", "")
        session_id = secrets.token_urlsafe(16)
        _teach_store.set_session_id(req.username, session_id)
        if old_sid:
            _teach_store.append_log(req.username, "kicked_previous_device", ip)
    _teach_store.append_log(req.username, "login", ip)
    token = create_token(
        req.username, f"teach:{req.username}", company="teach", session_id=session_id,
    )
    return {"token": token, "username": req.username}


# ── /teach/auth/verify ────────────────────────────────────────────────────────

@app.get("/teach/auth/verify")
def teach_verify(request: Request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"error": "未登录"})
    payload = verify_token(auth_header[7:])
    if not payload or payload.get("company") != "teach":
        return JSONResponse(status_code=401, content={"error": "token 无效或已过期"})
    kicked = _check_single_device(payload)
    if kicked:
        return kicked
    return {"ok": True, "username": payload.get("phone")}


# ── /teach/logs ────────────────────────────────────────────────────────────────

@app.get("/teach/logs")
def teach_get_logs(request: Request):
    _, err = _teach_token_required(request)
    if err:
        return err
    return {"logs": _teach_store.get_logs()}


# ── /teach/users ──────────────────────────────────────────────────────────────

@app.get("/teach/users")
def teach_list_users(request: Request):
    _, err = _teach_token_required(request)
    if err:
        return err
    return {"users": _teach_store.list_users()}


@app.post("/teach/users")
def teach_add_user(req: TeachUserRequest, request: Request):
    _, err = _teach_token_required(request)
    if err:
        return err
    ok = _teach_store.add_user(req.username, req.password, req.note)
    return {"ok": ok}


@app.delete("/teach/users/{username}")
def teach_delete_user(username: str, request: Request):
    _, err = _teach_token_required(request)
    if err:
        return err
    ok = _teach_store.delete_user(username)
    return {"ok": ok}


# ── /teach/credentials ────────────────────────────────────────────────────────

@app.get("/teach/credentials")
def teach_get_credentials(request: Request):
    _, err = _teach_token_required(request)
    if err:
        return err
    return {
        "current": _teach_store.get_current_users_with_passwords(),
        "history": _teach_store.get_credentials(),
    }


# ── /teach/checkin ────────────────────────────────────────────────────────────

@app.post("/teach/checkin")
def teach_checkin(request: Request):
    username, err = _teach_require(request)
    if err:
        return err
    result = _progress_store.checkin(username)
    return {"ok": True, **result}


# ── /teach/heartbeat ──────────────────────────────────────────────────────────

@app.post("/teach/heartbeat")
def teach_heartbeat(req: TeachHeartbeatRequest, request: Request):
    username, err = _teach_require(request)
    if err:
        return err
    result = _progress_store.heartbeat(username, req.chapter)
    if result is None:
        stats = _progress_store.get_study_stats(username)
        return {"ok": True, "throttled": True, "today_seconds": stats["today_seconds"]}
    return {"ok": True, "throttled": False, **result}


# ── /teach/me/stats ───────────────────────────────────────────────────────────

@app.get("/teach/me/stats")
def teach_me_stats(request: Request):
    username, err = _teach_require(request)
    if err:
        return err
    checkin_stats = _progress_store.get_checkin_stats(username)
    study_stats = _progress_store.get_study_stats(username)

    info = _teach_store.get_user_info(username)
    role = info.get("role", "normal")

    if role == "admin":
        chapters = [
            {
                "key":         key,
                "title":       _progress_store.CHAPTER_TITLES.get(key, key),
                "allowed":     True,
                "unlocked":    True,
                "read_done":   True,
                "quiz1_score": 100,
                "quiz2_score": 100,
                "quiz3_score": 100,
                "completed":   True,
            }
            for key in _progress_store.CHAPTER_ORDER
        ]
        progress = {
            "current_chapter": _progress_store.CHAPTER_ORDER[0],
            "completed_count": len(_progress_store.CHAPTER_ORDER),
            "total_count":     len(_progress_store.CHAPTER_ORDER),
            "chapters":        chapters,
        }
    else:
        progress = _progress_store.get_progress(username)
        access = _user_chapter_access(username, info)
        for ch in progress["chapters"]:
            st = access.get(ch["key"], {})
            ch["allowed"]  = st.get("allowed", False)
            ch["unlocked"] = st.get("unlocked", False)
            if not st.get("allowed"):
                ch["completed"] = False

    return {
        "ok":       True,
        "checkin":  checkin_stats,
        "study":    study_stats,
        "progress": progress,
    }


# ── /teach/quiz/{chapter}/{quiz_index} ────────────────────────────────────────

@app.get("/teach/quiz/{chapter}/{quiz_index}")
def teach_get_quiz(chapter: str, quiz_index: int, request: Request):
    username, err = _teach_require(request)
    if err:
        return err

    access_err = _teach_chapter_allowed(username, chapter, quiz_index)
    if access_err:
        return access_err

    questions = quiz_helpers.get_quiz(chapter, quiz_index)
    if questions is None:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": f"题目不存在：{chapter} quiz {quiz_index}"
        })

    bank = quiz_helpers.load_quiz_bank()
    title = bank.get(chapter, {}).get("title", chapter)

    return {
        "ok":         True,
        "chapter":    chapter,
        "title":      title,
        "quiz_index": quiz_index,
        "total":      len(questions),
        "questions":  quiz_helpers.strip_answers(questions),
    }


# ── /teach/quiz/submit ────────────────────────────────────────────────────────

@app.post("/teach/quiz/submit")
def teach_quiz_submit(req: TeachQuizSubmitRequest, request: Request):
    username, err = _teach_require(request)
    if err:
        return err

    access_err = _teach_chapter_allowed(username, req.chapter, req.quiz_index)
    if access_err:
        return access_err

    if req.quiz_index not in (1, 2, 3):
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "quiz_index 必须是 1/2/3"
        })

    questions = quiz_helpers.get_quiz(req.chapter, req.quiz_index)
    if questions is None:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "题目不存在"
        })

    if len(req.answers) != len(questions):
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": f"答案数量不符，期望 {len(questions)} 道，收到 {len(req.answers)} 道"
        })

    score, details = quiz_helpers.score_quiz(questions, req.answers)
    result = _progress_store.save_quiz_attempt(
        username=username,
        chapter=req.chapter,
        quiz_index=req.quiz_index,
        answers=req.answers,
        score=score,
        details=details,
    )

    return {
        "ok":               True,
        "score":            score,
        "passed":           result["passed"],
        "details":          details,
        "chapter_completed": result["chapter_completed"],
        "next_unlocked":    result["next_unlocked"],
    }


# ── /teach/quiz/history ───────────────────────────────────────────────────────

@app.get("/teach/quiz/history")
def teach_quiz_history(chapter: str, request: Request):
    username, err = _teach_require(request)
    if err:
        return err
    history = _progress_store.get_quiz_history(username, chapter)
    return {"ok": True, "chapter": chapter, "history": history}


# ── Platform 跨服务管理接口 ───────────────────────────────────────────────────

@app.get("/platform/teach/chapters")
def platform_teach_chapters(request: Request):
    err, _ = _require_platform(request)
    if err:
        return err
    return _get_teach_chapters_from_html()


@app.get("/platform/teach/users")
def platform_list_teach_users(request: Request):
    err, _ = _require_platform(request)
    if err:
        return err
    from teach_user_store import _read_users, _lock
    with _lock:
        users = _read_users()
    return [
        {
            "username": u,
            "password": v.get("key", ""),
            "enabled": v.get("enabled", True),
            "expires_on": v.get("expires_on", ""),
            "allowed_chapters": v.get("allowed_chapters"),
            "note": v.get("note", ""),
        }
        for u, v in users.items()
    ]


@app.post("/platform/teach/users")
def platform_create_teach_users(req: CreateTeachUsersRequest, request: Request):
    err, _ = _require_platform(request)
    if err:
        return err
    import random
    import string

    def _gen_pw():
        return "".join(random.choices(string.ascii_letters + string.digits, k=10))

    from teach_user_store import _read_users, _write_users, _append_cred_log, _lock
    with _lock:
        users = _read_users()
        created = []
        if req.mode == "batch":
            prefix = (req.company_prefix or "user").strip()
            cnt = max(1, req.count or 1)
            existing_nums = []
            for u in users:
                if u.startswith(prefix):
                    try:
                        existing_nums.append(int(u[len(prefix):]))
                    except ValueError:
                        pass
            start = max(existing_nums, default=0) + 1
            for i in range(cnt):
                num = start + i
                uname = f"{prefix}{num:02d}"
                pw = _gen_pw()
                entry = {"key": pw, "enabled": True, "note": prefix, "expires_on": req.expires_on}
                if req.allowed_chapters is not None:
                    entry["allowed_chapters"] = req.allowed_chapters
                users[uname] = entry
                _append_cred_log(uname, pw, prefix, "add")
                created.append({"username": uname, "password": pw, "expires_on": req.expires_on})
        else:
            if not req.username:
                return JSONResponse(status_code=400, content={"error": "单个模式需要填写账号名"})
            pw = _gen_pw()
            entry = {"key": pw, "enabled": True, "note": "", "expires_on": req.expires_on}
            if req.allowed_chapters is not None:
                entry["allowed_chapters"] = req.allowed_chapters
            users[req.username] = entry
            _append_cred_log(req.username, pw, "", "add")
            created.append({"username": req.username, "password": pw, "expires_on": req.expires_on})
        _write_users(users)
    return {"created": created}


@app.delete("/platform/teach/users/{username}")
def platform_delete_teach_user(username: str, request: Request):
    err, _ = _require_platform(request)
    if err:
        return err
    ok = _teach_store.delete_user(username)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "账号不存在"})
    return {"ok": True}


@app.patch("/platform/teach/users/{username}")
def platform_update_teach_user(username: str, req: UpdateTeachUserRequest, request: Request):
    err, _ = _require_platform(request)
    if err:
        return err
    ok = _teach_store.update_user_fields(
        username, req.expires_on, req.enabled, req.allowed_chapters
    )
    if not ok:
        return JSONResponse(status_code=404, content={"error": "账号不存在"})
    return {"ok": True}


# ── 静态站点挂载（放最后，避免吞掉上面的 API） ─────────────────────────────────

app.mount("/audio", StaticFiles(directory=str(ROOT / "audio")), name="audio")
app.mount("/", StaticFiles(directory=str(TEACH_DIR), html=True), name="teach")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
