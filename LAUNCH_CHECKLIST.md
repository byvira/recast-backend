## Recast — Remaining Launch Checklist

### 🔴 Blockers (deploy works without these, but these specific things break)
- [ ] EMAIL_FROM — verify a real domain in Resend, set EMAIL_FROM=noreply@yourdomain.com (currently gmail.com — production OTP email will fail without this)
- [ ] PRODUCTION_DOMAIN — set to the real frontend origin once one exists (currently the backend's own URL — CORS will block the frontend otherwise). **If this is still unset, it also explains a login loop**: ENVIRONMENT=production with no PRODUCTION_DOMAIN/FRONTEND_URL set silently degrades auth cookies to SameSite=Lax across what's actually a cross-site deployment — login appears to work, then every next request has no cookie. main.py now logs this loudly on startup if it's misconfigured — check the Render deploy log.
- [ ] Background worker service — confirmed NOT deployed (queried the live DB directly, 2026-09-22: `agent_worker_state` has zero documents anywhere). Remy and Odette are both entirely non-functional without it — no persona ever builds, no signals/insights/flags ever fire on a schedule. See DEPLOY.md's new "Background worker" section for the exact Render service to create (`arq app.workers.agent_worker.WorkerSettings`).

### Render dashboard
- [ ] Enter every env var from .env into Render → Environment (full reference in DEPLOY.md)
- [ ] Use the separate SECRET_KEY generated for Render — do NOT reuse the local .env value
- [ ] Set ENVIRONMENT=production
- [ ] Set start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"`
- [ ] Confirm health check path = /health (the shallow one, not /health/ready)
- [ ] Confirm plan / instance count

### External services
- [ ] MongoDB Atlas → Network Access → allow Render's egress IPs (or 0.0.0.0/0)
- [ ] Register 4 OAuth redirect URIs (Meta, Google, LinkedIn, Threads) in each provider console, byte-identical to Render's values
- [ ] Create a Slack incoming webhook, set SLACK_WEBHOOK_URL (optional — currently no-ops safely)

### Credential rotation (exposed to tooling this session)
- [ ] Rotate: Groq API key, MongoDB Atlas password, Upstash token, Meta/LinkedIn/Google/Threads app secrets
- [ ] FERNET_SECRET_KEY — run `scripts/rotate_fernet_key.py` (dry-run, then real) BEFORE swapping the key anywhere. Swapping it directly breaks every connected OAuth account.
- [ ] Save DOCS_PASSWORD somewhere safe (password manager) — not printed again after it was generated

### Post-deploy verification
- [ ] Re-run smoke tests (scripts/smoke_stage0–4) against the live URL
- [ ] Full user journey: signup → OTP → workspace → connect platform → generate → schedule → publish → analytics
- [ ] Break a dependency (bad REDIS_URL on a staging deploy) — confirm /health/ready goes red and Slack/email alert fires
- [ ] Check Render logs are one JSON object/line with request_id; grep for accidental secret leakage
- [ ] Confirm a real error reaches Sentry (environment + release tagged)
- [ ] Confirm a real agent run shows up in the LangSmith project
- [ ] Tag the release, snapshot the Atlas cluster
- [ ] Confirm rollback steps (Render "rollback to previous deploy" + when a DB restore is also needed)

### Lower priority
- [ ] Reconcile local Python 3.12.8 vs runtime.txt's pinned 3.11.9 (not currently causing issues)
- [ ] Consider upgrading the Groq plan — 8000 TPM cap got hit during smoke testing under light load
- [ ] Optional: prometheus-fastapi-instrumentator at /metrics if Sentry+LangSmith aren't enough
