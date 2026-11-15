import { expect } from "chai";
import { Keypair, SystemProgram } from "@solana/web3.js";
import { getAssociatedTokenAddressSync } from "@solana/spl-token";
import { Env, USDC, big, expectErr, expectOk, stateOf } from "./helpers";

const evOf = (r: any, name: string) => r.events.find((e: any) => e.name.toLowerCase() === name.toLowerCase());

describe("claim", () => {
  let env: Env;
  beforeEach(() => { env = new Env(); env.feePayer = env.relayer; });
  // side 0: u0 10, u1 20 (pool 30). side 1: u2 20, u3 19 (pool 39).
  const settled = async (win: number, o = {}) => {
    const m = await env.market([[0, 0, 10n * USDC], [1, 0, 20n * USDC], [2, 1, 20n * USDC], [3, 1, 19n * USDC]], o);
    expectOk(await env.lock(m.ref));
    expectOk(await env.settle(m.ref, win));
    return m;
  };
  const ata = (u: Keypair) => getAssociatedTokenAddressSync(env.mint, u.publicKey);

  it("winner payout matches the mock vectors (side 1 wins: 34_615_384 / 32_884_615)", async () => {
    const { ref, users } = await settled(1);
    const b = env.bet(ref);
    expect(big(b.feePaid)).to.equal(1_500_000n);
    expect(big(b.distributable)).to.equal(67_500_000n);
    const r = await env.claim(ref, users[2].publicKey);
    expectOk(r);
    expect(env.tokenBal(ata(users[2]))).to.equal(1000n * USDC - 20n * USDC + 34_615_384n);
    const c = evOf(r, "Claimed").data;
    expect(big(c.payout)).to.equal(34_615_384n);
    expect(c.refund).to.equal(false);
    expectOk(await env.claim(ref, users[3].publicKey));
    expect(env.tokenBal(ata(users[3]))).to.equal(1000n * USDC - 19n * USDC + 32_884_615n);
    expect(env.tokenBal(ref.vault)).to.equal(1n); // one base unit of dust
  });

  it("side 0 winning: 22_350_000 for the 10 stake", async () => {
    const { ref, users } = await settled(0);
    expectOk(await env.claim(ref, users[0].publicKey));
    expect(env.tokenBal(ata(users[0]))).to.equal(1000n * USDC - 10n * USDC + 22_350_000n);
  });

  it("refund pays exactly the stake (from Open and from Locked refunds)", async () => {
    const m = await env.market([[0, 0, 9n * USDC], [1, 1, 4n * USDC]]);
    expectOk(await env.refund(m.ref));
    const r = await env.claim(m.ref, m.users[0].publicKey);
    expectOk(r);
    expect(evOf(r, "Claimed").data.refund).to.equal(true);
    expectOk(await env.claim(m.ref, m.users[1].publicKey));
    expect(env.tokenBal(ata(m.users[0]))).to.equal(1000n * USDC);
    expect(env.tokenBal(ata(m.users[1]))).to.equal(1000n * USDC);
    expect(env.tokenBal(m.ref.vault)).to.equal(0n);
    const m2 = await env.market([[0, 0, 9n * USDC], [1, 1, 4n * USDC]]);
    expectOk(await env.lock(m2.ref));
    expectOk(await env.refund(m2.ref));
    expectOk(await env.claim(m2.ref, m2.users[0].publicKey));
    expect(env.tokenBal(ata(m2.users[0]))).to.equal(1000n * USDC);
  });

  it("a winner never gets less than the stake", async () => {
    const { ref, users } = await settled(0); // the big side wins against a smaller losing side
    for (const i of [0, 1]) {
      const stake = i === 0 ? 10n * USDC : 20n * USDC;
      const before = env.tokenBal(ata(users[i]));
      expectOk(await env.claim(ref, users[i].publicKey));
      expect(env.tokenBal(ata(users[i])) - before >= stake).to.equal(true);
    }
  });

  it("loser is rejected with NotAWinner and keeps the position", async () => {
    const { ref, users } = await settled(1);
    expectErr(await env.claim(ref, users[0].publicKey), "NotAWinner");
    expect(env.pos(ref, users[0].publicKey)).to.not.equal(null);
  });

  it("claiming before settlement is WrongState (Open and Locked)", async () => {
    const m = await env.market([[0, 0, 5n * USDC], [1, 1, 5n * USDC]]);
    expectErr(await env.claim(m.ref, m.users[0].publicKey), "WrongState");
    expectOk(await env.lock(m.ref));
    expectErr(await env.claim(m.ref, m.users[0].publicKey), "WrongState");
  });

  it("double claim is impossible: the position is closed", async () => {
    const { ref, users } = await settled(1);
    expectOk(await env.claim(ref, users[2].publicKey));
    expect(env.pos(ref, users[2].publicKey)).to.equal(null);
    expectErr(await env.claim(ref, users[2].publicKey), "AccountNotInitialized");
  });

  it("a closed position cannot be revived in the same transaction", async () => {
    const { ref, users } = await settled(1);
    const pos = env.posPda(ref.bet, users[2].publicKey);
    const c1 = await env.claimIx(ref, users[2].publicKey);
    const refill = SystemProgram.transfer({ fromPubkey: env.relayer.publicKey, toPubkey: pos, lamports: 5_000_000 });
    const c2 = await env.claimIx(ref, users[2].publicKey);
    const r = env.send([c1, refill, c2], [env.anyone]);
    expect(r.ok).to.equal(false);
    expect(env.pos(ref, users[2].publicKey)).to.not.equal(null); // the whole transaction rolled back
    // a deposit right after a claim in one transaction is rejected too (market is no longer Open)
    const d = await env.depositIx(ref, users[2], 1, USDC);
    const r2 = env.send([c1, d], [env.anyone, users[2], env.payer]);
    expectErr(r2, "MarketNotOpen");
  });

  it("destination must belong to the position owner and use the bet mint", async () => {
    const { ref, users } = await settled(1);
    const stranger = env.user(1n);
    expectErr(await env.claim(ref, users[2].publicKey, { dest: ata(stranger) }), "WrongDestination");
    const other = Keypair.generate();
    env.makeMint(other, 6);
    const wrongMint = env.ata(users[2].publicKey, other.publicKey);
    expectErr(await env.claim(ref, users[2].publicKey, { dest: wrongMint }), "WrongDestination");
    expectErr(await env.claim(ref, users[2].publicKey, { dest: env.treasuryAta }), "WrongDestination");
    expectErr(await env.claim(ref, users[2].publicKey, { dest: ref.vault }), "ConstraintDuplicateMutableAccount");
    expect(env.pos(ref, users[2].publicKey)).to.not.equal(null);
  });

  it("rent_receiver must be bet.payer", async () => {
    const { ref, users } = await settled(1);
    expectErr(await env.claim(ref, users[2].publicKey, { rentReceiver: users[2].publicKey }), "WrongRentReceiver");
    expectErr(await env.claim(ref, users[2].publicKey, { rentReceiver: env.relayer.publicKey }), "WrongRentReceiver");
  });

  it("anyone can submit a claim and funds still go to the owner; the sponsor gets the position rent", async () => {
    const { ref, users } = await settled(1);
    const rentLamports = env.lamports(env.posPda(ref.bet, users[2].publicKey));
    const payerBefore = env.lamports(env.payer.publicKey);
    const stranger = Keypair.generate(); // zero SOL, not the owner, not the sponsor
    expect(env.lamports(stranger.publicKey)).to.equal(0n);
    const r = await env.claim(ref, users[2].publicKey, { signer: stranger });
    expectOk(r);
    expect(env.lamports(env.payer.publicKey) - payerBefore).to.equal(rentLamports);
    expect(env.lamports(users[2].publicKey)).to.equal(0n);
    expect(env.exists(env.posPda(ref.bet, users[2].publicKey))).to.equal(false);
    expect(env.bet(ref).positionsOpen).to.equal(3);
  });
});

