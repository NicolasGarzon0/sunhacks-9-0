import { expect } from "chai";
import { Keypair } from "@solana/web3.js";
import { Env, USDC, big, expectErr, expectOk, stateOf } from "./helpers";

const two = (env: Env) => env.market([[0, 0, 10n * USDC], [1, 1, 20n * USDC]]);
const evOf = (r: any, name: string) => r.events.find((e: any) => e.name.toLowerCase() === name.toLowerCase());

describe("lock", () => {
  let env: Env;
  beforeEach(() => { env = new Env(); });

  it("Open -> Locked with both sides and 2+ participants, emits an event", async () => {
    const { ref } = await two(env);
    const r = await env.lock(ref);
    expectOk(r);
    expect(stateOf(env.bet(ref))).to.equal("locked");
    expect(evOf(r, "BetLocked")).to.exist;
  });
  it("one sided market is rejected with BothSidesRequired", async () => {
    const { ref } = await env.market([[0, 0, 5n * USDC], [1, 0, 5n * USDC]]);
    expectErr(await env.lock(ref), "BothSidesRequired");
    expect(stateOf(env.bet(ref))).to.equal("open");
  });
  it("a single participant and an empty market are rejected", async () => {
    const { ref } = await env.market([[0, 0, 5n * USDC]]);
    expectErr(await env.lock(ref), "BothSidesRequired");
    expectErr(await env.lock(await env.open()), "BothSidesRequired");
  });
  it("non authority is rejected, even the sponsor", async () => {
    const { ref } = await two(env);
    expectErr(await env.lock(ref, env.payer), "Unauthorized");
    expectErr(await env.lock(ref, Keypair.generate()), "Unauthorized");
    expect(stateOf(env.bet(ref))).to.equal("open");
  });
  it("only from Open", async () => {
    const { ref } = await two(env);
    expectOk(await env.lock(ref));
    expectErr(await env.lock(ref), "WrongState");
    const { ref: r2 } = await two(env);
    expectOk(await env.refund(r2));
    expectErr(await env.lock(r2), "WrongState");
  });
  it("works after funding_closes_at has passed (the oracle locks then)", async () => {
    const { ref } = await two(env);
    env.setNow(env.now() + 10 * 3600);
    expectOk(await env.lock(ref));
  });
});

