import { expect } from "chai";
import { Keypair } from "@solana/web3.js";
import { Env, HOUR, NOW, USDC, big, expectErr, expectOk } from "./helpers";

describe("deposit", () => {
  let env: Env;
  beforeEach(() => { env = new Env(); });

  it("moves tokens into the vault and records the position, totals and counts", async () => {
    const ref = await env.open();
    const u = env.user(50n);
    const r = await env.deposit(ref, u, 1, 7n * USDC);
    expectOk(r);
    expect(env.tokenBal(ref.vault)).to.equal(7n * USDC);
    expect(env.tokenBal(env.ata(u.publicKey))).to.equal(43n * USDC);
    const b = env.bet(ref);
    expect(b.sideTotals.map(big)).to.deep.equal([0n, 7n * USDC]);
    expect(b.participantCount).to.equal(1);
    expect(b.positionsOpen).to.equal(1);
    const p = env.pos(ref, u.publicKey);
    expect(p.owner.toBase58()).to.equal(u.publicKey.toBase58());
    expect(p.bet.toBase58()).to.equal(ref.bet.toBase58());
    expect(p.side).to.equal(1);
    expect(big(p.amount)).to.equal(7n * USDC);
    const ev = r.events.find((e) => e.name.toLowerCase() === "deposited")!;
    expect(ev, "Deposited event").to.exist;
    expect(big(ev.data.amount)).to.equal(7n * USDC);
    expect(ev.data.side).to.equal(1);
    expect(ev.data.newPosition).to.equal(true);
  });

  it("BelowMin / AboveMax on a single deposit / InvalidSide", async () => {
    const ref = await env.open({ min: 2n * USDC, max: 20n * USDC });
    const u = env.user();
    expectErr(await env.deposit(ref, u, 0, 2n * USDC - 1n), "BelowMin");
    expectErr(await env.deposit(ref, u, 0, 20n * USDC + 1n), "AboveMax");
    expectErr(await env.deposit(ref, u, 2, 5n * USDC), "InvalidSide");
    expectErr(await env.deposit(ref, u, 255, 5n * USDC), "InvalidSide");
    expectOk(await env.deposit(ref, u, 0, 2n * USDC)); // exactly min
    expectOk(await env.deposit(ref, u, 0, 18n * USDC)); // exactly max after top up
  });

  it("top up on the same side adds to one position and does not add a participant", async () => {
    const ref = await env.open();
    const u = env.user();
    expectOk(await env.deposit(ref, u, 0, 5n * USDC));
    expectOk(await env.deposit(ref, u, 0, 7n * USDC));
    const p = env.pos(ref, u.publicKey);
    expect(big(p.amount)).to.equal(12n * USDC);
    const b = env.bet(ref);
    expect(b.participantCount).to.equal(1);
    expect(b.positionsOpen).to.equal(1);
    expect(b.sideTotals.map(big)).to.deep.equal([12n * USDC, 0n]);
    expect(env.tokenBal(ref.vault)).to.equal(12n * USDC);
  });

  it("AboveMax by top ups changes nothing", async () => {
    const ref = await env.open({ max: 20n * USDC });
    const u = env.user();
    expectOk(await env.deposit(ref, u, 0, 15n * USDC));
    expectErr(await env.deposit(ref, u, 0, 6n * USDC), "AboveMax");
    expect(big(env.pos(ref, u.publicKey).amount)).to.equal(15n * USDC);
    expect(env.tokenBal(ref.vault)).to.equal(15n * USDC);
    expectOk(await env.deposit(ref, u, 0, 5n * USDC)); // exactly max
  });

  it("the other side is rejected with AlreadyOtherSide", async () => {
    const ref = await env.open();
    const u = env.user();
    expectOk(await env.deposit(ref, u, 0, 5n * USDC));
    expectErr(await env.deposit(ref, u, 1, 5n * USDC), "AlreadyOtherSide");
    expect(env.pos(ref, u.publicKey).side).to.equal(0);
  });

  it("participant cap blocks new wallets but a top up is still allowed on a full market", async () => {
    const ref = await env.open({ maxP: 2 });
    const [a, b, c] = [env.user(), env.user(), env.user()];
    expectOk(await env.deposit(ref, a, 0, 5n * USDC));
    expectOk(await env.deposit(ref, b, 1, 5n * USDC));
    expectErr(await env.deposit(ref, c, 0, 5n * USDC), "MarketFull");
    expect(env.pos(ref, c.publicKey)).to.equal(null); // the failed deposit left no position behind
    expectOk(await env.deposit(ref, a, 0, 3n * USDC)); // top up on a full market
    expect(env.bet(ref).participantCount).to.equal(2);
  });

  it("MarketNotOpen after lock and after refund", async () => {
    const { ref, users } = await env.market([[0, 0, 5n * USDC], [1, 1, 5n * USDC]]);
    expectOk(await env.lock(ref));
    expectErr(await env.deposit(ref, users[0], 0, USDC), "MarketNotOpen");
    expectErr(await env.deposit(ref, env.user(), 0, USDC), "MarketNotOpen");
    const ref2 = await env.open();
    expectOk(await env.refund(ref2));
    expectErr(await env.deposit(ref2, env.user(), 0, USDC), "MarketNotOpen");
  });

  it("FundingClosed at and after funding_closes_at, allowed one second before", async () => {
    const ref = await env.open({ closesAt: NOW + HOUR });
    const u = env.user();
    env.setNow(NOW + HOUR - 1);
    expectOk(await env.deposit(ref, u, 0, USDC));
    env.setNow(NOW + HOUR);
    expectErr(await env.deposit(ref, u, 0, USDC), "FundingClosed");
    env.setNow(NOW + 10 * HOUR);
    expectErr(await env.deposit(ref, env.user(), 1, USDC), "FundingClosed");
  });

  it("error priority: state, then funding window, then side, then min", async () => {
    const ref = await env.open({ min: 2n * USDC });
    const u = env.user();
    env.setNow(NOW + 2 * HOUR);
    expectErr(await env.deposit(ref, u, 9, 1n), "FundingClosed"); // closed beats invalid side and below min
    env.setNow(NOW);
    expectErr(await env.deposit(ref, u, 9, 1n), "InvalidSide"); // side beats below min
    expectOk(await env.refund(ref));
    env.setNow(NOW + 2 * HOUR);
    expectErr(await env.deposit(ref, u, 9, 1n), "MarketNotOpen"); // state beats everything
  });

  it("wrong mint is rejected", async () => {
    const ref = await env.open();
    const other = Keypair.generate();
    env.makeMint(other, 6);
    const u = env.user(100n);
    const otherAta = env.fund(u.publicKey, 100n, other.publicKey);
    // other mint's token account with the real mint account passed
    expectErr(await env.deposit(ref, u, 0, 5n * USDC, { userToken: otherAta }), "WrongMint");
    // the other mint passed as the mint account
    expectErr(await env.deposit(ref, u, 0, 5n * USDC, { mint: other.publicKey, userToken: otherAta }), "WrongMint");
    expect(env.tokenBal(ref.vault)).to.equal(0n);
  });

  it("the sponsor pays position rent and a user with zero SOL still deposits", async () => {
    const ref = await env.open();
    env.feePayer = env.relayer; // isolate the sponsor's lamports from tx fees
    const u = env.user(10n);
    expect(env.lamports(u.publicKey)).to.equal(0n);
    const before = env.lamports(env.payer.publicKey);
    expectOk(await env.deposit(ref, u, 0, 4n * USDC));
    const rent = env.svm.minimumBalanceForRentExemption(BigInt(env.svm.getAccount(env.posPda(ref.bet, u.publicKey).toBase58() as any).data.length));
    expect(before - env.lamports(env.payer.publicKey)).to.equal(rent);
    expect(env.lamports(u.publicKey)).to.equal(0n); // user never held or spent SOL
  });

  it("rent_payer must be bet.payer and must sign", async () => {
    const ref = await env.open();
    const u = env.user();
    const impostor = Keypair.generate();
    env.svm.airdrop(impostor.publicKey.toBase58() as any, 10_000_000_000n);
    expectErr(await env.deposit(ref, u, 0, USDC, { rentPayer: impostor }), "Unauthorized");
    // the sponsor key present but not signing
    const ix = await env.depositIx(ref, u, 0, USDC);
    expect(() => env.send([ix], [u], env.relayer)).to.throw(/signature/i);
    // the user key present but not signing
    expect(() => env.send([ix], [env.payer], env.relayer)).to.throw(/signature/i);
  });

  it("the depositor token account must belong to the depositor", async () => {
    const ref = await env.open();
    const victim = env.user(100n);
    const thief = env.user(100n);
    const victimAta = env.ata(victim.publicKey);
    expectErr(await env.deposit(ref, thief, 0, 5n * USDC, { userToken: victimAta }), "Unauthorized");
    expect(env.tokenBal(victimAta)).to.equal(100n * USDC);
  });

  it("an existing position keeps its owner and bet and cannot be reinitialised", async () => {
    const ref = await env.open();
    const u = env.user();
    expectOk(await env.deposit(ref, u, 0, 5n * USDC));
    const before = env.pos(ref, u.publicKey);
    expectOk(await env.deposit(ref, u, 0, 5n * USDC));
    const after = env.pos(ref, u.publicKey);
    expect(after.owner.toBase58()).to.equal(before.owner.toBase58());
    expect(after.bet.toBase58()).to.equal(before.bet.toBase58());
    expect(after.bump).to.equal(before.bump);
    expect(big(after.amount)).to.equal(10n * USDC);
    expect(env.bet(ref).participantCount).to.equal(1);
  });
});