describe("close_loser_position", () => {
  let env: Env;
  beforeEach(() => { env = new Env(); env.feePayer = env.relayer; });
  const settled = async (win: number) => {
    const m = await env.market([[0, 0, 10n * USDC], [1, 0, 20n * USDC], [2, 1, 20n * USDC], [3, 1, 19n * USDC]]);
    expectOk(await env.lock(m.ref));
    expectOk(await env.settle(m.ref, win));
    return m;
  };

  it("closes a loser's position, returns rent to the payer, moves no tokens", async () => {
    const { ref, users } = await settled(1);
    const rent = env.lamports(env.posPda(ref.bet, users[0].publicKey));
    const before = env.lamports(env.payer.publicKey);
    const vault = env.tokenBal(ref.vault);
    const r = await env.closeLoser(ref, users[0].publicKey);
    expectOk(r);
    expect(env.lamports(env.payer.publicKey) - before).to.equal(rent);
    expect(env.tokenBal(ref.vault)).to.equal(vault);
    expect(env.pos(ref, users[0].publicKey)).to.equal(null);
    expect(env.bet(ref).positionsOpen).to.equal(3);
    expect(evOf(r, "LoserClosed")).to.exist;
  });
  it("a winner is rejected with WrongState", async () => {
    const { ref, users } = await settled(1);
    expectErr(await env.closeLoser(ref, users[2].publicKey), "WrongState");
    expect(env.pos(ref, users[2].publicKey)).to.not.equal(null);
  });
  it("wrong states are rejected: Open, Locked, Refunded", async () => {
    const m = await env.market([[0, 0, 5n * USDC], [1, 1, 5n * USDC]]);
    expectErr(await env.closeLoser(m.ref, m.users[0].publicKey), "WrongState");
    expectOk(await env.lock(m.ref));
    expectErr(await env.closeLoser(m.ref, m.users[0].publicKey), "WrongState");
    expectOk(await env.refund(m.ref));
    expectErr(await env.closeLoser(m.ref, m.users[0].publicKey), "WrongState");
  });
  it("rent_receiver must be bet.payer and a second close fails", async () => {
    const { ref, users } = await settled(1);
    expectErr(await env.closeLoser(ref, users[0].publicKey, { rentReceiver: users[0].publicKey }), "WrongRentReceiver");
    expectOk(await env.closeLoser(ref, users[0].publicKey));
    expectErr(await env.closeLoser(ref, users[0].publicKey), "AccountNotInitialized");
  });
  it("a closed loser cannot claim", async () => {
    const { ref, users } = await settled(1);
    expectOk(await env.closeLoser(ref, users[0].publicKey));
    expectErr(await env.claim(ref, users[0].publicKey), "AccountNotInitialized");
  });
});
