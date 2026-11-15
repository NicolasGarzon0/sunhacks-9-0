import * as ed from "@noble/ed25519";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { base58Encode, fromBase64, toBase64 } from "../src/lib/format";
import { PrivyWalletProvider, type PrivyBridge } from "../src/wallet/PrivyWalletProvider";
import { WalletError } from "../src/wallet";

const FEE_PAYER = new Uint8Array(32).fill(7);
const PROGRAM = new Uint8Array(32).fill(9);
const BLOCKHASH = new Uint8Array(32).fill(5);

/** A legacy transaction like the service builds: 2 signature slots (payer, user) and a message with no instructions. */
function build(user: Uint8Array, blockhash = BLOCKHASH) {
  const message = new Uint8Array([2, 0, 1, 3, ...FEE_PAYER, ...user, ...PROGRAM, ...blockhash, 0]);
  const wire = new Uint8Array([2, ...new Uint8Array(128), ...message]);
  return { message, wire };
}
function withSig(wire: Uint8Array, slot: number, sig: Uint8Array) {
  const out = wire.slice();
  out.set(sig, 1 + slot * 64);
  return out;
}

let seed: Uint8Array;
let pub: Uint8Array;
let tx: ReturnType<typeof build>;
let state: { authenticated: boolean; address: string | null; sdkReady: boolean };
let bridge: PrivyBridge;
let sign: Mock<(wire: Uint8Array) => Promise<Uint8Array>>;
let login: Mock<() => void>;
let logout: Mock<() => Promise<void>>;
let provider: PrivyWalletProvider;

function push() { provider.attach({ ...bridge, ...state }); }

beforeEach(async () => {
  seed = ed.utils.randomSecretKey();
  pub = await ed.getPublicKeyAsync(seed);
  tx = build(pub);
  state = { authenticated: true, address: base58Encode(pub), sdkReady: true };
  sign = vi.fn<(wire: Uint8Array) => Promise<Uint8Array>>(async (wire) => withSig(wire, 1, await ed.signAsync(tx.message, seed)));
  login = vi.fn<() => void>();
  logout = vi.fn<() => Promise<void>>(async () => { state = { ...state, authenticated: false, address: null }; push(); });
  bridge = { ...state, login: () => login(), logout: () => logout(), signTransaction: (t) => sign(t) };
  provider = new PrivyWalletProvider();
  push();
});

const b64 = (u: Uint8Array) => toBase64(u);

