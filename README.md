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

## Support tickets

Members file tickets from the Support page and follow them there. Recast staff
work them in the Ops Dashboard under Support (`/ops/support`). Staff need
`is_platform_staff` (or `is_master_admin`) on their user document; an optional
`support_role` of `agent` (default), `lead` or `admin` sets what they may do.

Code: member routes `app/api/v1/support.py`, the assistant and live status
`support_assistant.py`, staff routes `ops_support.py`, `ops_support_tools.py`
and `ops_support_incidents.py`. The shared rules live in `app/shared/support*.py`
and the background housekeeping in `app/workers/support_lifecycle.py`.

### Statuses and allowed moves

Every status change, from any route, goes through one table
(`ALLOWED_TRANSITIONS` in `app/shared/support.py`) and writes an audit event.

| From | May move to |
|---|---|
| `open` | `investigating`, `resolved`, `closed` |
| `investigating` ("In progress") | `waiting_on_member`, `waiting_on_engineering`, `resolved`, `closed` |
| `waiting_on_member` | `investigating`, `resolved`, `closed` |
| `waiting_on_engineering` | `investigating`, `resolved`, `closed` |
| `resolved` | `investigating`, `closed` |
| `closed` | nothing (final) |

Staff can only close a `resolved` ticket. A member can close (withdraw) any open
ticket or confirm a resolved one. A member reply on a `resolved` or
`waiting_on_member` ticket moves it back to `investigating`. A reply on a
`closed` ticket is refused. There is no hard delete.

### Timing, limits and targets

All in `app/shared/support_rules.py`, one file, plain constants.

| Rule | Value |
|---|---|
| Reminder while `waiting_on_member` | after 3 days of silence |
| Auto-resolve `waiting_on_member` | after 7 days of silence |
| Warning before auto-close | 2 days before |
| Auto-close `resolved` | after 7 days |
| Tickets per member / per workspace | 5 per hour / 30 per day |
| Messages per member | 20 per hour |
| Uploads per member | 20 per hour |
| Duplicate guard | same area open within 10 minutes |
| Message length | 5,000 characters |
| Attachments | 5 per message, 10 MB each; png, jpg, webp, pdf, txt, log |
| First reply target | P1 1h, P2 4h, P3 24h |
| Resolution target | P1 24h, P2 72h, P3 7 days |

An admin can change the SLA targets and set who owns each area from the Ops
"Response targets" page (stored in `support_settings`). Paid plans get one
priority level faster once tier names are added to `PAID_TIERS`; there are no
paid plans yet so it is inactive.

### Roles

| Can | agent | lead | admin |
|---|---|---|---|
| View, claim, reply, note, change status, escalate | yes | yes | yes |
| Reassign others, bulk actions, merge, incidents, view-as-member, mute a member | no | yes | yes |
| Manage shared canned replies | own only | all | all |
| Change SLA targets and area owners, remove a message | no | no | yes |

### Things that must exist outside the code

- Resend templates with these aliases, or production emails fail safely (the
  in-app notification always works): `support-ticket-update` (NAME,
  TICKET_NUMBER, SUBJECT, HEADLINE, MESSAGE, LINK) and `support-ops-digest`
  (COUNT, LIST, LINK).
- Private attachments use Cloudinary's `authenticated` delivery type. Run one
  real upload and open the signed link once after deploying.

### AI in support

Drafting a reply (staff) and the member assistant call the model; the
diagnosis card and category rules do not. Ticket text is fenced as untrusted,
emails, phone numbers and secret-looking strings are masked first, calls are
capped per staff member per hour and per day, tokens are counted under a
platform bucket rather than a customer's AI budget, and each call is logged
with its prompt version. The AI category fallback is off by default
(`AI_CATEGORY_FALLBACK_ENABLED`).
