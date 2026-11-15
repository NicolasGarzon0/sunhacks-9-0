// npm run smoke   End to end proof on devnet: settled market, refund market, emergency refund market.
// Users are throwaway wallets with ZERO SOL; the payer sponsors fees and rent.
import * as fs from "fs";
import * as path from "path";
import { AnchorProvider, BN, Program } from "@anchor-lang/core";
import { Connection, Keypair, PublicKey, SYSVAR_CLOCK_PUBKEY, SystemProgram, TransactionInstruction, TransactionMessage, VersionedTransaction } from "@solana/web3.js";
import { TOKEN_PROGRAM_ID, createAssociatedTokenAccountIdempotentInstruction, getAssociatedTokenAddressSync } from "@solana/spl-token";
import { airdropUsdc } from "./lib/airdrop";
import { ROOT, USDC, explorerTx, guardDevnet, loadKeypair, log, makeConnection, readDevnet, run, sleep, sol, withRetry } from "./lib/config";

const IDL = JSON.parse(fs.readFileSync(path.join(ROOT, "target/idl/handshake_pool.json"), "utf8"));
const results: { name: string; ok: boolean; detail: string }[] = [];
const links: [string, string][] = [];
function check(name: string, ok: boolean, detail = "") {
  results.push({ name, ok, detail });
  log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  (" + detail + ")" : ""}`);
}

