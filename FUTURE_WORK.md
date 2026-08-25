# Future Work — Backend & Commercialization Notes

Reference document. Captures architecture decisions and implementation plans
discussed but not yet built. Update as decisions firm up.

---

## 1. Per-user quota (the freemium hook)

### Why local enforcement does not work
Any quota stored in `%APPDATA%` (file, registry, hidden marker) is bypassable
in under a minute:
- Delete the file → counter resets
- Reinstall the app → counter resets
- Sign in with a second Gmail → fresh quota
- Snapshot AppData before use, restore after → infinite resets

This is why no real freemium product (ChatGPT, Cursor, Notion AI) attempts
client-side quota. Enforcement must live on a server.

### The standard pattern (3 pieces)
1. **Backend server** with a database
2. **Verify Google ID token server-side** — key the user row on Google's
   `sub` field (permanent), NOT email (technically mutable)
3. **Client calls server, server calls LLM** — server owns the LLM API keys
   and increments the user's quota counter on each call

### Minimum users table
```
users
─────
google_sub       TEXT PRIMARY KEY    ← from Google ID token, permanent
email            TEXT                ← display only
query_count      INT  DEFAULT 0
quota            INT  DEFAULT 10
paid_credits     INT  DEFAULT 0
plan             TEXT DEFAULT 'free'
created_at       TIMESTAMP
```

### Quota check pseudo-flow
```
on POST /v1/chat:
    user = verify_jwt(request.headers.Authorization)
    if user.query_count >= user.quota + user.paid_credits:
        return 402 { "buy_credits_url": "..." }
    response = call_upstream_llm(...)
    INSERT INTO quota_events (user_id, delta=+1, reason='chat', ...)
    return response
```

Use a **ledger pattern** (append to `quota_events`), not direct
`UPDATE users SET query_count = query_count + 1`. Race-condition-safe and
gives perfect audit trail for billing disputes.

---

## 2. Full backend architecture

```
┌──────────────────────────────────────────────────────────┐
│  Tkinter desktop client (existing app)                   │
└────────────┬─────────────────────────────────────────────┘
             │  HTTPS, Bearer <session JWT>
             ▼
┌──────────────────────────────────────────────────────────┐
│  Cloudflare  (TLS, DDoS, edge rate-limit, geo-block)     │
└────────────┬─────────────────────────────────────────────┘
             ▼
┌──────────────────────────────────────────────────────────┐
│  FastAPI app server  (stateless, horizontal scale)       │
│  /v1/auth/login   /v1/chat   /v1/transcribe              │
│  /v1/me           /v1/checkout   /v1/webhooks/...        │
└──┬──────────┬──────────┬──────────┬──────────┬───────────┘
   ▼          ▼          ▼          ▼          ▼
┌──────┐ ┌────────┐ ┌──────────┐ ┌────────┐ ┌──────────┐
│ PG   │ │ Redis  │ │ LLM APIs │ │ S3/R2  │ │ Razorpay │
│(data)│ │(cache) │ │Groq/GPT/ │ │(audio, │ │ /Stripe  │
│      │ │        │ │  Claude  │ │ logs)  │ │(payments)│
└──────┘ └────────┘ └──────────┘ └────────┘ └──────────┘
              │
              ▼
       ┌─────────────────────────────────┐
       │ Observability: Sentry + Grafana │
       └─────────────────────────────────┘
```

---

## 3. API surface (FastAPI endpoints)

| Endpoint | Purpose |
|---|---|
| `POST /v1/auth/login` | Exchange Google ID token → own session JWT (~24h) |
| `POST /v1/chat` | Main chat. Verifies user, checks quota, calls LLM, streams back |
| `POST /v1/chat/vision` | Same but with image attachments |
| `POST /v1/transcribe` | Audio bytes → Whisper transcript |
| `GET  /v1/me` | `{email, plan, queries_used, quota, credits}` |
| `POST /v1/checkout` | Create Razorpay/Stripe order → return payment URL |
| `POST /v1/webhooks/razorpay` | Razorpay calls on payment success → bumps credits |
| `GET  /v1/healthz` | Liveness probe |

---

## 4. Auth flow (replacing direct Google flow)

```
1. App starts → reads google_token.dat → has Google ID token
2. App → POST /v1/auth/login { id_token }
3. Server → verifies JWT signature against Google JWKS, extracts `sub`
4. Server → upserts users row → issues OWN session JWT (HS256, 24h)
5. App → stores session JWT in memory (NOT on disk)
6. App → every request: Authorization: Bearer <session_jwt>
7. Server → verifies JWT locally (microseconds, no Google round-trip)
8. On 401: app re-logs in via step 2 with refreshed Google token
```

Why a separate session JWT instead of forwarding Google's:
- Google ID tokens expire in 1h; refresh requires JWKS lookup
- Own JWT verifies in microseconds with shared HS256 secret
- Decouples from Google in case scopes change later

---

## 5. Database tables (Postgres)

