// npm run airdrop -- <wallet> [amount]   (default 25 USDC, hard cap 100 per call)
import { PublicKey } from "@solana/web3.js";
import { airdropUsdc, DEFAULT_AIRDROP_USDC, parseUsdcAmount } from "./lib/airdrop";
import { guardDevnet, loadKeypair, log, makeConnection, readDevnet, run } from "./lib/config";

run(async () => {
  const [wallet, amount = DEFAULT_AIRDROP_USDC] = process.argv.slice(2).filter((a) => !a.startsWith("--"));
  if (!wallet) throw new Error("usage: npm run airdrop -- <wallet> [amount]");
  parseUsdcAmount(amount); // fail fast before touching the network
  const conn = makeConnection();
  await guardDevnet(conn);
  const d = readDevnet();
  if (!d.testUsdcMint) throw new Error("no test mint yet: run npm run mint:create");
  const r = await airdropUsdc({ connection: conn, payer: loadKeypair("payer"), mintAuthority: loadKeypair("mint-authority"), mint: new PublicKey(d.testUsdcMint), wallet, amount });
  log(`minted ${Number(r.amountBase) / 1e6} USDC to ${wallet}\n  token account ${r.ata}\n  signature ${r.signature}\n  ${r.explorer}`);
});
