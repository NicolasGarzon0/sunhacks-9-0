// Full lifecycle on a real local validator (solana-test-validator, started by `anchor test`).
// The LiteSVM suite in tests/ holds the detailed cases; this proves the .so also behaves on a real validator.
import * as fs from "fs";
import * as path from "path";
import { expect } from "chai";
import { AnchorProvider, BN, Program, setProvider } from "@anchor-lang/core";
import { Keypair, LAMPORTS_PER_SOL, PublicKey, SystemProgram } from "@solana/web3.js";
import { TOKEN_PROGRAM_ID, createMint, getAccount, getAssociatedTokenAddressSync, getOrCreateAssociatedTokenAccount, mintTo } from "@solana/spl-token";

const IDL = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../target/idl/handshake_pool.json"), "utf8"));
const USDC = 1_000_000;

describe("validator smoke: full lifecycle", () => {
  const provider = AnchorProvider.env();
  setProvider(provider);
  const conn = provider.connection;
  const sponsor = (provider.wallet as any).payer as Keypair; // the throwaway wallet from Anchor.toml
  const program = new Program(IDL, provider);
  const oracle = Keypair.generate();
  const treasuryOwner = Keypair.generate();
  const userA = Keypair.generate(); // zero SOL
  const userB = Keypair.generate(); // zero SOL
  const betId = Array.from(Keypair.generate().publicKey.toBytes().slice(0, 16));
  const bet = PublicKey.findProgramAddressSync([Buffer.from("bet"), Buffer.from(betId)], program.programId)[0];
  const vault = PublicKey.findProgramAddressSync([Buffer.from("vault"), bet.toBuffer()], program.programId)[0];
  const pos = (o: PublicKey) => PublicKey.findProgramAddressSync([Buffer.from("position"), bet.toBuffer(), o.toBuffer()], program.programId)[0];
  let mint: PublicKey, ataA: PublicKey, ataB: PublicKey, treasuryAta: PublicKey;
  const bal = async (a: PublicKey) => Number((await getAccount(conn, a)).amount);

  before(async () => {
    const sig = await conn.requestAirdrop(oracle.publicKey, 2 * LAMPORTS_PER_SOL);
    await conn.confirmTransaction(sig, "confirmed");
    mint = await createMint(conn, sponsor, sponsor.publicKey, null, 6);
    ataA = (await getOrCreateAssociatedTokenAccount(conn, sponsor, mint, userA.publicKey)).address;
    ataB = (await getOrCreateAssociatedTokenAccount(conn, sponsor, mint, userB.publicKey)).address;
    treasuryAta = (await getOrCreateAssociatedTokenAccount(conn, sponsor, mint, treasuryOwner.publicKey)).address;
    await mintTo(conn, sponsor, mint, ataA, sponsor, 100 * USDC);
    await mintTo(conn, sponsor, mint, ataB, sponsor, 100 * USDC);
  });

  it("create, deposit x2 (user + sponsor sign), lock, settle, claim, close loser, close_bet", async () => {
    const closesAt = Math.floor(Date.now() / 1000) + 3600;
    await program.methods
      .createBet(betId, oracle.publicKey, treasuryOwner.publicKey, 500, new BN(1 * USDC), new BN(50 * USDC), 10, new BN(closesAt), new BN(closesAt + 12 * 3600))
      .accountsStrict({ payer: sponsor.publicKey, bet, mint, vault, tokenProgram: TOKEN_PROGRAM_ID, systemProgram: SystemProgram.programId })
      .rpc();

    for (const [user, ata, side, amt] of [[userA, ataA, 0, 13 * USDC], [userB, ataB, 1, 13 * USDC]] as const) {
      await program.methods
        .deposit(side, new BN(amt))
        .accountsStrict({ depositor: user.publicKey, rentPayer: sponsor.publicKey, bet, vault, mint, position: pos(user.publicKey), depositorToken: ata, tokenProgram: TOKEN_PROGRAM_ID, systemProgram: SystemProgram.programId })
        .signers([user]) // the provider wallet (sponsor) signs as fee payer and rent_payer
        .rpc();
    }
    expect(await conn.getBalance(userA.publicKey)).to.equal(0);
    expect(await bal(vault)).to.equal(26 * USDC);

    // a non authority cannot lock
    let failed = false;
    try { await program.methods.lock().accountsStrict({ authority: sponsor.publicKey, bet }).rpc(); } catch { failed = true; }
    expect(failed).to.equal(true);

    await program.methods.lock().accountsStrict({ authority: oracle.publicKey, bet }).signers([oracle]).rpc();
    await program.methods.settle(1).accountsStrict({ authority: oracle.publicKey, bet, vault, mint, treasuryToken: treasuryAta, tokenProgram: TOKEN_PROGRAM_ID }).signers([oracle]).rpc();
    expect(await bal(treasuryAta)).to.equal(650_000);

    // the sponsor submits the winner's claim
    const before = await bal(ataB);
    await program.methods
      .claim()
      .accountsStrict({ signer: sponsor.publicKey, bet, vault, mint, position: pos(userB.publicKey), ownerToken: ataB, rentReceiver: sponsor.publicKey, tokenProgram: TOKEN_PROGRAM_ID })
      .rpc();
    expect((await bal(ataB)) - before).to.equal(25_350_000);

    await program.methods.closeLoserPosition().accountsStrict({ signer: sponsor.publicKey, bet, position: pos(userA.publicKey), rentReceiver: sponsor.publicKey }).rpc();
    await program.methods.closeBet().accountsStrict({ authority: oracle.publicKey, bet, payer: sponsor.publicKey, vault, mint, treasuryToken: treasuryAta, tokenProgram: TOKEN_PROGRAM_ID }).signers([oracle]).rpc();

    expect(await conn.getAccountInfo(bet)).to.equal(null);
    expect(await conn.getAccountInfo(vault)).to.equal(null);
    expect(await bal(treasuryAta)).to.equal(650_000); // no dust in this vector
    expect(getAssociatedTokenAddressSync(mint, userB.publicKey).equals(ataB)).to.equal(true);
  });
});
