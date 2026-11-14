# iMessage gateway: interface

A long-running process that (1) forwards inbound iMessage DMs to the backend over HTTP and (2) exposes `POST /send` so the backend can text registered users. It contains no bet logic. Plan: Photon free shared pool, DMs only.

Status legend: **VERIFIED** (from SDK source/docs/tests), **UNVERIFIED** (needs the live test), see the last section.

## Which number to text
With the current implementation (free shared pool), the number that receives and answers messages is **+1 (628) 358-1277** (observed from the tester's phone, 2026-11-14).
- This is **not** the `kind: "agent"` handle in the Photon project (`+1 628 997 9661`), which is only the project's identity.
- The shared pool routes each registered user through a pool number, so another user's phone may see a different line, and it can change later. Do not hardcode it in a UI. Tell each user to reply to the thread they already have, or confirm the number from their own phone.
- A fixed public number needs a dedicated (paid) line and is out of scope for this plan.

## Shared secret
Every request in both directions carries `X-Gateway-Secret: <GATEWAY_SHARED_SECRET>`. Same value on both sides. Comparison is timing-safe. `GET /health` needs no secret.

## Inbound: gateway -> backend
`POST {BACKEND_URL}/inbound`, `Content-Type: application/json`, header `X-Gateway-Secret`.
```json
{ "messageId": "string", "handle": "string", "text": "string", "receivedAt": "2026-01-01T00:00:00.000Z" }
```
- `handle` is `message.sender.id` exactly as Spectrum gives it (never reformatted). Use it as the `to` for `/send`.
- `messageId` is `message.id`. Dedup key (gateway also dedups for 1 hour).
- Respond 2xx quickly (5 s timeout per attempt). Do slow work asynchronously and reply via `/send`.
- Retries: network error, timeout, 5xx, 408, 429 are retried with exponential backoff (0.5 s doubling, max 8 s) for about 30 s, then the message is logged and **dropped**. Other 4xx are not retried (dropped).
- Delivery is at-least-once: make `/inbound` idempotent on `messageId`.
- Only text DMs are forwarded. Dropped: group chats, own/outbound messages, messages without a sender, attachments, reactions, read events, membership/rename events.

## Outbound: backend -> gateway
`POST {GATEWAY_URL}/send`, header `X-Gateway-Secret`.
```json
{ "to": "handle", "text": "string (max 5000)", "idempotencyKey": "string (max 200)" }
```
Response (always JSON):
```json
{ "ok": true, "messageId": "string | null" }
{ "ok": false, "code": "NOT_ALLOWED", "detail": "string", "retryAfter": 12 }
```
`messageId` is `null` when Spectrum's send resolved without a message object.

| code | HTTP | Meaning |
|---|---|---|
| UNAUTHORIZED | 401 | Missing/wrong `X-Gateway-Secret` |
| BAD_REQUEST | 400 | Invalid JSON/shape (`to` empty or has whitespace, `text` empty, missing key, body > 16 KB), or unknown route (404) |
| NOT_ALLOWED | 403 | Photon refused: "Target not allowed for this project" (recipient not a registered project user on the shared plan) |
| RATE_LIMITED | 429 | Gateway limit or Photon quota/throttle. `retryAfter` (seconds) when known; also a `Retry-After` header |
| SEND_FAILED | 502 | Any other failure (connection, auth to Photon, etc.). `detail` holds the raw message (phones masked only in logs, not in this field) |

**Idempotency:** the same `idempotencyKey` within 10 minutes returns the original result and does not send again (concurrent duplicates share one send). Failures are cached too; `RATE_LIMITED` is never cached, so retry with the same key later. The key alone decides: a different `to`/`text` under a reused key returns the old result. Stored in memory, reset on restart.

**Gateway limits (defaults, in `src/outbound.ts`):** 10 sends/min per recipient, 60/min global, 4000 per rolling 24 h, and at most 45 distinct new recipients per rolling 24 h (Photon: 50 new conversations/line/day, 5000 msgs/day). A handle that texted us first is treated as an existing conversation (replies do not count). Counters are in memory and reset on restart, so a restart can briefly under-count against Photon's real quota.

## GET /health
```json
{ "status": "ok", "spectrumConnected": true, "lastInboundAt": null, "uptimeSeconds": 12,
  "newRecipientsLast24h": 0, "sendsLast24h": 0, "idempotencyEntries": 0 }
```
The SDK exposes no connection state. `spectrumConnected` is best effort: true after `Spectrum()` resolved and the inbound loop has not ended or thrown. It is not a live link check. Use `lastInboundAt` as a liveness hint.

## Caveats
- **A resolved send is not proof of delivery.** `ok: true` means Spectrum accepted the send. Unsupported content is warned and skipped by the SDK without throwing.
- **Allowlist:** on the free shared plan the agent can only text registered Photon project users (max 25, set in the Photon dashboard). Anyone else gets `NOT_ALLOWED`. Group chats are impossible on this plan.
- **Dedup and restart:** inbound dedup (1 h TTL), outbound idempotency and rate counters are all in memory. A restart resets them, so a redelivered message after restart can be forwarded twice.
- **Possible lost inbound:** the SDK reconnects forever with cursor catch-up; if the server rejects the cursor it resumes live and messages in the gap are lost (the SDK logs it; the gateway cannot detect it).
- Messages for one sender are forwarded in order; different senders are concurrent.

## Environment variables (names only; see `.env.example`)
`PROJECT_ID`, `PROJECT_SECRET` (Photon), `BACKEND_URL`, `GATEWAY_SHARED_SECRET`, `PORT` (default 8787), `LOG_LEVEL` (default `info`; `debug` also logs message text). Scripts: `GATEWAY_URL` (default `http://localhost:$PORT`), `MOCK_BACKEND_PORT` (default 8788).

## Running
`npm start` (loads `.env` if present), `npm run dev`, `npm run typecheck`, `npm test`.
- `npm run mock-backend`: receives `/inbound`, replies `echo: <text>` through `/send`. Set `BACKEND_URL=http://localhost:8788`.
- `npm run send-dm -- <handle> "text" [--yes]`: prints a plan, asks `[y/N]`, then calls the running gateway's `/send` (opens no Spectrum connection of its own).

Logs are one JSON line per event (`inbound.received|forwarded|dropped|retry`, `send.requested|ok|failed|idempotent_replay`), handles masked to the last 4 digits, text only at debug level.

## Verification status
| Item | Status |
|---|---|
| Inbound `imessage(space).type` is `"dm"` or `"group"` | VERIFIED live (a real DM passed the DM guard) |
| `space.send` returns `Promise<Message \| undefined>`, `Message.id: string` | VERIFIED in types and live (real `messageId` returned) |
| SDK error classes importable | `@photon-ai/advanced-imessage` is not a declared dependency; gateway classifies by duck typing (name/code/grpcCode/message) instead |
| Quota ErrorCode strings exist (dailyLimitExceeded, newContactThrottled, ...) | VERIFIED in SDK types; mapping to RATE_LIMITED unit-tested, not live |
| `"Target not allowed for this project"` detected by message substring | Unit-tested; real error class/code/grpcCode **UNVERIFIED** until live test (gateway logs them on first occurrence) |
| `clientGuid`/idempotency on `space.send` | Not exposed by Spectrum's Space API; gateway uses its own map |
| `retryAfter` units from Photon | **UNVERIFIED** (assumed seconds, passed through as given) |
| Handle format in `message.sender.id` and Photon users | VERIFIED live: E.164 (`+1` + 10 digits), matches the registered user's handle |
| Live DM round trip (registered phone texts line, mock backend echoes via `/send`) | VERIFIED live: received, forwarded (1 attempt), `send.ok` with real messageId in ~750 ms. Phone receipt of the echo is reported by the tester, not observable from the gateway |
| (a) Reply inside an inbound DM works for an unregistered sender | **NOT TESTED, out of scope.** Decision: gateway supports registered users only |
| (b) Exact error for a proactive send to an unregistered handle | **NOT TESTED, out of scope.** Expected `NOT_ALLOWED` via message substring; real class/code/grpcCode unknown (logged on first occurrence) |