describe("PrivyWalletProvider", () => {
  it("exposes address and ready from the embedded wallet, and is not a test wallet", () => {
    expect(provider.address).toBe(base58Encode(pub));
    expect(provider.ready).toBe(true);
    expect(provider.isTest).toBe(false);
    expect(provider.requiresLogin).toBe(true);
  });

  it("is not ready before login", () => {
    state = { ...state, authenticated: false, address: null };
    push();
    expect(provider.address).toBeNull();
    expect(provider.ready).toBe(false);
  });

  it("signs the exact message and returns a valid user signature, keeping the message unchanged", async () => {
    const out = await provider.signDeposit(b64(tx.wire), b64(tx.message));
    expect(out).toHaveProperty("user_signature_b64");
    if (!("user_signature_b64" in out)) throw new Error("unexpected shape");
    expect(out.message_b64).toBe(b64(tx.message));
    expect(fromBase64(out.message_b64)).toEqual(tx.message);
    expect(await ed.verifyAsync(fromBase64(out.user_signature_b64), tx.message, pub)).toBe(true);
    // the wallet got the unsigned transaction untouched and was asked to sign only
    expect(sign).toHaveBeenCalledTimes(1);
    expect(sign.mock.calls[0][0]).toEqual(tx.wire);
  });

  it("refuses a signed transaction whose message was changed (blockhash)", async () => {
    sign.mockImplementation(async () => {
      const other = build(pub, new Uint8Array(32).fill(6));
      return withSig(other.wire, 1, await ed.signAsync(other.message, seed));
    });
    await expect(provider.signDeposit(b64(tx.wire), b64(tx.message))).rejects.toThrow(/changed the transaction/);
  });

  it("refuses a signed transaction whose fee payer or instructions were changed", async () => {
    sign.mockImplementation(async (wire: Uint8Array) => {
      const w = withSig(wire, 1, await ed.signAsync(tx.message, seed));
      w[1 + 128 + 4] ^= 0xff; // first byte of the fee payer key inside the message
      return w;
    });
    await expect(provider.signDeposit(b64(tx.wire), b64(tx.message))).rejects.toBeInstanceOf(WalletError);
    sign.mockImplementation(async (wire: Uint8Array) => {
      const w = new Uint8Array([...withSig(wire, 1, await ed.signAsync(tx.message, seed)), 1, 2, 3]); // trailing bytes
      return w;
    });
    await expect(provider.signDeposit(b64(tx.wire), b64(tx.message))).rejects.toThrow(/changed the transaction/);
  });

  it("refuses a signature that does not verify, or a missing one", async () => {
    sign.mockImplementation(async (wire: Uint8Array) => withSig(wire, 1, new Uint8Array(64).fill(1)));
    await expect(provider.signDeposit(b64(tx.wire), b64(tx.message))).rejects.toThrow(/does not match/);
    sign.mockImplementation(async (wire: Uint8Array) => wire);
    await expect(provider.signDeposit(b64(tx.wire), b64(tx.message))).rejects.toThrow(/does not match/);
  });

  it("refuses to sign when the service transaction does not match its own message, without asking the wallet", async () => {
    await expect(provider.signDeposit(b64(tx.wire), b64(new Uint8Array([1, 2, 3, 4])))).rejects.toThrow(/does not match the message/);
    expect(sign).not.toHaveBeenCalled();
  });

  it("maps a user rejection to WALLET_REJECTED and a network failure to WALLET_NETWORK", async () => {
    sign.mockRejectedValueOnce(new Error("User rejected the request"));
    await expect(provider.signDeposit(b64(tx.wire), b64(tx.message))).rejects.toMatchObject({ code: "WALLET_REJECTED" });
    sign.mockRejectedValueOnce(new Error("Network request failed"));
    await expect(provider.signDeposit(b64(tx.wire), b64(tx.message))).rejects.toMatchObject({ code: "WALLET_NETWORK" });
  });

  it("cannot sign when logged out", async () => {
    state = { ...state, authenticated: false, address: null };
    push();
    await expect(provider.signDeposit(b64(tx.wire), b64(tx.message))).rejects.toMatchObject({ code: "WALLET" });
    expect(sign).not.toHaveBeenCalled();
  });

  describe("login", () => {
    beforeEach(() => { state = { ...state, authenticated: false, address: null }; push(); });

    it("opens the login screen once and resolves when the wallet address arrives", async () => {
      const p1 = provider.connect();
      const p2 = provider.connect();
      expect(login).toHaveBeenCalledTimes(1);
      state = { ...state, authenticated: true, address: null }; // logged in, wallet still being created
      push();
      state = { ...state, address: base58Encode(pub) };
      push();
      await expect(Promise.all([p1, p2])).resolves.toBeDefined();
      expect(provider.address).toBe(base58Encode(pub));
    });

    it("waits for the SDK before opening the login screen", async () => {
      state = { ...state, sdkReady: false };
      push();
      const p = provider.connect();
      expect(login).not.toHaveBeenCalled();
      state = { ...state, sdkReady: true };
      push();
      expect(login).toHaveBeenCalledTimes(1);
      provider.onLoginError("exited_auth_flow");
      await expect(p).rejects.toMatchObject({ code: "WALLET_LOGIN_CANCELLED" });
    });

    it("maps a closed login screen to WALLET_LOGIN_CANCELLED and a network error to WALLET_NETWORK, and can retry", async () => {
      const p = provider.connect();
      provider.onLoginError("exited_auth_flow");
      await expect(p).rejects.toMatchObject({ code: "WALLET_LOGIN_CANCELLED" });
      const p2 = provider.connect();
      expect(login).toHaveBeenCalledTimes(2);
      provider.onLoginError(new Error("Failed to fetch: network error"));
      await expect(p2).rejects.toMatchObject({ code: "WALLET_NETWORK" });
    });

    it("gives up if the wallet is never created", async () => {
      vi.useFakeTimers();
      try {
        const p = provider.connect();
        const assertion = expect(p).rejects.toMatchObject({ code: "WALLET_NETWORK" });
        state = { ...state, authenticated: true, address: null };
        push();
        await vi.advanceTimersByTimeAsync(21_000);
        await assertion;
      } finally { vi.useRealTimers(); }
    });
  });

  it("logs out and notifies subscribers", async () => {
    const seen = vi.fn();
    provider.subscribe(seen);
    await provider.disconnect();
    expect(logout).toHaveBeenCalledTimes(1);
    expect(provider.address).toBeNull();
    expect(seen).toHaveBeenCalled();
  });
});
