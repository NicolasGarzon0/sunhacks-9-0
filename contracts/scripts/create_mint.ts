// npm run mint:create   Create the 6 decimal test USDC mint (once) and mint an initial supply to the payer.
import { Keypair, PublicKey, SystemProgram, Transaction } from "@solana/web3.js";
import { MINT_SIZE, TOKEN_PROGRAM_ID, createAssociatedTokenAccountIdempotentInstruction, createInitializeMint2Instruction, createMintToCheckedInstruction, getAssociatedTokenAddressSync, getMint } from "@solana/spl-token";
import { USDC, explorerAddr, explorerTx, guardDevnet, loadKeypair, log, makeConnection, readDevnet, run, withRetry, writeDevnet, writeEnvGenerated } from "./lib/config";

const INITIAL_SUPPLY = 10_000n * USDC;

run(async () => {
  const conn = makeConnection();
  await guardDevnet(conn);
  const d = readDevnet();
  const payer = loadKeypair("payer");
  const mintAuth = loadKeypair("mint-authority");

  let mint: PublicKey;
  if (d.testUsdcMint && (await conn.getAccountInfo(new PublicKey(d.testUsdcMint)))) {
    mint = new PublicKey(d.testUsdcMint);
    log(`mint exists ${mint.toBase58()}`);
  } else {
    const kp = Keypair.generate(); // the mint address is public; its key is not kept
    mint = kp.publicKey;
    const rent = await conn.getMinimumBalanceForRentExemption(MINT_SIZE);
    const tx = new Transaction().add(
      SystemProgram.createAccount({ fromPubkey: payer.publicKey, newAccountPubkey: mint, lamports: rent, space: MINT_SIZE, programId: TOKEN_PROGRAM_ID }),
      createInitializeMint2Instruction(mint, 6, mintAuth.publicKey, null), // no freeze authority
    );
    tx.feePayer = payer.publicKey;
    const sig = await withRetry("create mint", async () => {
      tx.recentBlockhash = (await conn.getLatestBlockhash("confirmed")).blockhash;
      tx.signatures = [];
      tx.sign(payer, kp);
      return conn.sendRawTransaction(tx.serialize());
    });
    await waitConfirmed(sig);
    log(`created mint ${mint.toBase58()}\n  ${explorerTx(sig)}`);
    d.testUsdcMint = mint.toBase58();
    writeDevnet(d);
  }

  const info = await getMint(conn, mint);
  if (info.decimals !== 6 || info.freezeAuthority !== null || !info.mintAuthority?.equals(mintAuth.publicKey)) {
    throw new Error("mint on chain does not have decimals 6, no freeze authority and the mint_authority key as authority");
  }

  const ata = getAssociatedTokenAddressSync(mint, payer.publicKey);
  const have = (await conn.getAccountInfo(ata)) ? (await conn.getTokenAccountBalance(ata)).value.amount : "0";
  if (BigInt(have) < INITIAL_SUPPLY) {
    const tx = new Transaction().add(
      createAssociatedTokenAccountIdempotentInstruction(payer.publicKey, ata, payer.publicKey, mint),
      createMintToCheckedInstruction(mint, ata, mintAuth.publicKey, INITIAL_SUPPLY, 6),
    );
    tx.feePayer = payer.publicKey;
    const sig = await withRetry("mint supply", async () => {
      tx.recentBlockhash = (await conn.getLatestBlockhash("confirmed")).blockhash;
      tx.signatures = [];
      tx.sign(payer, mintAuth);
      return conn.sendRawTransaction(tx.serialize());
    });
    await waitConfirmed(sig);
    log(`minted ${INITIAL_SUPPLY / USDC} USDC to the payer token account ${ata.toBase58()}\n  ${explorerTx(sig)}`);
  } else log(`payer already holds ${Number(have) / 1e6} USDC`);
  writeEnvGenerated();
  log(`\nOK  mint ${mint.toBase58()}  ${explorerAddr(mint)}`);

  async function waitConfirmed(sig: string) {
    for (let i = 0; i < 60; i++) {
      const s = (await conn.getSignatureStatuses([sig])).value[0];
      if (s?.err) throw new Error("transaction failed: " + JSON.stringify(s.err));
      if (s?.confirmationStatus === "confirmed" || s?.confirmationStatus === "finalized") return;
      await new Promise((r) => setTimeout(r, 1000));
    }
    throw new Error("not confirmed in 60s");
  }
});
