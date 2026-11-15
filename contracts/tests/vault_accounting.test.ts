import { expect } from "chai";
import { Env, USDC, big, expectOk, stateOf } from "./helpers";

// Same integer math as backend/markets/pool_math.py and programs/handshake_pool/src/math.rs
const fee = (losing: bigint, bps: bigint) => (losing * bps) / 10_000n;
const payout = (stake: bigint, dist: bigint, win: bigint) => (stake * dist) / win;

describe("payout vectors (end to end through the program)", () => {
  let env: Env;
  beforeEach(() => { env = new Env(); env.feePayer = env.relayer; });

  it("$13 per side: fee 650000, distributable 25350000, winner gets 1.95x", async () => {
    const { ref, users } = await env.market([[0, 0, 13n * USDC], [1, 1, 13n * USDC]]);
    expectOk(await env.lock(ref));
    expectOk(await env.settle(ref, 1));
    const b = env.bet(ref);
    expect(big(b.feePaid)).to.equal(650_000n);
    expect(big(b.distributable)).to.equal(25_350_000n);
    const before = env.tokenBal(env.ata(users[1].publicKey));
    expectOk(await env.claim(ref, users[1].publicKey));
    const got = env.tokenBal(env.ata(users[1].publicKey)) - before;
    expect(got).to.equal(25_350_000n);
    expect(got * 100n / (13n * USDC)).to.equal(195n); // 1.95 times stake
  });

  it("lopsided pool, big side loses: the lone winner takes everything but the fee (300 vs 100)", async () => {
    const o = { max: 200n * USDC };
    const { ref, users } = await env.market([[0, 0, 100n * USDC], [1, 0, 100n * USDC], [2, 0, 100n * USDC], [3, 1, 100n * USDC]], o);
    expectOk(await env.lock(ref));
    expectOk(await env.settle(ref, 1));
    expect(big(env.bet(ref).feePaid)).to.equal(15n * USDC);
    expect(big(env.bet(ref).distributable)).to.equal(385n * USDC);
    const before = env.tokenBal(env.ata(users[3].publicKey));
    expectOk(await env.claim(ref, users[3].publicKey));
    expect(env.tokenBal(env.ata(users[3].publicKey)) - before).to.equal(385n * USDC);
  });

  it("lopsided pool, big side wins: 30 of 300 gets 39.5 (fee 5, distributable 395)", async () => {
    const o = { max: 200n * USDC };
    const { ref, users } = await env.market([[0, 0, 30n * USDC], [1, 0, 70n * USDC], [2, 0, 200n * USDC], [3, 1, 100n * USDC]], o);
    expectOk(await env.lock(ref));
    expectOk(await env.settle(ref, 0));
    expect(big(env.bet(ref).feePaid)).to.equal(5n * USDC);
    expect(big(env.bet(ref).distributable)).to.equal(395n * USDC);
    const before = env.tokenBal(env.ata(users[0].publicKey));
    expectOk(await env.claim(ref, users[0].publicKey));
    expect(env.tokenBal(env.ata(users[0].publicKey)) - before).to.equal(39_500_000n);
  });

  it("rounding dust: awkward base units leave less than one unit per winner in the vault", async () => {
    const o = { min: 1n, max: 100n * USDC };
    const rows: [number, number, bigint][] = [[0, 0, 10_000_001n], [1, 1, 3_333_333n], [2, 1, 3_333_335n]];
    const { ref, users } = await env.market(rows, o);
    expectOk(await env.lock(ref));
    expectOk(await env.settle(ref, 1));
    const f = fee(10_000_001n, 500n);
    const dist = 16_666_669n - f;
    expect(big(env.bet(ref).feePaid)).to.equal(f);
    let paid = 0n;
    for (const [i, , stake] of rows.slice(1)) {
      const before = env.tokenBal(env.ata(users[i].publicKey));
      expectOk(await env.claim(ref, users[i].publicKey));
      const got = env.tokenBal(env.ata(users[i].publicKey)) - before;
      expect(got).to.equal(payout(stake, dist, 6_666_668n));
      expect(got >= stake).to.equal(true);
      paid += got;
    }
    const dust = env.tokenBal(ref.vault);
    expect(dust).to.equal(dist - paid);
    expect(dust < 2n).to.equal(true);
  });
});

describe("vault accounting", () => {
  it("after all claims and loser closes the vault holds only dust and the treasury holds exactly the fee", async () => {
    const env = new Env();
    env.feePayer = env.relayer;
    // side 0: 7 wallets, side 1: 5 wallets, with awkward amounts and some top ups
    const rows: [number, number, bigint][] = [];
    const amts0 = [1_234_567n, 5_000_001n, 7_777_777n, 2_000_003n, 19_999_999n, 3_333_333n, 4_444_449n];
    const amts1 = [6_000_007n, 9_999_991n, 1_000_001n, 12_345_679n, 8_888_889n];
    amts0.forEach((a, i) => rows.push([i, 0, a]));
    amts1.forEach((a, i) => rows.push([100 + i, 1, a]));
    rows.push([0, 0, 1_000_000n], [101, 1, 2_222_223n]); // top ups
    const { ref, users } = await env.market(rows, { min: 1n, max: 25n * USDC, maxP: 20, feeBps: 777 });
    expect(env.bet(ref).participantCount).to.equal(12);
    expectOk(await env.lock(ref));
    const t = env.bet(ref).sideTotals.map(big);
    const total = t[0] + t[1];
    expect(env.tokenBal(ref.vault)).to.equal(total);
    expectOk(await env.settle(ref, 1));
    const f = fee(t[0], 777n);
    const dist = total - f;
    expect(env.tokenBal(env.treasuryAta)).to.equal(f);
    let paid = 0n;
    const winners = Object.keys(users).map(Number).filter((k) => k >= 100);
    const losers = Object.keys(users).map(Number).filter((k) => k < 100);
    for (const k of winners) {
      const stake = big(env.pos(ref, users[k].publicKey).amount);
      const before = env.tokenBal(env.ata(users[k].publicKey));
      expectOk(await env.claim(ref, users[k].publicKey));
      const got = env.tokenBal(env.ata(users[k].publicKey)) - before;
      expect(got).to.equal(payout(stake, dist, t[1]));
      expect(got >= stake).to.equal(true);
      paid += got;
    }
    for (const k of losers) expectOk(await env.closeLoser(ref, users[k].publicKey));
    const dust = env.tokenBal(ref.vault);
    expect(dust).to.equal(dist - paid);
    expect(dust < BigInt(winners.length)).to.equal(true);
    expect(env.tokenBal(env.treasuryAta)).to.equal(big(env.bet(ref).feePaid));
    expect(env.bet(ref).positionsOpen).to.equal(0);
    expect(stateOf(env.bet(ref))).to.equal("settled");
  });
});
