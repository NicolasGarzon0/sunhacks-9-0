# TextBet

Text an AI agent a game, get a betting link, share it with friends. Solana escrow holds the money and pays the winners automatically.

Built for SunHacks 9.0, Solana track.

## How it works

1. DM the agent on iMessage: "create a bet for ASU vs Texas Tech"
2. Confirm the game and get a betting link
3. Share the link. Friends pick a side, choose an amount, and pay
4. When the game ends, winners are paid automatically and the agent texts you the result

## Stack

- Agent: Python, Claude Haiku, Photon (iMessage)
- Backend: Python, Supabase, Render
- Escrow: Rust (Anchor) on Solana
- Deposit page: TypeScript, Vercel

Runs on Solana devnet with test USDC. No real money.

## Team

Nicolas Garzon, ASU
