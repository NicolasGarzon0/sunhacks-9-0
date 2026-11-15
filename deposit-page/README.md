# Deposit page (chunk 4)

The page that opens from the link in the iMessage text (`/b/:token`). Vite, React, TypeScript, plain CSS. It talks only to the
backend market API (`docs/deposit-page-api.md`). **No secrets live in the frontend**: only public configuration.

## Setup

```sh
cd deposit-page
npm install
cp .env.example .env.local      # optional, edit the values below
npm run dev                     # http://127.0.0.1:5173
```

| Variable | Default | Meaning |
|---|---|---|
| `VITE_API_BASE_URL` | `http://127.0.0.1:8788` | backend base URL (`/markets` is appended if missing) |
| `VITE_WALLET_MODE` | `local` | `local` (throwaway test wallet) or `privy` (stub until chunk 5) |
| `VITE_EXPLORER_CLUSTER` | `devnet` | cluster in the explorer links |

The backend must allow this page's origin: set `DEPOSIT_PAGE_URL` (and `DEPOSIT_PAGE_ORIGIN` if it differs) to the page's URL, and for
the test network `CHAIN_MODE=real` and `AIRDROP_ENABLED=true`. In `CHAIN_MODE=mock` the wallet, airdrop and deposit endpoints answer 404 and
the page says deposits are unavailable.

## Scripts

`npm run dev` · `npm run build` (typecheck + production build into `dist/`) · `npm run preview` · `npm run typecheck` · `npm run lint` · `npm test`.

## What it does

- `/` explains the product. `/b/:token` loads the market and shows the view for its status: loading skeleton, network error with retry,
  invalid link (404), expired link (410), **open** (deposit), **locked**, **settled**, **refunded**, **failed**.
- Polls the market every 5 s while open or locked and every 15 s afterwards, pauses while the tab is hidden, backs off (x2, capped at 60 s)
  on errors and shows a "reconnecting" banner after 3 failures in a row. The countdown to funding close uses the browser clock; the page stops
  offering deposits 30 s before the close, like the server.
- Deposit: `deposit_tx` -> the wallet signs `message_b64` -> `deposit_submit`. `TX_EXPIRED` rebuilds and signs again once, then shows an error.
  Every API error code is mapped to one friendly message in `src/lib/errors.ts` (a test checks it against the table in the API doc).
- All API text (team names, display names, fee lines) is rendered as React text nodes. There is no raw HTML anywhere (a lint rule forbids
  `dangerouslySetInnerHTML`). Team badges are generated colored initials; no logos.

## The wallet layer

The UI only knows the interface in `src/wallet/types.ts`:

```ts
interface WalletProvider {
  name; address /* null until ready */; ready; isTest;
  connect(); signDeposit(unsignedTxB64, messageB64) /* { signed_tx_b64 } | { message_b64, user_signature_b64 } */;
  disconnect(); subscribe(listener);
}
```

Components get it through `useWallet()` (`src/wallet/WalletContext.tsx`). Nothing outside `src/wallet/` imports a concrete provider or a crypto
library (enforced by an ESLint `no-restricted-imports` rule); `src/wallet/index.ts` (`createWalletProvider(mode)`) is the one place that
picks the implementation.

- `LocalDevWalletProvider`: generates an ed25519 key in the browser (`@noble/ed25519`), stores the 32 byte seed in `localStorage` (try/catch, with an
  in-memory fallback), signs the message locally and shows the "test wallet" notice with a two step **Reset wallet**. Devnet test money only.
  Anyone with access to the browser profile can read the key: never put value in it.
- `PrivyWalletProvider`: a documented stub that throws `not implemented in chunk 4`. `VITE_WALLET_MODE=privy` selects it.

### Chunk 5: adding Privy

Implement `PrivyWalletProvider` with the same interface (login, embedded Solana wallet public key as `address`, `signDeposit` signing the
`messageB64` bytes and returning `{ message_b64, user_signature_b64 }`), add the Privy app id as a `VITE_` variable, and set
`VITE_WALLET_MODE=privy`. No component changes: the UI already works from `useWallet()`. Never alter the message before signing: the server
rebuilds the exact transaction and rejects anything that differs.

## Tests

`npm test` runs Vitest: unit tests for the pure logic (amount parsing, countdown, error copy against the API doc, status to view, polling
backoff, base58, the local wallet) and React Testing Library tests with a mocked API for each status view, deposit success, `TX_EXPIRED`, every
blocking error, the disabled other side, the full market message, the airdrop flow, hostile display names and the Privy stub.

## Live check (devnet)

`e2e/live.mjs` drives the page in the system Chrome with `playwright-core` (no browser download) against the real stack, and
`e2e/live_helper.py` opens the market through the DM flow and sets the simulated result. Screenshots go to `screenshots/` (gitignored).
It needs the chain service, the backend (`CHAIN_MODE=real`, `AIRDROP_ENABLED=true`, `GAME_PROVIDER=simulated`, short timings:
`FUNDING_MINUTES_BEFORE_KICKOFF=3`, `LINK_GRACE_MINUTES=1`, `WORKER_TICK_SECONDS=10`, `DEPOSIT_PAGE_URL=http://127.0.0.1:5173`), the gateway
and `npm run dev` running, and it sends one real text to the live-test roster entry. Run `node e2e/live.mjs`.

## Deploy prep

`npm run build` writes a static site to `dist/`. `vercel.json` rewrites every path to `index.html`, so `/b/:token` works on refresh. Set the three
`VITE_` variables in the host, and the backend's `DEPOSIT_PAGE_URL` / `DEPOSIT_PAGE_ORIGIN` to the deployed origin (the backend allows one origin).
Nothing is deployed yet.
