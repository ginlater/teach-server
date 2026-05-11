# teach-server

`teach.aibeautyfulwomen.com` 独立 FastAPI 服务，从 `followup-agent` 拆分出来后部署在同一台机器的 8001 端口。

## 目录

| 路径 | 说明 |
|------|------|
| `app.py` | FastAPI 入口（路由 + 静态挂载） |
| `auth.py` | JWT 签发 / 验证（与主站共享 `JWT_SECRET`） |
| `teach_user_store.py` | teach 账号体系（直接复用） |
| `teach_progress_store.py` | 学习进度（SQLite） |
| `quiz_helpers.py` | 题库加载、去答案、打分 |
| `teach/` | 10 个前端 HTML |
| `scripts/build_quiz_bank.py` | 题库生成脚本 |
| `data/` | 运行时数据（账号、进度、题库） |

## 启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，把 JWT_SECRET 改成与 followup-agent 一致的值

./start.sh
# 或: .venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8001
```

## 路由概览

学员侧（teach token）：
- `POST /teach/login`
- `GET  /teach/auth/verify`
- `POST /teach/checkin`
- `POST /teach/heartbeat`
- `GET  /teach/me/stats`
- `GET  /teach/quiz/{chapter}/{quiz_index}`
- `POST /teach/quiz/submit`
- `GET  /teach/quiz/history?chapter=...`
- `GET  /teach/logs`、`/teach/users`、`/teach/credentials`（管理用）

Platform 后台跨服务（platform token，由主站 nginx 反代 `/platform/teach/*` 进来）：
- `GET  /platform/teach/chapters`
- `GET|POST /platform/teach/users`
- `DELETE|PATCH /platform/teach/users/{username}`

## 部署须知

- 8001 端口仅 127.0.0.1 暴露，由 nginx 反代
- `JWT_SECRET` 必须与 followup-agent `.env` 一致
- 迁移 `data/teach_progress.db` 时务必先 stop followup-agent，避免写锁冲突
