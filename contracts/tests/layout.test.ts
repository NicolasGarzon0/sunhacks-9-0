import { expect } from "chai";
import { SystemProgram } from "@solana/web3.js";
import { Env, IDL, PROGRAM_ID, USDC, expectOk } from "./helpers";

describe("account layout and getProgramAccounts filter", () => {
  it("Position is 82 bytes: disc 0..8, bet 8..40, owner 40..72, side 72, amount 73..81, bump 81", async () => {
    const env = new Env();
    const { ref, users } = await env.market([[0, 0, 5n * USDC], [1, 1, 6n * USDC]]);
    const a = env.svm.getAccount(env.posPda(ref.bet, users[1].publicKey).toBase58() as any);
    const d = Buffer.from(a.data);
    expect(d.length).to.equal(82);
    const disc = IDL.accounts.find((x: any) => x.name === "Position").discriminator;
    expect([...d.subarray(0, 8)]).to.deep.equal(disc);
    expect(d.subarray(8, 40).equals(ref.bet.toBuffer())).to.equal(true);
    expect(d.subarray(40, 72).equals(users[1].publicKey.toBuffer())).to.equal(true);
    expect(d[72]).to.equal(1);
    expect(d.readBigUInt64LE(73)).to.equal(6n * USDC);
    expect(d[81]).to.equal(env.pos(ref, users[1].publicKey).bump);
  });

  it("memcmp(offset 8 = bet) + dataSize 82 lists exactly the positions of one bet", async () => {
    const env = new Env();
    const m1 = await env.market([[0, 0, 5n * USDC], [1, 1, 5n * USDC], [2, 1, 5n * USDC]]);
    const m2 = await env.market([[0, 0, 5n * USDC], [1, 1, 5n * USDC]]);
    const posDisc = Buffer.from(IDL.accounts.find((x: any) => x.name === "Position").discriminator);
    const mine = (bet: Buffer) =>
      env.svm.getProgramAccounts(PROGRAM_ID.toBase58() as any).filter((x: any) => {
        const d = Buffer.from(x.data);
        return d.length === 82 && d.subarray(0, 8).equals(posDisc) && d.subarray(8, 40).equals(bet);
      });
    expect(mine(m1.ref.bet.toBuffer()).length).to.equal(3);
    expect(mine(m2.ref.bet.toBuffer()).length).to.equal(2);
  });
});

describe("griefing: pre-funded PDAs", () => {
  it("lamports sent to a Position, Bet or Vault address in advance do not block creating or depositing", async () => {
    const env = new Env();
    const id = env.newBetId();
    const bet = env.betPda(id);
    const vault = env.vaultPda(bet);
    const u = env.user();
    const pos = env.posPda(bet, u.publicKey);
    for (const a of [bet, vault, pos]) {
      expectOk(env.send([SystemProgram.transfer({ fromPubkey: env.payer.publicKey, toPubkey: a, lamports: 1_000_000 })], [env.payer]));
    }
    const ref = await env.open({ betId: id });
    expectOk(await env.deposit(ref, u, 0, 3n * USDC));
    expect(env.tokenBal(ref.vault)).to.equal(3n * USDC);
  });

  it("the sponsor can also be the depositor", async () => {
    const env = new Env();
    const ref = await env.open();
    env.fund(env.payer.publicKey, 50n);
    expectOk(await env.deposit(ref, env.payer, 1, 4n * USDC));
    expect(env.tokenBal(ref.vault)).to.equal(4n * USDC);
  });
});
