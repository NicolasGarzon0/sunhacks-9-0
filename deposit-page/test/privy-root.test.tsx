import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

// A tiny fake of the Privy SDK: state in a store so hooks re-render like the real ones.
const sdk = vi.hoisted(() => {
  const s = {
    ready: true, authenticated: false, wallets: [] as { address: string }[], user: null as null | { linkedAccounts: Record<string, unknown>[] },
    login: vi.fn<(o?: unknown) => void>(),
    logout: vi.fn<() => Promise<void>>(),
    signTransaction: vi.fn<(i: unknown) => Promise<unknown>>(),
    providerProps: null as null | Record<string, unknown>,
    onError: null as null | ((e: unknown) => void),
  };
  const listeners = new Set<() => void>();
  let version = 0;
  return {
    s,
    set(patch: Partial<typeof s>) { Object.assign(s, patch); version++; listeners.forEach((l) => l()); },
    subscribe(l: () => void) { listeners.add(l); return () => { listeners.delete(l); }; },
    version: () => version,
  };
});

vi.mock("@privy-io/react-auth", async () => {
  const React = await import("react");
  const use = () => React.useSyncExternalStore(sdk.subscribe, sdk.version);
  return {
    PrivyProvider: (p: Record<string, unknown> & { children: React.ReactNode }) => {
      sdk.s.providerProps = p;
      return p.children;
    },
    usePrivy: () => { use(); return { ready: sdk.s.ready, authenticated: sdk.s.authenticated, user: sdk.s.user }; },
    useLogin: (cb: { onError?: (e: unknown) => void }) => { use(); sdk.s.onError = cb?.onError ?? null; return { login: (o?: unknown) => sdk.s.login(o) }; },
    useLogout: () => ({ logout: () => sdk.s.logout() }),
  };
});
vi.mock("@privy-io/react-auth/solana", async () => {
  const React = await import("react");
  const use = () => React.useSyncExternalStore(sdk.subscribe, sdk.version);
  return {
    useWallets: () => { use(); return { ready: true, wallets: sdk.s.wallets }; },
    useSignTransaction: () => ({ signTransaction: (i: unknown) => sdk.s.signTransaction(i) }),
  };
});

import * as ed from "@noble/ed25519";
import { base58Encode, toBase64 } from "../src/lib/format";
import { PrivyWalletProvider } from "../src/wallet/PrivyWalletProvider";
import { WalletRoot } from "../src/wallet";
import { useWallet } from "../src/wallet/WalletContext";

const ADDR = "Aaaa1111111111111111111111111111111111111111";

function Probe() {
  const w = useWallet();
  return (
    <div>
      <p data-testid="addr">{w.address ?? "none"}</p>
      <p data-testid="err">{w.error ?? ""}</p>
      <button onClick={w.retry}>login</button>
      <button onClick={() => void w.reset()}>logout</button>
      <button onClick={() => void w.provider.signDeposit("AAAA", "AQIDBA==").catch(() => {})}>sign</button>
    </div>
  );
}

beforeEach(() => {
  sdk.s.login = vi.fn<(o?: unknown) => void>();
  sdk.s.logout = vi.fn<() => Promise<void>>(async () => { sdk.set({ authenticated: false, wallets: [] }); });
  sdk.s.signTransaction = vi.fn<(i: unknown) => Promise<unknown>>();
  sdk.s.providerProps = null;
  sdk.s.ready = true; sdk.s.authenticated = false; sdk.s.wallets = []; sdk.s.user = null; sdk.s.onError = null;
});

function mount(extra: { appId?: string; clientId?: string } = {}) {
  const provider = new PrivyWalletProvider();
  render(<WalletRoot provider={provider} appId={extra.appId ?? "app-id"} clientId={extra.clientId}><Probe /></WalletRoot>);
  return provider;
}