describe("settle", () => {
  let env: Env;
  beforeEach(() => { env = new Env(); });
  const locked = async (o = {}) => { const m = await env.market([[0, 0, 10n * USDC], [1, 1, 20n * USDC]], o); expectOk(await env.lock(m.ref)); return m; };

  it("fee lands in the treasury token account and the vault keeps the rest", async () => {
    const { ref } = await locked(); // side 1 wins: losing pool 10 -> fee 0.5
    const r = await env.settle(ref, 1);
    expectOk(r);
    const b = env.bet(ref);
    expect(stateOf(b)).to.equal("settled");
    expect(b.winningSide).to.equal(1);
    expect(big(b.feePaid)).to.equal(500_000n);
    expect(big(b.distributable)).to.equal(29_500_000n);
    expect(env.tokenBal(env.treasuryAta)).to.equal(500_000n);
    expect(env.tokenBal(ref.vault)).to.equal(29_500_000n);
    expect(big(evOf(r, "BetSettled").data.fee)).to.equal(500_000n);
  });
  it("side 0 winning takes the fee from side 1", async () => {
    const { ref } = await locked(); // losing pool 20 -> fee 1
    expectOk(await env.settle(ref, 0));
    expect(env.tokenBal(env.treasuryAta)).to.equal(1_000_000n);
    expect(big(env.bet(ref).distributable)).to.equal(29_000_000n);
  });
  it("fee zero path moves no tokens", async () => {
    const { ref } = await locked({ feeBps: 0 });
    expectOk(await env.settle(ref, 1));
    expect(env.tokenBal(env.treasuryAta)).to.equal(0n);
    expect(big(env.bet(ref).feePaid)).to.equal(0n);
    expect(big(env.bet(ref).distributable)).to.equal(30n * USDC);
    expect(env.tokenBal(ref.vault)).to.equal(30n * USDC);
  });
  it("fee that floors to zero moves no tokens", async () => {
    const m = await env.market([[0, 0, 19n], [1, 1, 40n]], { min: 1n, max: 100n * USDC, feeBps: 500 });
    expectOk(await env.lock(m.ref));
    expectOk(await env.settle(m.ref, 1)); // 5% of 19 = 0
    expect(big(env.bet(m.ref).feePaid)).to.equal(0n);
    expect(env.tokenBal(env.treasuryAta)).to.equal(0n);
  });
  it("wrong treasury token account is rejected", async () => {
    const { ref } = await locked();
    const stranger = env.ata(Keypair.generate().publicKey);
    expectErr(await env.settle(ref, 1, { treasuryToken: stranger }), "WrongTreasury");
    const other = Keypair.generate();
    env.makeMint(other, 6);
    const otherMintAta = env.ata(env.treasuryOwner.publicKey, other.publicKey);
    expectErr(await env.settle(ref, 1, { treasuryToken: otherMintAta }), "WrongTreasury");
    expectErr(await env.settle(ref, 1, { treasuryToken: ref.vault }), "ConstraintDuplicateMutableAccount"); // vault as its own destination is blocked by Anchor
    expect(stateOf(env.bet(ref))).to.equal("locked");
  });
  it("non authority is rejected", async () => {
    const { ref } = await locked();
    expectErr(await env.settle(ref, 1, { signer: env.payer }), "Unauthorized");
    expectErr(await env.settle(ref, 1, { signer: Keypair.generate() }), "Unauthorized");
  });
  it("state checks: only Locked can settle, and only once", async () => {
    const { ref: open } = await two(env);
    expectErr(await env.settle(open, 0), "WrongState");
    const { ref } = await locked();
    expectOk(await env.settle(ref, 1));
    expectErr(await env.settle(ref, 1), "WrongState");
    expectErr(await env.settle(ref, 0), "WrongState");
    const { ref: rf } = await two(env);
    expectOk(await env.refund(rf));
    expectErr(await env.settle(rf, 0), "WrongState");
  });
  it("InvalidSide for winning_side 2 and 255; wrong mint account rejected", async () => {
    const { ref } = await locked();
    expectErr(await env.settle(ref, 2), "InvalidSide");
    expectErr(await env.settle(ref, 255), "InvalidSide");
    const other = Keypair.generate();
    env.makeMint(other, 6);
    expectErr(await env.settle(ref, 1, { mint: other.publicKey }), "WrongMint");
  });
});

describe("refund_all", () => {
  let env: Env;
  beforeEach(() => { env = new Env(); });

  it("from Open: no transfers, no fee", async () => {
    const { ref } = await two(env);
    const r = await env.refund(ref);
    expectOk(r);
    const b = env.bet(ref);
    expect(stateOf(b)).to.equal("refunded");
    expect(big(b.feePaid)).to.equal(0n);
    expect(env.tokenBal(ref.vault)).to.equal(30n * USDC);
    expect(env.tokenBal(env.treasuryAta)).to.equal(0n);
    expect(evOf(r, "BetRefunded")).to.exist;
  });
  it("from Open with no deposits at all", async () => {
    expectOk(await env.refund(await env.open()));
  });
  it("from Locked", async () => {
    const { ref } = await two(env);
    expectOk(await env.lock(ref));
    expectOk(await env.refund(ref));
    expect(stateOf(env.bet(ref))).to.equal("refunded");
    expect(env.tokenBal(env.treasuryAta)).to.equal(0n);
  });
  it("non authority is rejected", async () => {
    const { ref } = await two(env);
    expectErr(await env.refund(ref, env.payer), "Unauthorized");
    expectErr(await env.refund(ref, Keypair.generate()), "Unauthorized");
  });
  it("not from Settled or Refunded", async () => {
    const { ref } = await two(env);
    expectOk(await env.lock(ref));
    expectOk(await env.settle(ref, 0));
    expectErr(await env.refund(ref), "WrongState");
    const { ref: r2 } = await two(env);
    expectOk(await env.refund(r2));
    expectErr(await env.refund(r2), "WrongState");
  });
});