```
users            ─ google_sub (PK), email, name, plan,
                   quota, queries_used, paid_credits, created_at
sessions         ─ session_id, user_id, started_at, last_msg_at
messages         ─ session_id, role, content_hash, tokens, created_at
                   (hash, not raw text, for privacy + storage)
quota_events     ─ user_id, delta, reason, idempotency_key, at
                   (append-only ledger; reconciles to users.queries_used)
payments         ─ razorpay_order_id, user_id, amount, status, raw_webhook
audit_log        ─ user_id, action, ip, ua, at
```

Two principles to follow from day one:
- **Ledger pattern** for quota (append-only, never direct UPDATE)
- **Idempotency keys** on every mutation — client sends UUID in
  `Idempotency-Key` header; server caches result. Retries do not double-charge

---

## 6. Cache layer (Redis / Upstash)

- Rate-limit counters (sliding window per `user_id`)
- Idempotency-key results (5 min TTL)
- Last-N session messages (skip DB hit for memory window)
- Google JWKS public keys (~6h TTL)
- Webhook dedup (Razorpay can deliver same webhook 5× — SETNX makes idempotent)

---

## 7. LLM proxy layer

Where the server adds value over direct calls:

- **Server-side keys** — Groq/OpenAI/Anthropic keys in env vars, never on client
- **Routing** — `{"model": "groq-llama-scout"}` → Groq client, etc.
- **Streaming (SSE)** — tokens stream to client as generated
- **Cost tracking** — log prompt+completion tokens, multiply by upstream price
- **Circuit breakers** — provider 5xx 3× in 30s → route to fallback model
- **Response cache** — identical prompt in last 5 min → cached response
- **Per-user system-prompt injection** — append user name/stack on server

---

## 8. Payments (Razorpay — India-first)

- Razorpay Checkout: UPI, cards, netbanking, wallets (UPI critical for INR)
- Webhook signature verification with shared secret (`X-Razorpay-Signature`)
- Idempotent handler keyed on `razorpay_payment_id`
- Hourly reconciliation cron — fetch yesterday's payments via API,
  verify DB has all of them (Razorpay webhooks occasionally drop)
- For global expansion: add Stripe later as a second provider

---

## 9. Observability (non-negotiable)

| Tool | Purpose | Free tier? |
|---|---|---|
| Sentry | Exception tracking with stack traces | ~5k events/month |
| Grafana Cloud Loki | Structured logs | 50 GB/month |
| Grafana Cloud Prometheus | Metrics | 10k series |
| Grafana Cloud Tempo | Distributed traces | 50 GB/month |

Every log line tagged with `request_id`, `user_id`, `endpoint`.
One dashboard showing: req/s, p95 latency, LLM cost/hour, free-vs-paid ratio,
4xx/5xx rate, payment success rate.

---

## 10. Background jobs

- **Quota reset cron** (daily/monthly) — `UPDATE users SET queries_used = 0`
- **Payment reconciliation** (hourly)
- **Session cleanup** (delete chat memory > 30 days)
- **Email digests** (optional re-engagement)

Cloud Scheduler → HTTP endpoint with shared secret → handler in same FastAPI
app. Avoid a separate worker service until traffic justifies it.

---

## 11. Recommended stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Same as client, share Pydantic models |
| Web framework | FastAPI | Async, auto OpenAPI docs, fast |
| ASGI server | Uvicorn + Gunicorn | Standard combo |
| ORM | SQLAlchemy 2.0 + Alembic | Battle-tested migrations |
| Database | Supabase or Neon Postgres | Free tier, scales, branching |
| Cache | Upstash Redis | Serverless, pay-per-request, free tier |
| Hosting | Google Cloud Run | Scales to zero, ~free at low traffic |
| DNS/CDN | Cloudflare | Free TLS + DDoS + analytics |
| Payments | Razorpay (IN) + Stripe (global later) | UPI native for India |
| Errors | Sentry | Free tier covers small apps |
| Metrics/logs | Grafana Cloud free tier | Loki + Prometheus + Tempo |
| Secrets | GCP Secret Manager | Native to Cloud Run |
| CI/CD | GitHub Actions | 2000 min/month free for private repos |
| IaC (later) | Terraform or Pulumi | Reproducible infra from code |

**Cost at <1000 active users: ₹0–₹800/month** (mostly free tier, LLM upstream
is the only real cost).

**Cost at 10k active users: ₹4000–₹12000/month** depending on LLM usage.

---

## 12. Server folder structure