run(async () => {
  const conn = makeConnection();
  await guardDevnet(conn);
  const d = readDevnet();
  if (!d.programId || !d.testUsdcMint) throw new Error("deploy the program and create the mint first (npm run deploy, npm run mint:create)");
  const programId = new PublicKey(d.programId);
  const mint = new PublicKey(d.testUsdcMint);
  if (IDL.address !== d.programId) throw new Error(`IDL address ${IDL.address} != devnet.json programId; rebuild (npm run build)`);
  const payer = loadKeypair("payer"), oracle = loadKeypair("oracle"), mintAuth = loadKeypair("mint-authority");
  const treasury = new PublicKey(d.keys.treasury);
  const treasuryAta = getAssociatedTokenAddressSync(mint, treasury, true);
  const wallet: any = { publicKey: payer.publicKey, signTransaction: async (t: any) => t, signAllTransactions: async (t: any) => t };
  const program = new Program(IDL, new AnchorProvider(conn, wallet, {}));
  const relayer = Keypair.generate(); // fee payer for claims and the negative test; gets a little SOL from the payer, returned at the end

  // ---------- helpers ----------
  const betPda = (id: Buffer) => PublicKey.findProgramAddressSync([Buffer.from("bet"), id], programId)[0];
  const vaultPda = (bet: PublicKey) => PublicKey.findProgramAddressSync([Buffer.from("vault"), bet.toBuffer()], programId)[0];
  const posPda = (bet: PublicKey, o: PublicKey) => PublicKey.findProgramAddressSync([Buffer.from("position"), bet.toBuffer(), o.toBuffer()], programId)[0];
  const ataOf = (o: PublicKey) => getAssociatedTokenAddressSync(mint, o, true);
  const tokenBal = async (a: PublicKey): Promise<bigint> => {
    try { return BigInt((await withRetry("token balance", () => conn.getTokenAccountBalance(a))).value.amount); } catch { return 0n; }
  };
  const lamports = (a: PublicKey) => withRetry("balance", () => conn.getBalance(a));
  const bh = () => withRetry("blockhash", () => conn.getLatestBlockhash("confirmed"));
  const freeze = async (ixs: TransactionInstruction[], feePayer: PublicKey) =>
    new VersionedTransaction(new TransactionMessage({ payerKey: feePayer, recentBlockhash: (await bh()).blockhash, instructions: ixs }).compileToV0Message());
  async function confirm(sig: string) {
    for (let i = 0; i < 90; i++) {
      const s = (await withRetry("status", () => conn.getSignatureStatuses([sig]))).value[0];
      if (s?.err) return s.err;
      if (s?.confirmationStatus === "confirmed" || s?.confirmationStatus === "finalized") return null;
      await sleep(1000);
    }
    throw new Error("transaction not confirmed in 90s: " + sig);
  }
  async function submit(tx: VersionedTransaction, label: string, link = true): Promise<string> {
    const sig = await withRetry(label, () => conn.sendRawTransaction(tx.serialize(), { preflightCommitment: "confirmed" }));
    const err = await confirm(sig);
    if (err) throw new Error(`${label} failed on chain: ${JSON.stringify(err)}`);
    if (link) links.push([label, sig]);
    await sleep(300); // be polite to the public RPC
    return sig;
  }
  /** one instruction, signed by the listed keys, fee payer first */
  const send = async (label: string, ixs: TransactionInstruction[], signers: Keypair[], feePayer: Keypair = payer, link = true) => {
    const tx = await freeze(ixs, feePayer.publicKey);
    tx.sign([feePayer, ...signers.filter((s) => !s.publicKey.equals(feePayer.publicKey))]);
    return submit(tx, label, link);
  };
  /** the call must be rejected by the program; checked by simulation (no fee) */
  async function expectFail(label: string, ixs: TransactionInstruction[], signers: Keypair[], code: string, feePayer: Keypair = payer) {
    const tx = await freeze(ixs, feePayer.publicKey);
    tx.sign([feePayer, ...signers.filter((s) => !s.publicKey.equals(feePayer.publicKey))]);
    const r = await withRetry(label, () => conn.simulateTransaction(tx, { commitment: "confirmed" }));
    const hit = !!r.value.err && (r.value.logs ?? []).some((l) => l.includes(`Error Code: ${code}`));
    check(label, hit, hit ? code : `expected ${code}, got ${JSON.stringify(r.value.err)}`);
  }
  async function chainNow(): Promise<number> {
    const a = await withRetry("clock", () => conn.getAccountInfo(SYSVAR_CLOCK_PUBKEY, "confirmed"));
    return Number(a!.data.readBigInt64LE(32));
  }
  async function waitUntil(ts: number, what: string) {
    for (;;) {
      const now = await chainNow();
      if (now > ts) return;
      log(`  waiting for ${what}: ${ts - now + 1}s`);
      await sleep(Math.min(8000, (ts - now + 1) * 1000));
    }
  }
  const getBet = async (bet: PublicKey) => program.coder.accounts.decode("bet", (await withRetry("bet", () => conn.getAccountInfo(bet, "confirmed")))!.data) as any;
  const state = (b: any) => Object.keys(b.state)[0];

  type Market = { id: Buffer; bet: PublicKey; vault: PublicKey; closes: number; refundAfter: number };
  async function createMarket(label: string, fundSecs: number, refundGap: number): Promise<Market> {
    const id = Buffer.from(Keypair.generate().publicKey.toBytes().slice(0, 16));
    const bet = betPda(id), vault = vaultPda(bet);
    const now = await chainNow();
    const closes = now + fundSecs, refundAfter = closes + refundGap;
    const ix = await program.methods
      .createBet(Array.from(id), oracle.publicKey, treasury, 500, new BN(1_000_000), new BN(20_000_000), 20, new BN(closes), new BN(refundAfter))
      .accountsStrict({ payer: payer.publicKey, bet, mint, vault, tokenProgram: TOKEN_PROGRAM_ID, systemProgram: SystemProgram.programId })
      .instruction();
    await send(`create_bet ${label}`, [ix], [payer]);
    return { id, bet, vault, closes, refundAfter };
  }
  const depositIx = (m: Market, user: PublicKey, side: number, amount: bigint) =>
    program.methods.deposit(side, new BN(amount.toString()))
      .accountsStrict({ depositor: user, rentPayer: payer.publicKey, bet: m.bet, vault: m.vault, mint, position: posPda(m.bet, user), depositorToken: ataOf(user), tokenProgram: TOKEN_PROGRAM_ID, systemProgram: SystemProgram.programId })
      .instruction();
  /** user pays nothing: build once, freeze the message, user signs, then the payer signs, then send */
  async function deposit(label: string, m: Market, user: Keypair, side: number, amount: bigint) {
    const tx = await freeze([await depositIx(m, user.publicKey, side, amount)], payer.publicKey);
    tx.sign([user]);
    tx.sign([payer]);
    await submit(tx, label);
  }
  const lockIx = (m: Market) => program.methods.lock().accountsStrict({ authority: oracle.publicKey, bet: m.bet }).instruction();
  const settleIx = (m: Market, side: number) => program.methods.settle(side).accountsStrict({ authority: oracle.publicKey, bet: m.bet, vault: m.vault, mint, treasuryToken: treasuryAta, tokenProgram: TOKEN_PROGRAM_ID }).instruction();
  const refundIx = (m: Market) => program.methods.refundAll().accountsStrict({ authority: oracle.publicKey, bet: m.bet }).instruction();
  const emergencyIx = (m: Market, signer: PublicKey) => program.methods.emergencyRefund().accountsStrict({ signer, bet: m.bet }).instruction();
  const claimIx = (m: Market, owner: PublicKey, signer: PublicKey) =>
    program.methods.claim().accountsStrict({ signer, bet: m.bet, vault: m.vault, mint, position: posPda(m.bet, owner), ownerToken: ataOf(owner), rentReceiver: payer.publicKey, tokenProgram: TOKEN_PROGRAM_ID }).instruction();
  const closeLoserIx = (m: Market, owner: PublicKey, signer: PublicKey) =>
    program.methods.closeLoserPosition().accountsStrict({ signer, bet: m.bet, position: posPda(m.bet, owner), rentReceiver: payer.publicKey }).instruction();
  const closeBetIx = (m: Market) =>
    program.methods.closeBet().accountsStrict({ authority: oracle.publicKey, bet: m.bet, payer: payer.publicKey, vault: m.vault, mint, treasuryToken: treasuryAta, tokenProgram: TOKEN_PROGRAM_ID }).instruction();
  const fund = async (w: Keypair, usdc: string) => { await airdropUsdc({ connection: conn, payer, mintAuthority: mintAuth, mint, wallet: w.publicKey, amount: usdc }); await sleep(300); };

  const floorPayout = (stake: bigint, dist: bigint, win: bigint) => (stake * dist) / win;

  // ---------- setup ----------
  const startPayer = await lamports(payer.publicKey);
  log(`program ${programId.toBase58()}\nmint    ${mint.toBase58()}\npayer   ${payer.publicKey.toBase58()}  ${sol(startPayer)} SOL`);
  if (startPayer < 200_000_000) throw new Error("payer has less than 0.2 SOL; fund it first (npm run fund)");
  await send("setup: treasury token account + relayer gas", [
    createAssociatedTokenAccountIdempotentInstruction(payer.publicKey, treasuryAta, treasury, mint),
    SystemProgram.transfer({ fromPubkey: payer.publicKey, toPubkey: relayer.publicKey, lamports: 10_000_000 }),
  ], [payer], payer, false);

  const [u1, u2, u3, u4, b1, b2, c1] = Array.from({ length: 7 }, () => Keypair.generate()); // zero SOL
  log("\n== funding throwaway users with test USDC (they hold zero SOL) ==");
  for (const u of [u1, u2, u3, u4]) await fund(u, "25");
  for (const u of [b1, b2, c1]) await fund(u, "25");
  check("throwaway users hold zero SOL", (await Promise.all([u1, u2, u3, u4, b1, b2, c1].map((u) => lamports(u.publicKey)))).every((x) => x === 0));

  // ---------- create the markets (funding window ~100s) ----------
  log("\n== market A (settled path) ==");
  const A = await createMarket("A", 100, 300);
  const betA = await getBet(A.bet);
  check("A: Bet account is 236 bytes", (await conn.getAccountInfo(A.bet))!.data.length === 236);
  check("A: stored payer/authority/treasury/mint", betA.payer.equals(payer.publicKey) && betA.authority.equals(oracle.publicKey) && betA.treasury.equals(treasury) && betA.mint.equals(mint));

  const aRows: [Keypair, number, bigint][] = [[u1, 0, 5n * USDC], [u2, 0, 7n * USDC], [u3, 1, 6n * USDC], [u4, 1, 9_250_000n]];
  for (const [i, [u, side, amt]] of aRows.entries()) await deposit(`A deposit u${i + 1} side ${side}`, A, u, side, amt);
  await deposit("A top up u1 side 0 (+3)", A, u1, 0, 3n * USDC);
  const stakes = new Map<string, bigint>([[u1.publicKey.toBase58(), 8n * USDC], [u2.publicKey.toBase58(), 7n * USDC], [u3.publicKey.toBase58(), 6n * USDC], [u4.publicKey.toBase58(), 9_250_000n]]);
  check("A: users still hold zero SOL after depositing", (await Promise.all([u1, u2, u3, u4].map((u) => lamports(u.publicKey)))).every((x) => x === 0));
  check("A: top up kept one position per user", (await getBet(A.bet)).participantCount === 4);

  // payer signature is required: client side the frozen tx is incomplete, and on chain a tx without it fails
  {
    const tx = await freeze([await depositIx(A, u2.publicKey, 0, USDC)], payer.publicKey);
    tx.sign([u2]); // user only, payer never signs
    let rejected = false;
    try { const s = await conn.sendRawTransaction(tx.serialize(), { preflightCommitment: "confirmed" }); rejected = !!(await confirm(s)); } catch { rejected = true; }
    check("deposit signed only by the user (payer is fee payer) is rejected", rejected);
  }
  {
    // payer present in the account list but NOT signing rent_payer; a relayer pays the fee; sent without preflight so it lands on chain
    const ix = await depositIx(A, u2.publicKey, 0, USDC);
    ix.keys.forEach((k) => { if (k.pubkey.equals(payer.publicKey)) k.isSigner = false; });
    const tx = await freeze([ix], relayer.publicKey);
    tx.sign([relayer, u2]);
    const sig = await withRetry("neg deposit", () => conn.sendRawTransaction(tx.serialize(), { skipPreflight: true }));
    const err = await confirm(sig);
    await sleep(1500);
    const t = await withRetry("getTransaction", () => conn.getTransaction(sig, { commitment: "confirmed", maxSupportedTransactionVersion: 0 }));
    const logs = t?.meta?.logMessages ?? [];
    check("on chain: deposit without the payer signature fails (AccountNotSigner)", !!err && logs.some((l) => l.includes("AccountNotSigner")), JSON.stringify(err));
    links.push(["A negative deposit (user signed only), failed on chain", sig]);
  }

  // ---------- market B and C created now so their waits overlap ----------
  log("\n== market B (refund path) and C (emergency refund) ==");
  const B = await createMarket("B", 600, 600);
  await deposit("B deposit b1 side 0", B, b1, 0, 4n * USDC);
  await deposit("B deposit b2 side 0", B, b2, 0, 6n * USDC);
  const C = await createMarket("C", 100, 70);
  await deposit("C deposit c1 side 0", C, c1, 0, 2n * USDC);

  // ---------- A: wait, lock, settle, claim ----------
  log("\n== A: lock, settle, claims ==");
  await waitUntil(A.closes, "funding to end");
  await expectFail("A: deposit after funding_closes_at rejected (FundingClosed)", [await depositIx(A, u1.publicKey, 0, USDC)], [u1, payer], "FundingClosed");
  await send("A lock (oracle)", [await lockIx(A)], [oracle]);
  const treasuryBefore = await tokenBal(treasuryAta);
  await send("A settle side 1 (oracle)", [await settleIx(A, 1)], [oracle]);
  const sA = await getBet(A.bet);
  const side0 = 15n * USDC, side1 = 15_250_000n;
  const fee = (side0 * 500n) / 10_000n, dist = side0 + side1 - fee;
  check("A: fee is 5% of the losing side", BigInt(sA.feePaid.toString()) === fee && fee === 750_000n, `${fee}`);
  check("A: distributable = pool - fee", BigInt(sA.distributable.toString()) === dist, `${dist}`);
  check("A: fee sits in the treasury token account", (await tokenBal(treasuryAta)) - treasuryBefore === fee);

  const rentOf = new Map<string, number>();
  for (const u of [u1, u2, u3, u4]) rentOf.set(u.publicKey.toBase58(), await lamports(posPda(A.bet, u.publicKey)));
  const payerBeforeClaims = await lamports(payer.publicKey);
  let paid = 0n;
  for (const [i, u] of [u3, u4].entries()) {
    const before = await tokenBal(ataOf(u.publicKey));
    await send(`A claim u${i + 3} (any signer: relayer)`, [await claimIx(A, u.publicKey, relayer.publicKey)], [relayer], relayer);
    const got = (await tokenBal(ataOf(u.publicKey))) - before;
    const want = floorPayout(stakes.get(u.publicKey.toBase58())!, dist, side1);
    paid += got;
    check(`A: u${i + 3} payout equals the pool math`, got === want && got >= stakes.get(u.publicKey.toBase58())!, `${got} of ${want}`);
  }
  await expectFail("A: loser cannot claim (NotAWinner)", [await claimIx(A, u1.publicKey, relayer.publicKey)], [relayer], "NotAWinner", relayer);
  for (const [i, u] of [u1, u2].entries()) await send(`A close loser u${i + 1}`, [await closeLoserIx(A, u.publicKey, relayer.publicKey)], [relayer], relayer);
  const rentTotal = [...rentOf.values()].reduce((a, b) => a + b, 0);
  const payerAfterClaims = await lamports(payer.publicKey);
  check("A: payer got every position rent lamport back", payerAfterClaims - payerBeforeClaims === rentTotal, `${rentTotal} lamports`);
  const dust = await tokenBal(A.vault);
  check("A: vault holds only rounding dust", dust === dist - paid && dust < 2n, `${dust} base units`);
  check("A: all positions closed", (await getBet(A.bet)).positionsOpen === 0);
  check("A: treasury holds exactly the fee", (await tokenBal(treasuryAta)) - treasuryBefore === fee);
  await send("A close_bet (oracle)", [await closeBetIx(A)], [oracle]);
  check("A: Bet and Vault closed", !(await conn.getAccountInfo(A.bet)) && !(await conn.getAccountInfo(A.vault)));

  // ---------- B: refund ----------
  log("\n== B: refund_all ==");
  const trB = await tokenBal(treasuryAta);
  await send("B refund_all (oracle)", [await refundIx(B)], [oracle]);
  const bBet = await getBet(B.bet);
  check("B: state Refunded, fee 0", state(bBet) === "refunded" && BigInt(bBet.feePaid.toString()) === 0n);
  for (const [u, stake] of [[b1, 4n * USDC], [b2, 6n * USDC]] as [Keypair, bigint][]) {
    const before = await tokenBal(ataOf(u.publicKey));
    await send("B claim (refund)", [await claimIx(B, u.publicKey, relayer.publicKey)], [relayer], relayer);
    check("B: refund returns the full stake", (await tokenBal(ataOf(u.publicKey))) - before === stake);
  }
  check("B: no fee reached the treasury, vault is empty", (await tokenBal(treasuryAta)) === trB && (await tokenBal(B.vault)) === 0n);
  await send("B close_bet (oracle)", [await closeBetIx(B)], [oracle]);

  // ---------- C: emergency refund ----------
  log("\n== C: emergency_refund (short deadlines) ==");
  await expectFail("C: emergency_refund before refund_after rejected (RefundTooEarly)", [await emergencyIx(C, relayer.publicKey)], [relayer], "RefundTooEarly", relayer);
  await waitUntil(C.refundAfter, "refund_after");
  await expectFail("C: lock after refund_after rejected (PastRefundDeadline)", [await lockIx(C)], [oracle], "PastRefundDeadline");
  await send("C emergency_refund (random signer)", [await emergencyIx(C, relayer.publicKey)], [relayer], relayer);
  const cBefore = await tokenBal(ataOf(c1.publicKey));
  await send("C claim (refund)", [await claimIx(C, c1.publicKey, relayer.publicKey)], [relayer], relayer);
  check("C: claim after emergency refund returns the full stake, vault empty", (await tokenBal(ataOf(c1.publicKey))) - cBefore === 2n * USDC && (await tokenBal(C.vault)) === 0n);
  await send("C close_bet (oracle)", [await closeBetIx(C)], [oracle]);

  // ---------- return the relayer's leftover gas ----------
  const left = await lamports(relayer.publicKey);
  if (left > 5000) await send("relayer returns leftover gas", [SystemProgram.transfer({ fromPubkey: relayer.publicKey, toPubkey: payer.publicKey, lamports: left - 5000 })], [relayer], relayer, false);

  // ---------- summary ----------
  const endPayer = await lamports(payer.publicKey);
  log("\n== explorer links ==");
  for (const [l, s] of links) log(`  ${l}\n    ${explorerTx(s)}`);
  log(`\npayer SOL spent: ${sol(startPayer - endPayer)} (fees, 8 user token accounts, treasury account; position/bet/vault rent was returned)`);
  const failed = results.filter((r) => !r.ok);
  log(`\n${results.length - failed.length}/${results.length} checks passed`);
  if (failed.length) { log("FAILED:\n" + failed.map((f) => "  - " + f.name + " " + f.detail).join("\n")); process.exit(1); }
  log("SMOKE TEST PASS");
});
