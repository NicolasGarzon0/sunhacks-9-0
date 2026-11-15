import { expect } from "chai";
import { Keypair } from "@solana/web3.js";
import { Env, HOUR, NOW, USDC, big, expectErr, expectOk, stateOf } from "./helpers";

const CLOSES = NOW + HOUR;
const REFUND = CLOSES + 12 * HOUR; // the default in the helpers
const two = (env: Env) => env.market([[0, 0, 10n * USDC], [1, 1, 20n * USDC]]);

describe("emergency_refund", () => {
  let env: Env;
  beforeEach(() => { env = new Env(); env.feePayer = env.relayer; });

  it("a random signer can refund from Open once refund_after is reached", async () => {
    const { ref } = await two(env);
    env.setNow(REFUND);
    const stranger = Keypair.generate(); // zero SOL, no relation to the bet
    const r = await env.emergencyRefund(ref, stranger);
    expectOk(r);
    const b = env.bet(ref);
    expect(stateOf(b)).to.equal("refunded");
    expect(b.winningSide).to.equal(255);
    expect(big(b.feePaid)).to.equal(0n);
    const ev = r.events.find((e) => e.name.toLowerCase() === "emergencyrefunded")!;
    expect(ev, "EmergencyRefunded event").to.exist;
    expect(ev.data.previousState).to.equal(0);
    expect(ev.data.caller.toBase58()).to.equal(stranger.publicKey.toBase58());
  });

  it("works from Locked", async () => {
    const { ref } = await two(env);
    expectOk(await env.lock(ref));
    env.setNow(REFUND + 5 * HOUR);
    const r = await env.emergencyRefund(ref);
    expectOk(r);
    expect(stateOf(env.bet(ref))).to.equal("refunded");
    expect(r.events.find((e) => e.name.toLowerCase() === "emergencyrefunded")!.data.previousState).to.equal(1);
  });

  it("works on an empty market", async () => {
    const ref = await env.open();
    env.setNow(REFUND);
    expectOk(await env.emergencyRefund(ref));
  });

  it("rejected before refund_after (RefundTooEarly), also one second before", async () => {
    const { ref } = await two(env);
    expectErr(await env.emergencyRefund(ref), "RefundTooEarly");
    env.setNow(CLOSES + 1);
    expectErr(await env.emergencyRefund(ref), "RefundTooEarly");
    env.setNow(REFUND - 1);
    expectErr(await env.emergencyRefund(ref), "RefundTooEarly");
    expect(stateOf(env.bet(ref))).to.equal("open");
    env.setNow(REFUND);
    expectOk(await env.emergencyRefund(ref));
  });

  it("rejected in Settled and in Refunded (WrongState)", async () => {
    const { ref } = await two(env);
    const { ref: r2 } = await two(env);
    const { ref: r3 } = await two(env); // all markets exist before the clock moves
    expectOk(await env.lock(ref));
    expectOk(await env.settle(ref, 1));
    expectOk(await env.refund(r2));
    env.setNow(REFUND + HOUR);
    expectErr(await env.emergencyRefund(ref), "WrongState");
    expectErr(await env.emergencyRefund(r2), "WrongState");
    expectOk(await env.emergencyRefund(r3));
    expectErr(await env.emergencyRefund(r3), "WrongState"); // not twice
  });

  it("lock and settle are rejected after refund_after (PastRefundDeadline)", async () => {
    const { ref } = await two(env);
    const { ref: l2 } = await two(env);
    const { ref: l3 } = await two(env);
    env.setNow(REFUND - 1);
    expectOk(await env.lock(l3)); // one second before the deadline still works
    expectOk(await env.lock(ref));
    env.setNow(REFUND);
    expectErr(await env.lock(l2), "PastRefundDeadline");
    expectErr(await env.settle(ref, 1), "PastRefundDeadline"); // locked earlier, oracle too late
    expect(stateOf(env.bet(ref))).to.equal("locked");
    expect(stateOf(env.bet(l2))).to.equal("open");
    expectOk(await env.emergencyRefund(ref)); // and the refund goes through
  });

  it("settle one second before the deadline still works", async () => {
    const { ref } = await two(env);
    expectOk(await env.lock(ref));
    env.setNow(REFUND - 1);
    expectOk(await env.settle(ref, 0));
  });

  it("claims after an emergency refund return full stakes, rent goes to bet.payer, vault ends empty", async () => {
    const { ref, users } = await env.market([[0, 0, 10n * USDC + 7n], [1, 1, 20n * USDC], [2, 1, 3n * USDC + 1n]], { min: 1n });
    expectOk(await env.lock(ref)); // Locked, then the oracle disappears
    env.setNow(REFUND + HOUR);
    expectOk(await env.emergencyRefund(ref, Keypair.generate()));
    const stakes = [10n * USDC + 7n, 20n * USDC, 3n * USDC + 1n];
    const payerBefore = env.lamports(env.payer.publicKey);
    let rent = 0n;
    for (let i = 0; i < 3; i++) {
      const before = env.tokenBal(env.ata(users[i].publicKey));
      rent += env.lamports(env.posPda(ref.bet, users[i].publicKey));
      const r = await env.claim(ref, users[i].publicKey, { signer: Keypair.generate() });
      expectOk(r);
      expect(env.tokenBal(env.ata(users[i].publicKey)) - before).to.equal(stakes[i]);
      expect(r.events.find((e) => e.name.toLowerCase() === "claimed")!.data.refund).to.equal(true);
    }
    expect(env.tokenBal(ref.vault)).to.equal(0n);
    expect(env.tokenBal(env.treasuryAta)).to.equal(0n);
    expect(env.lamports(env.payer.publicKey) - payerBefore).to.equal(rent);
    expectErr(await env.claim(ref, users[0].publicKey, { rentReceiver: users[0].publicKey }), "AccountNotInitialized"); // closed: fails before any receiver check
    expectOk(await env.closeBet(ref));
    expect(env.exists(ref.bet)).to.equal(false);
  });

  it("claim with a wrong rent_receiver is still rejected after an emergency refund", async () => {
    const { ref, users } = await two(env);
    env.setNow(REFUND);
    expectOk(await env.emergencyRefund(ref));
    expectErr(await env.claim(ref, users[0].publicKey, { rentReceiver: users[0].publicKey }), "WrongRentReceiver");
  });

  it("a second claim fails with AccountNotInitialized (what the backend maps to ALREADY_CLAIMED)", async () => {
    const { ref, users } = await two(env);
    env.setNow(REFUND);
    expectOk(await env.emergencyRefund(ref));
    expectOk(await env.claim(ref, users[0].publicKey));
    expectErr(await env.claim(ref, users[0].publicKey), "AccountNotInitialized");
  });

  it("deposits stay closed after funding_closes_at, and after the refund", async () => {
    const { ref } = await two(env);
    env.setNow(REFUND);
    expectErr(await env.deposit(ref, env.user(), 0, USDC), "FundingClosed");
    expectOk(await env.emergencyRefund(ref));
    expectErr(await env.deposit(ref, env.user(), 0, USDC), "MarketNotOpen");
  });
});