```
ia-server/
├── app/
│   ├── main.py                ← FastAPI app factory + middleware
│   ├── settings.py            ← pydantic-settings (loads env)
│   ├── deps.py                ← shared FastAPI dependencies
│   │                            (current_user, db_session, redis)
│   ├── auth/
│   │   ├── google.py          ← verify Google ID token
│   │   └── session.py         ← issue/verify own session JWT
│   ├── routers/
│   │   ├── auth.py
│   │   ├── chat.py
│   │   ├── transcribe.py
│   │   ├── billing.py
│   │   └── webhooks.py
│   ├── llm/
│   │   ├── base.py            ← abstract Provider
│   │   ├── groq.py
│   │   ├── openai.py
│   │   └── anthropic.py
│   ├── services/
│   │   ├── quota.py
│   │   ├── payments.py
│   │   └── audit.py
│   ├── models/                ← SQLAlchemy ORM
│   └── schemas/               ← Pydantic DTOs
├── migrations/                ← Alembic
├── tests/
├── Dockerfile
├── docker-compose.yml         ← local dev (postgres + redis)
├── pyproject.toml
├── alembic.ini
└── .github/workflows/deploy.yml
```

Keep server in `/server` subfolder or a separate repo so deploys are
independent of the client.

---

## 13. Client-side changes when backend lands

Today, [llm_service.py](llm_service.py) calls upstream LLMs directly:

```python
# direct
self.client = Groq(api_key=key)
self.client.chat.completions.create(...)
```

After backend exists:

```python
# via server
self.api = APIClient(base_url="https://api.yourapp.com",
                     session_jwt=jwt)
self.api.chat(message, session_id)
```

New `api_client.py` will handle:
- Login (exchange Google token for session JWT)
- Session JWT storage in memory only (never on disk)
- Auto re-login on 401
- Streaming SSE response decoding
- Surfacing 402 quota-exceeded → triggers "Buy credits" dialog

Settings panel changes:
- Remove BYO API key fields (Groq/OpenAI/Claude entries)
- Show `Plan: Free • 7/10 queries used`
- Add `Buy credits` button → opens Razorpay checkout URL

---

## 14. Decisions to make before scaffolding the server

1. **Fully proxy LLM, or BYOK (bring your own keys)?**
   - Proxy = standard freemium, margin per query, simpler UX
   - BYOK = faster v1, no LLM bill for you, but no charging mechanism
2. **Lifetime / daily / monthly quota?**
   - Affects `quota_events` reset logic and cost projections
3. **Single region or multi-region?**
   - Start `asia-south1` (Mumbai) for India audience
   - Multi-region only when paying users elsewhere appear
4. **Phone-number verification for free tier?**
   - Prevents trivial multi-account abuse
   - ~₹4/SMS via Twilio Verify
   - Most aggressive freemium products (ChatGPT, Cursor) require this

---

## 15. CI/CD pipeline (GitHub Actions on push to main)

```
1. Lint (ruff)
2. Type-check (mypy)
3. pytest
4. Build Docker image
5. Push to Artifact Registry
6. Deploy to Cloud Run STAGING (auto)
7. Smoke test (curl /v1/healthz)
8. Manual approval gate
9. Run Alembic migrations
10. Deploy to Cloud Run PROD
```

Database migrations always run **before** traffic flip — never deploy code
that expects schema X if migrations have not committed schema X yet.

---

## 16. Secrets management

- `.env` in `.gitignore` from commit 1
- Pre-commit hook (e.g., `gitleaks`) to block accidental key commits
- Cloud Run env vars for low-sensitivity config
- GCP Secret Manager for high-sensitivity (LLM keys, JWT secret, DB password)
- Rotate provider keys quarterly
- Separate `staging` and `prod` secrets — never share

---

## 17. Things to add later (nice-to-have, post-MVP)

- **Streaming responses (SSE)** — typing-effect UI
- **Multi-device sync** — same Google account on Windows + future Mac/web
- **Usage analytics for the user** — "you used 47 queries this month, mostly
  vision"
- **Team plans** — pool quota across multiple Google accounts (B2B angle)
- **Referral codes** — earn 10 free queries per referral
- **Web client** — Tkinter app is Windows-only; a Next.js web app could share
  the same backend
- **Native macOS / Linux clients** — bigger TAM
- **Voice in / voice out** — already have audio transcription; add TTS reply
- **Mobile companion app** — for notifications, quota check, payments
- **Admin dashboard** — internal-only view of users, revenue, costs, errors

---

## 18. Reference: existing local code touch-points

| Concern | File | Notes |
|---|---|---|
| API keys storage | [llm_service.py:37](llm_service.py#L37) | `_KEYS_FILE` in AppData |
| Google OAuth | [auth.py:35](auth.py#L35) | `TOKEN_FILE` in AppData |
| DPAPI encryption | [secure_store.py](secure_store.py) | All `.dat` files use this |
| Logout helper | [auth.py](auth.py) `logout()` | Deletes token file |
| Wipe API keys | [llm_service.py](llm_service.py) `wipe_api_keys()` | Deletes keys file |
| Direct LLM calls | [llm_service.py](llm_service.py) Provider classes | Will be replaced by API client |
| Model list | [config.py:3](config.py#L3) | `AVAILABLE_MODELS` dict |
| Chat memory window | [config.py:40](config.py#L40) | `CHAT_MEMORY_LIMIT = 15` |

When migrating to backend: most of the Provider classes in `llm_service.py`
get deleted from the client and re-implemented on the server side.
