# SaaS Backend

Minimal FastAPI boilerplate — auth, MongoDB, Redis, content pipelines. No product logic included.

## Stack

| Layer | Package |
|---|---|
| API | FastAPI 0.115, Uvicorn |
| Auth | python-jose (JWT), passlib (bcrypt) |
| Database | Motor 3 (async MongoDB) |
| Cache | redis.asyncio |
| Config | pydantic-settings |
| AI | anthropic, langchain-anthropic, langgraph |
| Payments | stripe |

## Prerequisites

- Python 3.12.8
- MongoDB running locally or a connection string
- Redis running locally or a connection string

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

> **Note:** Copy `.env.example` to `.env` and fill in all values before running.

## Environment

```powershell
copy .env.example .env
```

| Variable | Description |
|---|---|
| `SECRET_KEY` | Run `python -c "import secrets; print(secrets.token_hex(32))"` |
| `MONGODB_URL` | Must include database name, e.g. `mongodb://localhost:27017/saas_dev` |
| `REDIS_URL` | e.g. `redis://localhost:6379/0` |

## Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | None | Service health check |
| GET | `/api/v1/text` | None | Text pipeline status |
| POST | `/api/v1/text` | Bearer JWT | Queue text generation |
| GET | `/api/v1/audio` | None | Audio pipeline status |
| POST | `/api/v1/audio` | Bearer JWT | Queue audio generation |
| GET | `/api/v1/video` | None | Video pipeline status |
| POST | `/api/v1/video` | Bearer JWT | Queue video processing |
| GET | `/api/v1/image` | None | Image pipeline status |
| POST | `/api/v1/image` | Bearer JWT | Queue image generation |
| GET | `/api/v1/brand` | None | Brand pipeline status |
| POST | `/api/v1/brand` | Bearer JWT | Queue brand processing |
| GET | `/api/v1/publish` | None | Publish pipeline status |
| POST | `/api/v1/publish` | Bearer JWT | Queue publish job |

## Interactive docs

- Swagger UI — http://localhost:8000/docs
- ReDoc — http://localhost:8000/redoc
