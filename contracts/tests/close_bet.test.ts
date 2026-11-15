import { expect } from "chai";
import { Keypair } from "@solana/web3.js";
import { Env, USDC, big, expectErr, expectOk } from "./helpers";

describe("close_bet", () => {
  let env: Env;
  beforeEach(() => { env = new Env(); env.feePayer = env.relayer; });

  const settled = async () => {
    const m = await env.market([[0, 0, 10n * USDC + 1n], [1, 0, 20n * USDC], [2, 1, 20n * USDC], [3, 1, 19n * USDC + 7n]], { min: 1n });
    expectOk(await env.lock(m.ref));
    expectOk(await env.settle(m.ref, 1));
    return m;
  };
  const drain = async (m: { ref: any; users: Keypair[] }) => {
    for (const i of [0, 1]) expectOk(await env.closeLoser(m.ref, m.users[i].publicKey));
    for (const i of [2, 3]) expectOk(await env.claim(m.ref, m.users[i].publicKey));
  };

  it("only after all positions are closed", async () => {
    const m = await settled();
    expectErr(await env.closeBet(m.ref), "PositionsStillOpen");
    for (const i of [0, 1, 2]) {
      if (i < 2) expectOk(await env.closeLoser(m.ref, m.users[i].publicKey));
      else expectOk(await env.claim(m.ref, m.users[i].publicKey));
      expectErr(await env.closeBet(m.ref), "PositionsStillOpen");
    }
    expectOk(await env.claim(m.ref, m.users[3].publicKey));
    expectOk(await env.closeBet(m.ref));
  });

  it("sends dust to the treasury, closes Vault and Bet, returns all rent to the payer", async () => {
    const m = await settled();
    await drain(m);
    const dust = env.tokenBal(m.ref.vault);
    expect(dust > 0n).to.equal(true);
    const treasuryBefore = env.tokenBal(env.treasuryAta);
    const fee = big(env.bet(m.ref).feePaid);
    const rent = env.lamports(m.ref.bet) + env.lamports(m.ref.vault);
    const payerBefore = env.lamports(env.payer.publicKey);
    const r = await env.closeBet(m.ref);
    expectOk(r);
    expect(env.tokenBal(env.treasuryAta) - treasuryBefore).to.equal(dust);
    expect(env.tokenBal(env.treasuryAta)).to.equal(fee + dust);
    expect(env.exists(m.ref.vault)).to.equal(false);
    expect(env.exists(m.ref.bet)).to.equal(false);
    expect(env.lamports(env.payer.publicKey) - payerBefore).to.equal(rent);
    expect(r.events.find((e) => e.name.toLowerCase() === "betclosed")).to.exist;
  });

  it("closed bet is gone: nothing can use it afterwards", async () => {
    const m = await settled();
    await drain(m);
    expectOk(await env.closeBet(m.ref));
    expect((await env.deposit(m.ref, env.user(), 0, USDC)).ok).to.equal(false);
    expect((await env.closeBet(m.ref)).ok).to.equal(false);
  });

  it("works from Refunded after everyone claimed their refund (no dust)", async () => {
    const m = await env.market([[0, 0, 9n * USDC], [1, 1, 4n * USDC]]);
    expectOk(await env.refund(m.ref));
    expectErr(await env.closeBet(m.ref), "PositionsStillOpen");
    for (const i of [0, 1]) expectOk(await env.claim(m.ref, m.users[i].publicKey));
    expectOk(await env.closeBet(m.ref));
    expect(env.tokenBal(env.treasuryAta)).to.equal(0n);
    expect(env.exists(m.ref.bet)).to.equal(false);
  });

  it("an empty refunded bet can be closed right away", async () => {
    const ref = await env.open();
    expectOk(await env.refund(ref));
    expectOk(await env.closeBet(ref));
  });

  it("wrong states: Open and Locked", async () => {
    const ref = await env.open();
    expectErr(await env.closeBet(ref), "WrongState");
    const m = await env.market([[0, 0, 5n * USDC], [1, 1, 5n * USDC]]);
    expectOk(await env.lock(m.ref));
    expectErr(await env.closeBet(m.ref), "WrongState");
  });

  it("non authority, wrong rent receiver and wrong treasury are rejected", async () => {
    const m = await settled();
    await drain(m);
    const stranger = Keypair.generate();
    expectErr(await env.closeBet(m.ref, { signer: stranger }), "Unauthorized");
    expectErr(await env.closeBet(m.ref, { signer: env.payer }), "Unauthorized");
    expectErr(await env.closeBet(m.ref, { payer: env.relayer.publicKey }), "WrongRentReceiver");
    expectErr(await env.closeBet(m.ref, { treasuryToken: env.ata(stranger.publicKey) }), "WrongTreasury");
    expect(env.exists(m.ref.bet)).to.equal(true);
    expectOk(await env.closeBet(m.ref));
  });
});