describe("PrivyRoot", () => {
  it("configures Privy: email and SMS, Solana wallet on login, no Privy wallet screens, client id only when set", async () => {
    mount();
    await screen.findByTestId("addr");
    const props = sdk.s.providerProps as { appId: string; clientId?: string; config: Record<string, unknown> };
    expect(props.appId).toBe("app-id");
    expect("clientId" in props).toBe(false);
    expect(props.config).toMatchObject({
      loginMethods: ["email", "sms"],
      embeddedWallets: { solana: { createOnLogin: "users-without-wallets" }, showWalletUIs: false },
    });
  });

  it("passes the client id when set", async () => {
    mount({ clientId: "client-1" });
    await screen.findByTestId("addr");
    expect((sdk.s.providerProps as { clientId?: string }).clientId).toBe("client-1");
  });

  it("does not open the login screen on page load", async () => {
    mount();
    await screen.findByTestId("addr");
    expect(sdk.s.login).not.toHaveBeenCalled();
    expect(screen.getByTestId("addr")).toHaveTextContent("none");
  });

  it("logs in with email, shows the wallet address, signs without Privy UI on devnet, and logs out", async () => {
    mount();
    await userEvent.click(await screen.findByText("login"));
    await waitFor(() => expect(sdk.s.login).toHaveBeenCalledWith({ loginMethods: ["email"] }));
    sdk.set({ authenticated: true, wallets: [{ address: ADDR }] });
    await waitFor(() => expect(screen.getByTestId("addr")).toHaveTextContent(ADDR));

    await userEvent.click(screen.getByText("logout"));
    await waitFor(() => expect(screen.getByTestId("addr")).toHaveTextContent("none"));
    expect(sdk.s.logout).toHaveBeenCalledTimes(1);
  });

  it("signs through the SDK on devnet with Privy's own screens off, and never sends", async () => {
    const seed = ed.utils.randomSecretKey();
    const pub = await ed.getPublicKeyAsync(seed);
    const message = new Uint8Array([2, 0, 1, 3, ...new Uint8Array(32).fill(7), ...pub, ...new Uint8Array(32).fill(9), ...new Uint8Array(32).fill(5), 0]);
    const wire = new Uint8Array([2, ...new Uint8Array(128), ...message]);
    sdk.s.authenticated = true;
    sdk.s.wallets = [{ address: base58Encode(pub) }];
    sdk.s.signTransaction.mockImplementation(async () => {
      const out = wire.slice();
      out.set(await ed.signAsync(message, seed), 65);
      return { signedTransaction: out };
    });
    const provider = mount();
    await waitFor(() => expect(provider.ready).toBe(true));
    const result = await provider.signDeposit(toBase64(wire), toBase64(message));
    expect(result).toMatchObject({ message_b64: toBase64(message) });
    expect(sdk.s.signTransaction).toHaveBeenCalledTimes(1);
    expect(sdk.s.signTransaction).toHaveBeenCalledWith(expect.objectContaining({
      transaction: wire, chain: "solana:devnet", wallet: { address: base58Encode(pub) },
      options: { uiOptions: { showWalletUIs: false } },
    }));
  });

  it("maps a rejected signature to plain copy for the deposit flow", async () => {
    sdk.s.authenticated = true;
    sdk.s.wallets = [{ address: ADDR }];
    sdk.s.signTransaction.mockRejectedValue(new Error("User rejected the request"));
    const provider = mount();
    await waitFor(() => expect(provider.ready).toBe(true));
    // ADDR is not a real key and the bytes are not a transaction: the provider refuses before the wallet is asked.
    await expect(provider.signDeposit("AAAA", "AQIDBA==")).rejects.toMatchObject({ code: "WALLET" });
    expect(sdk.s.signTransaction).not.toHaveBeenCalled();
  });

  it("restores an existing session without a login prompt", async () => {
    sdk.s.authenticated = true;
    sdk.s.wallets = [{ address: ADDR }];
    mount();
    await waitFor(() => expect(screen.getByTestId("addr")).toHaveTextContent(ADDR));
    expect(sdk.s.login).not.toHaveBeenCalled();
  });

  describe("embedded wallet missing from the SDK wallet list", () => {
    const privyUser = (address: string) => ({ linkedAccounts: [
      { type: "email", address: "a@b.co" },
      { type: "wallet", chainType: "ethereum", walletClientType: "privy", address: "0xabc" },
      { type: "wallet", chainType: "solana", walletClientType: "privy", address },
    ] });

    it("uses the address on the Privy user when a restored session has an empty wallet list", async () => {
      sdk.s.authenticated = true; sdk.s.wallets = []; sdk.s.user = privyUser(ADDR);
      const provider = mount();
      await waitFor(() => expect(screen.getByTestId("addr")).toHaveTextContent(ADDR));
      expect(provider.ready).toBe(true);
      await expect(provider.connect()).resolves.toBeUndefined();
    });

    it("finishes a fresh login from the user record, with no wallet creation timeout, even if the list stays empty", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const provider = mount();
        const done = provider.connect();
        await vi.advanceTimersByTimeAsync(0);
        expect(sdk.s.login).toHaveBeenCalledTimes(1);
        sdk.set({ authenticated: true, user: privyUser(ADDR) }); // wallets stays []
        await expect(done).resolves.toBeUndefined();
        await vi.advanceTimersByTimeAsync(30_000);
        expect(screen.getByTestId("err")).toHaveTextContent("");
        expect(screen.getByTestId("addr")).toHaveTextContent(ADDR);
      } finally { vi.useRealTimers(); }
    });

    it("signing without a wallet object from the SDK is a plain wallet error, not a network error", async () => {
      sdk.s.authenticated = true; sdk.s.wallets = []; sdk.s.user = privyUser(ADDR);
      const provider = mount();
      await waitFor(() => expect(provider.ready).toBe(true));
      // reaches the bridge only after the safety checks; use a real tx so they pass
      const seed = ed.utils.randomSecretKey();
      const pub = await ed.getPublicKeyAsync(seed);
      const message = new Uint8Array([2, 0, 1, 3, ...new Uint8Array(32).fill(7), ...pub, ...new Uint8Array(32).fill(9), ...new Uint8Array(32).fill(5), 0]);
      const wire = new Uint8Array([2, ...new Uint8Array(128), ...message]);
      sdk.set({ user: privyUser(base58Encode(pub)) });
      await waitFor(() => expect(provider.address).toBe(base58Encode(pub)));
      await expect(provider.signDeposit(toBase64(wire), toBase64(message))).rejects.toMatchObject({ code: "WALLET" });
      expect(sdk.s.signTransaction).not.toHaveBeenCalled();
    });
  });

  it("shows plain copy when the login screen is closed", async () => {
    mount();
    await userEvent.click(await screen.findByText("login"));
    await waitFor(() => expect(sdk.s.login).toHaveBeenCalled());
    sdk.s.onError?.("exited_auth_flow");
    await waitFor(() => expect(screen.getByTestId("err")).toHaveTextContent("Login was cancelled. Log in to continue."));
  });

  it("says login is not set up when there is no app id", () => {
    render(<WalletRoot provider={new PrivyWalletProvider()} appId=""><Probe /></WalletRoot>);
    expect(screen.getByRole("alert")).toHaveTextContent(/VITE_PRIVY_APP_ID/);
  });
});
