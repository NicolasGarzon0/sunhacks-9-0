import { expect } from "chai";
import { Keypair, PublicKey } from "@solana/web3.js";
import { TOKEN_2022_PROGRAM_ID, createInitializeMint2Instruction } from "@solana/spl-token";
import { SystemProgram } from "@solana/web3.js";
import { Env, HOUR, NOW, USDC, expectErr, expectOk, big, stateOf } from "./helpers";

describe("create_bet", () => {
  let env: Env;
  beforeEach(() => { env = new Env(); });

  it("creates the Bet and the Vault and stores the sponsor as payer", async () => {
    const before = env.lamports(env.payer.publicKey);
    const { ref, res } = await env.createBet({ feeBps: 500, min: 1n * USDC, max: 20n * USDC, maxP: 8 });
    expectOk(res);
    const b = env.bet(ref);
    expect(b.authority.toBase58()).to.equal(env.authority.publicKey.toBase58());
    expect(b.payer.toBase58()).to.equal(env.payer.publicKey.toBase58());
    expect(b.treasury.toBase58()).to.equal(env.treasuryOwner.publicKey.toBase58());
    expect(b.mint.toBase58()).to.equal(env.mint.toBase58());
    expect(b.feeBps).to.equal(500);
    expect(big(b.minStake)).to.equal(1n * USDC);
    expect(big(b.maxStake)).to.equal(20n * USDC);
    expect(b.maxParticipants).to.equal(8);
    expect(b.participantCount).to.equal(0);
    expect(b.positionsOpen).to.equal(0);
    expect(b.sideTotals.map(big)).to.deep.equal([0n, 0n]);
    expect(stateOf(b)).to.equal("open");
    expect(b.winningSide).to.equal(255);
    expect(big(b.feePaid)).to.equal(0n);
    expect(big(b.distributable)).to.equal(0n);
    expect(Number(b.fundingClosesAt)).to.equal(NOW + HOUR);
    expect(Number(b.refundAfter)).to.equal(NOW + 13 * HOUR);
    expect(Number(b.createdAt)).to.equal(NOW);
    expect(Buffer.from(b.betId).equals(ref.betId)).to.equal(true);
    expect(env.exists(ref.vault)).to.equal(true);
    expect(env.tokenBal(ref.vault)).to.equal(0n);
    expect(env.lamports(env.payer.publicKey) < before).to.equal(true); // the sponsor paid rent
    const [bet, bump] = PublicKey.findProgramAddressSync([Buffer.from("bet"), ref.betId], new PublicKey(env.program.programId));
    expect(bet.toBase58()).to.equal(ref.bet.toBase58());
    expect(b.bump).to.equal(bump);
  });

  it("fee cap: 1000 bps is fine, 1001 is FeeTooHigh", async () => {
    expectOk((await env.createBet({ feeBps: 1000 })).res);
    expectErr((await env.createBet({ feeBps: 1001 })).res, "FeeTooHigh");
    expectOk((await env.createBet({ feeBps: 0 })).res);
  });

  it("rejects bad stake limits", async () => {
    expectErr((await env.createBet({ min: 0n })).res, "InvalidParams");
    expectErr((await env.createBet({ min: 5n * USDC, max: 4n * USDC })).res, "InvalidParams");
    expectOk((await env.createBet({ min: 5n * USDC, max: 5n * USDC })).res); // equal is allowed
  });

  it("rejects bad participant caps (2..=100 allowed)", async () => {
    expectErr((await env.createBet({ maxP: 1 })).res, "InvalidParams");
    expectErr((await env.createBet({ maxP: 0 })).res, "InvalidParams");
    expectErr((await env.createBet({ maxP: 101 })).res, "InvalidParams");
    expectOk((await env.createBet({ maxP: 2 })).res);
    expectOk((await env.createBet({ maxP: 100 })).res);
  });

  it("rejects funding_closes_at that is not in the future", async () => {
    expectErr((await env.createBet({ closesAt: NOW })).res, "InvalidParams");
    expectErr((await env.createBet({ closesAt: NOW - 1 })).res, "InvalidParams");
    expectOk((await env.createBet({ closesAt: NOW + 1 })).res);
  });

  it("rejects refund_after that is not strictly after funding_closes_at", async () => {
    expectErr((await env.createBet({ closesAt: NOW + HOUR, refundAfter: NOW + HOUR })).res, "InvalidParams");
    expectErr((await env.createBet({ closesAt: NOW + HOUR, refundAfter: NOW + HOUR - 1 })).res, "InvalidParams");
    expectErr((await env.createBet({ refundAfter: 0 })).res, "InvalidParams");
    expectOk((await env.createBet({ closesAt: NOW + HOUR, refundAfter: NOW + HOUR + 1 })).res);
  });

  it("refuses a Token-2022 mint", async () => {
    const m22 = Keypair.generate();
    const rent = env.svm.minimumBalanceForRentExemption(82n);
    const mk = env.send(
      [
        SystemProgram.createAccount({ fromPubkey: env.payer.publicKey, newAccountPubkey: m22.publicKey, lamports: Number(rent), space: 82, programId: TOKEN_2022_PROGRAM_ID }),
        createInitializeMint2Instruction(m22.publicKey, 6, env.mintAuth.publicKey, null, TOKEN_2022_PROGRAM_ID),
      ],
      [m22, env.payer],
    );
    expectOk(mk);
    const { res } = await env.createBet({ mint: m22.publicKey, tokenProgram: TOKEN_2022_PROGRAM_ID });
    expectErr(res, "WrongMint");
  });

  it("rejects a mint that does not have 6 decimals", async () => {
    const m9 = Keypair.generate();
    env.makeMint(m9, 9);
    expectErr((await env.createBet({ mint: m9.publicKey })).res, "InvalidParams");
    const m0 = Keypair.generate();
    env.makeMint(m0, 0);
    expectErr((await env.createBet({ mint: m0.publicKey })).res, "InvalidParams");
  });

  it("rejects default authority or treasury (would strand funds)", async () => {
    expectErr((await env.createBet({ authority: PublicKey.default })).res, "InvalidParams");
    expectErr((await env.createBet({ treasuryOwner: PublicKey.default })).res, "InvalidParams");
  });

  it("the same bet_id cannot be created twice", async () => {
    const id = env.newBetId();
    expectOk((await env.createBet({ betId: id })).res);
    expect((await env.createBet({ betId: id })).res.ok).to.equal(false);
  });

  it("the payer must sign", async () => {
    const { ix } = await env.createBetIx();
    // relayer pays the fee but the sponsor key never signs: the transaction cannot even be signed
    expect(() => env.send([ix], [], env.relayer)).to.throw(/signature/i);
  });
});
