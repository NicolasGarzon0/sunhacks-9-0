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
| `VITE_WALLET_MODE` | `local` | `local` (throwaway test wallet) or `privy` (email login, embedded Solana wallet) |
| `VITE_PRIVY_APP_ID` | empty | Privy app id (public). Required when `VITE_WALLET_MODE=privy` |
| `VITE_PRIVY_CLIENT_ID` | empty | Privy client id (public). Optional: passed to Privy only if set |
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
- `PrivyWalletProvider` (`VITE_WALLET_MODE=privy`): see below.

### Privy wallet (real users)

Switch modes with one variable. In `deposit-page/.env.local`:

```sh
VITE_WALLET_MODE=privy
VITE_PRIVY_APP_ID=<your Privy app id>      # public id, read from env, never hardcoded
# VITE_PRIVY_CLIENT_ID=<client id>         # optional, only if your Privy app has one
```

Leave `VITE_WALLET_MODE` unset (or `local`) for the throwaway test wallet: that build does not load the Privy SDK at all. There is **no Privy
app secret** and the page must never use one. In the Privy dashboard the allowed origin must include the page's origin (`http://localhost:5173`
for dev) and the login methods email and SMS (SMS works in the US and Canada only; the page opens the email form by default).

- Only `src/wallet/` imports Privy (`PrivyRoot.tsx` has the hooks, lazy loaded; `PrivyWalletProvider.ts` has no SDK import and is driven through
  a small bridge). `WalletRoot` wraps the app in `PrivyProvider` only in privy mode. ESLint enforces it for the rest of `src/`.
- Config: `loginMethods: ["email", "sms"]`, `embeddedWallets.solana.createOnLogin: "users-without-wallets"`, `embeddedWallets.showWalletUIs: false`
  (Privy's own confirmation screens are off, our page runs the flow), external wallets off, signing on `solana:devnet`.
- The UI: a **Log in** button when there is no wallet (Privy's email screen opens), then "Signed in" with the shortened wallet address and a
  **Log out** button. The test USDC airdrop and the deposit use the Privy address exactly like the local wallet.
- **The wallet only signs, never sends.** `signDeposit` hands the service's unsigned legacy transaction to Privy's `signTransaction`
  (`@privy-io/react-auth/solana`), then checks the result before it is used: the signed message must equal `message_b64` byte for byte (fee payer,
  blockhash and instructions unchanged), the signature must be in the depositor's slot and must verify against that message. Anything else is
  refused and nothing is submitted. It returns `{ message_b64, user_signature_b64 }`, the same shape the local wallet returns.
- Errors (plain copy in `src/lib/errors.ts`): `WALLET_LOGIN_CANCELLED`, `WALLET_REJECTED`, `WALLET_NETWORK`, and the generic `WALLET`.

## Tests

`npm test` runs Vitest: unit tests for the pure logic (amount parsing, countdown, error copy against the API doc, status to view, polling
backoff, base58, the local wallet) and React Testing Library tests with a mocked API for each status view, deposit success, `TX_EXPIRED`, every
blocking error, the disabled other side, the full market message, the airdrop flow, hostile display names, and the Privy wallet (a mocked SDK for login, address, signing, rejection and logout, plus byte level checks that the signed message is unchanged).

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
