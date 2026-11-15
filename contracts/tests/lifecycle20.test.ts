import { expect } from "chai";
import { Keypair } from "@solana/web3.js";
import { Env, USDC, big, expectOk, stateOf } from "./helpers";

const fee = (losing: bigint, bps: bigint) => (losing * bps) / 10_000n;
const payout = (stake: bigint, dist: bigint, win: bigint) => (stake * dist) / win;

describe("full lifecycle with 20 participants", () => {
  for (const winner of [0, 1]) {
    it(`create, 20 deposits (with top ups), lock, settle side ${winner}, claims, loser closes, close_bet`, async () => {
      const env = new Env();
      env.feePayer = env.relayer; // the sponsor's lamports now move only by rent
      const users: Keypair[] = Array.from({ length: 20 }, () => env.user(500n)); // all with zero SOL
      const extra = env.user(); // created before the snapshot: ATA rent is not part of the pool's rent flow
      const payerStart = env.lamports(env.payer.publicKey);
      const ref = await env.open({ maxP: 20, min: 1n * USDC, max: 50n * USDC, feeBps: 500 });

      const stake = users.map((_, i) => BigInt(1 + ((i * 7) % 13)) * USDC + BigInt(i * 1_011)); // varied, awkward amounts
      for (let i = 0; i < 20; i++) expectOk(await env.deposit(ref, users[i], i % 2, stake[i]));
      for (const i of [0, 3, 6, 9, 12, 15]) { // top ups on the same side
        expectOk(await env.deposit(ref, users[i], i % 2, 2n * USDC));
        stake[i] += 2n * USDC;
      }
      const b = env.bet(ref);
      expect(b.participantCount).to.equal(20);
      expect(b.positionsOpen).to.equal(20);
      const totals = [0n, 0n];
      stake.forEach((s, i) => (totals[i % 2] += s));
      expect(b.sideTotals.map(big)).to.deep.equal(totals);
      expect(env.tokenBal(ref.vault)).to.equal(totals[0] + totals[1]);
      expect((await env.deposit(ref, extra, 0, USDC)).code).to.equal("MarketFull");

      expectOk(await env.lock(ref));
      expectOk(await env.settle(ref, winner));
      const f = fee(totals[1 - winner], 500n);
      const dist = totals[0] + totals[1] - f;
      expect(env.tokenBal(env.treasuryAta)).to.equal(f);

      let paid = 0n;
      for (let i = 0; i < 20; i++) {
        if (i % 2 === winner) {
          const before = env.tokenBal(env.ata(users[i].publicKey));
          expectOk(await env.claim(ref, users[i].publicKey, { signer: Keypair.generate() })); // a different stranger each time
          const got = env.tokenBal(env.ata(users[i].publicKey)) - before;
          expect(got).to.equal(payout(stake[i], dist, totals[winner]));
          expect(got >= stake[i]).to.equal(true);
          paid += got;
        } else {
          expectOk(await env.closeLoser(ref, users[i].publicKey));
        }
      }
      expect(env.bet(ref).positionsOpen).to.equal(0);
      const dust = env.tokenBal(ref.vault);
      expect(dust).to.equal(dist - paid);
      expect(dust < 10n).to.equal(true);
      expect(env.tokenBal(env.treasuryAta)).to.equal(f); // exactly the fee, dust not yet swept
      expect(stateOf(env.bet(ref))).to.equal("settled");

      expectOk(await env.closeBet(ref));
      expect(env.tokenBal(env.treasuryAta)).to.equal(f + dust);
      expect(env.exists(ref.bet) || env.exists(ref.vault)).to.equal(false);
      for (const u of users) expect(env.pos(ref, u.publicKey)).to.equal(null);
      // every lamport of rent came back to the sponsor, and the users never held SOL
      expect(env.lamports(env.payer.publicKey)).to.equal(payerStart);
      for (const u of users) expect(env.lamports(u.publicKey)).to.equal(0n);
    });
  }
});
