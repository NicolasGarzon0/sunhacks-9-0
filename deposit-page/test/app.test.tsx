import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DepositTx } from "../src/api/types";
import { market, people, position } from "./fixtures";

const api = vi.hoisted(() => ({
  market: vi.fn(), participants: vi.fn(), position: vi.fn(), preview: vi.fn(), wallet: vi.fn(), airdrop: vi.fn(),
  depositTx: vi.fn(), depositSubmit: vi.fn(),
}));
vi.mock("../src/api/client", async (orig) => ({ ...(await orig<typeof import("../src/api/client")>()), api }));

import { ApiError } from "../src/api/client";
import { BetPage } from "../src/BetPage";
import * as ed from "@noble/ed25519";
import { base58Encode, toBase64 } from "../src/lib/format";
import type { PrivyBridge } from "../src/wallet/PrivyWalletProvider";
import { LocalDevWalletProvider } from "../src/wallet/LocalDevWalletProvider";
import { PrivyWalletProvider } from "../src/wallet/PrivyWalletProvider";
import type { WalletProvider } from "../src/wallet";
import { WalletProviderContext } from "../src/wallet/WalletContext";

const mem = () => {
  const m = new Map<string, string>();
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k) };
};

function txFor(side: 0 | 1, amount: string): DepositTx {
  return {
    ok: true, bet_id: "b1", transaction_b64: "AAAA", message_b64: "AQIDBA==", blockhash: "h", last_valid_block_height: 1,
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    summary: {
      side, team: side === 0 ? "Seattle Seahawks" : "Arizona Cardinals", amount_usd: `${amount}.00`, amount_base: Number(amount) * 1e6,
      your_total_stake_usd: `${amount}.00`, fee_bps: 500, fee_line: "fee", payout_usd: "9.75", profit_usd: "4.75", multiplier: "1.95", text: "t",
    },
  };
}

function renderBet(provider: WalletProvider = new LocalDevWalletProvider(mem())) {
  return render(<WalletProviderContext provider={provider}><BetPage token="tok" /></WalletProviderContext>);
}

const funded = { ok: true, wallet: "w", has_token_account: true, usdc_base: 25_000_000, usdc_usd: "25.00" };

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset());
  api.market.mockResolvedValue(market());
  api.participants.mockResolvedValue(people());
  api.position.mockResolvedValue(position());
  api.wallet.mockResolvedValue(funded);
  api.preview.mockResolvedValue({ ok: true, side: 1, amount_base: 5e6, amount_usd: "5.00", payout_usd: "9.75", profit_usd: "4.75", multiplier: "1.95", fee_bps: 500, fee_line: "f" });
});

async function openForm() {
  await screen.findByRole("radiogroup");
}

/** The wallet box comes after a team and an amount are set. */
async function openWallet(amount = "5") {
  await openForm();
  await userEvent.click(screen.getByRole("radio", { name: /^Arizona Cardinals/ }));
  await userEvent.type(screen.getByLabelText("Amount (USD)"), amount);
  await waitFor(() => expect(screen.getByText(/Test wallet on devnet/)).toBeInTheDocument());
  await waitFor(() => expect(screen.getByText("$25.00")).toBeInTheDocument());
}

describe("status views", () => {
  it("shows a skeleton while loading", () => {
    api.market.mockReturnValue(new Promise(() => {}));
    renderBet();
    expect(screen.getByLabelText("Loading the bet")).toBeInTheDocument();
  });

  it("explains an invalid link (404) and an expired link (410) without codes", async () => {
    api.market.mockRejectedValue(new ApiError("BAD_TOKEN", 404));
    const a = renderBet();
    expect(await screen.findByText("This link is not valid")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("BAD_TOKEN");
    a.unmount();
    api.market.mockRejectedValue(new ApiError("LINK_EXPIRED", 410));
    renderBet();
    expect(await screen.findByText("This bet is over")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("LINK_EXPIRED");
  });

  it("shows a network error with retry", async () => {
    api.market.mockRejectedValueOnce(new ApiError("NETWORK", 0));
    renderBet();
    const retry = await screen.findByRole("button", { name: "Try again" });
    await userEvent.click(retry);
    expect(await screen.findByRole("radiogroup")).toBeInTheDocument();
  });

  it("open: header, pools, multipliers, countdown, participants", async () => {
    renderBet();
    await openForm();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Seattle Seahawks");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Arizona Cardinals");
    expect(screen.getByText("$27.25")).toBeInTheDocument();
    expect(screen.getByText("4 / 20")).toBeInTheDocument();
    expect(screen.getByText("2.27x")).toBeInTheDocument();
    expect(screen.getByText("1.95x")).toBeInTheDocument();
    expect(screen.getByTestId("countdown").textContent).toMatch(/\d/);
    expect(screen.getByText("Dana")).toBeInTheDocument();
    expect(screen.getByText("Test money on devnet. Fees are sponsored.")).toBeInTheDocument();
  });

  it("the countdown follows the server clock, not a wrong browser clock", async () => {
    // the server is one hour ahead of this browser; betting closes 90 s after the server's now
    const serverNow = Date.now() + 3_600_000;
    api.market.mockResolvedValue(market({
      server_time: new Date(serverNow).toISOString(), funding_closes_at: new Date(serverNow + 90_000).toISOString(),
    }));
    renderBet();
    await openForm();
    expect(screen.getByTestId("countdown").textContent).toMatch(/^1m \d\ds$/); // the browser clock alone would say "1h 01m"
  });

  it("a browser clock that is behind cannot keep betting open after the server has closed it", async () => {
    const serverNow = Date.now() + 3_600_000;
    api.market.mockResolvedValue(market({
      server_time: new Date(serverNow).toISOString(), funding_closes_at: new Date(serverNow - 5_000).toISOString(),
    }));
    renderBet();
    await openForm();
    expect(screen.getByTestId("countdown").textContent).toBe("Closed"); // the browser clock alone would count down 55 minutes
  });

  it("without server_time (an older backend) the page falls back to the browser clock", async () => {
    api.market.mockResolvedValue(market({ funding_closes_at: new Date(Date.now() + 90_000).toISOString() }));
    renderBet();
    await openForm();
    expect(screen.getByTestId("countdown").textContent).toMatch(/^1m \d\ds$/);
  });

  it("locked: waiting for the game, final pools, your position", async () => {
    api.market.mockResolvedValue(market({ status: "locked", game_pending: true }));
    api.position.mockResolvedValue(position({ has_position: true, side: 1, stake_base: 5e6, stake_usd: "5.00" }));
    renderBet();
    expect(await screen.findByTestId("locked-note")).toHaveTextContent("Waiting for the game");
    expect(screen.queryByRole("radiogroup")).toBeNull();
    expect(await screen.findByTestId("your-position")).toHaveTextContent("$5.00 on Arizona Cardinals");
  });

  it("settled: winner, fee, multiplier, payout and paid status, participants with outcomes", async () => {
    api.market.mockResolvedValue(market({
      status: "settled", winning_side: 1, fee_paid_usd: "0.60", distributable_usd: "26.65", final_multiplier: "1.75",
    }));
    api.position.mockResolvedValue(position({ has_position: true, side: 1, stake_usd: "9.00", payout_usd: "15.75", paid: true }));
    renderBet();
    expect(await screen.findByTestId("winner")).toHaveTextContent("Arizona Cardinals won");
    expect(screen.getByText("1.75x", { selector: "dd" })).toBeInTheDocument();
    expect(screen.getByText("$0.60")).toBeInTheDocument();
    expect(await screen.findByTestId("your-position")).toHaveTextContent("You won $15.75");
    expect(screen.getByText("Paid to your wallet.")).toBeInTheDocument();
    expect(screen.getAllByText("Won").length + screen.getAllByText("Lost").length).toBe(2);
  });

  it.each([
    ["one_sided", /same team/], ["too_few_participants", /Not enough people/], ["cancelled", /cancelled/],
    ["postponed", /postponed/], ["tie", /tie/], ["no_result", /final result/],
  ])("refunded (%s) in plain words", async (reason, re) => {
    api.market.mockResolvedValue(market({ status: "refunded", refund_reason: reason, multipliers: null }));
    renderBet();
    expect(await screen.findByTestId("refund-reason")).toHaveTextContent(re);
    expect(screen.getByTestId("refund-reason").textContent).not.toMatch(/_/);
  });

  it("refunded: the viewer's refund status", async () => {
    api.market.mockResolvedValue(market({ status: "refunded", refund_reason: "tie", multipliers: null }));
    api.position.mockResolvedValue(position({ has_position: true, side: 0, stake_usd: "5.00", paid: false }));
    renderBet();
    expect(await screen.findByTestId("your-position")).toHaveTextContent("being returned");
  });

  it("failed: apology", async () => {
    api.market.mockResolvedValue(market({ status: "failed", failed: true }));
    renderBet();
    expect(await screen.findByText("This bet did not start")).toBeInTheDocument();
  });
});

describe("deposit", () => {
  async function fill(side: RegExp, amount: string) {
    await userEvent.click(screen.getByRole("radio", { name: side }));
    await userEvent.type(screen.getByLabelText("Amount (USD)"), amount);
  }

  it("runs deposit_tx, signs, submits, then shows the success screen and the pool", async () => {
    api.depositTx.mockResolvedValue(txFor(1, "5"));
    const after = market({ participant_count: 5, total_pool_base: 32_250_000, total_pool_usd: "32.25" });
    api.depositSubmit.mockImplementation(async () => {
      api.market.mockResolvedValue(after);
      return { ok: true, signature: "SIG123", explorer: "x", already: false, market: after };
    });
    renderBet();
    await openForm();
    await fill(/^Arizona Cardinals/, "5");
    await waitFor(() => expect(screen.getByTestId("preview")).toHaveTextContent("about $9.75 (1.95x) after the 5% fee on the losing side"));
    await userEvent.type(screen.getByLabelText(/Display name/), "Dana B");
    await userEvent.click(screen.getByRole("button", { name: /Deposit \$5 on Arizona Cardinals/ }));
    expect(await screen.findByTestId("success")).toBeInTheDocument();
    expect(api.depositTx).toHaveBeenCalledWith("tok", expect.any(String), 1, "5");
    const submitArgs = api.depositSubmit.mock.calls[0];
    expect(submitArgs[3]).toBe("5");
    expect(submitArgs[4]).toMatchObject({ message_b64: "AQIDBA==" });
    expect(submitArgs[4].user_signature_b64.length).toBeGreaterThan(80);
    expect(submitArgs[5]).toBe("Dana B");
    const link = screen.getByRole("link", { name: /explorer/ });
    expect(link).toHaveAttribute("href", expect.stringContaining("SIG123"));
    expect(link).toHaveAttribute("href", expect.stringContaining("cluster=devnet"));
    await waitFor(() => expect(screen.getByText("$32.25")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "View the pool" }));
    expect(screen.queryByTestId("success")).toBeNull();
    expect(screen.getByRole("radiogroup")).toBeInTheDocument();
  });

  it("rebuilds and signs again once after TX_EXPIRED, then succeeds", async () => {
    api.depositTx.mockResolvedValue(txFor(0, "5"));
    api.depositSubmit
      .mockRejectedValueOnce(new ApiError("TX_EXPIRED", 409))
      .mockResolvedValueOnce({ ok: true, signature: "S2", explorer: "x", already: false, market: market() });
    renderBet();
    await openForm();
    await fill(/^Seattle Seahawks/, "5");
    await userEvent.click(screen.getByRole("button", { name: /Deposit \$5 on Seattle Seahawks/ }));
    expect(await screen.findByTestId("success")).toBeInTheDocument();
    expect(api.depositTx).toHaveBeenCalledTimes(2);
    expect(api.depositSubmit).toHaveBeenCalledTimes(2);
  });

  it("shows the error when TX_EXPIRED happens twice", async () => {
    api.depositTx.mockResolvedValue(txFor(0, "5"));
    api.depositSubmit.mockRejectedValue(new ApiError("TX_EXPIRED", 409));
    renderBet();
    await openForm();
    await fill(/^Seattle Seahawks/, "5");
    await userEvent.click(screen.getByRole("button", { name: /Deposit \$5/ }));
    expect(await screen.findByRole("alert", {}, { timeout: 3000 })).toHaveTextContent("That took too long. Please confirm again.");
    expect(api.depositTx).toHaveBeenCalledTimes(2);
    expect(api.depositSubmit).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: /Deposit \$5/ })).toBeEnabled();
  });

  it.each([
    ["BELOW_MIN", "The minimum is $1.00."], ["ABOVE_MAX", "The most one wallet can put in is $20.00."],
    ["MARKET_FULL", "This bet is full."], ["ALREADY_OTHER_SIDE", "You already picked the other team in this bet."],
    ["MARKET_NOT_OPEN", "Betting is closed for this game."], ["FUNDING_CLOSED", "Betting just closed."],
    ["NO_TOKEN_ACCOUNT", "Get some test USDC first."], ["INSUFFICIENT_USDC", "You do not have enough USDC."],
    ["TX_REJECTED", "We could not verify that transaction. Please start the deposit again."],
    ["SUBMIT_FAILED", "Something went wrong on our side. Please try again."], ["RATE_LIMITED", "Too many tries. Wait a moment."],
    ["SOMETHING_NEW", "Something unexpected happened. Please try again."],
  ])("maps %s from deposit_tx to friendly copy", async (code, copy) => {
    api.depositTx.mockRejectedValue(new ApiError(code, 409));
    renderBet();
    await openForm();
    await fill(/^Seattle Seahawks/, "5");
    await userEvent.click(screen.getByRole("button", { name: /Deposit \$5/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(copy);
    expect(document.body.textContent).not.toContain(code === "SOMETHING_NEW" ? "NOPE" : code);
    expect(api.depositSubmit).not.toHaveBeenCalled();
  });

  it("disables the button for invalid input and explains the amount", async () => {
    renderBet();
    await openForm();
    const noBtn = () => expect(screen.queryByRole("button", { name: /Pick a team|Enter an amount|Deposit/ })).toBeNull();
    noBtn(); // no amount field and no button until a team is picked
    expect(screen.queryByLabelText("Amount (USD)")).toBeNull();
    await userEvent.click(screen.getByRole("radio", { name: /^Arizona Cardinals/ }));
    noBtn(); // the amount comes first, the button after it
    await userEvent.type(screen.getByLabelText("Amount (USD)"), "0.5");
    expect(screen.getByText("The minimum is $1.00.")).toBeInTheDocument();
    noBtn();
    await userEvent.clear(screen.getByLabelText("Amount (USD)"));
    await userEvent.type(screen.getByLabelText("Amount (USD)"), "25");
    expect(screen.getByText("The most one wallet can put in is $20.00.")).toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText("Amount (USD)"));
    await userEvent.type(screen.getByLabelText("Amount (USD)"), "1.0000001");
    expect(screen.getByText("Enter a valid dollar amount.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "$10" }));
    expect(screen.getByLabelText("Amount (USD)")).toHaveValue("10");
    expect(screen.getByRole("button", { name: /Deposit \$10 on Arizona Cardinals/ })).toBeEnabled();
  });

  it("clicking a picked team again unselects it", async () => {
    renderBet();
    await openForm();
    const team = () => screen.getByRole("radio", { name: /^Arizona Cardinals/ });
    await userEvent.click(team());
    expect(team()).toHaveAttribute("aria-checked", "true");
    await userEvent.click(team());
    expect(team()).toHaveAttribute("aria-checked", "false");
  });

  it("disables the wrong side and says why when the wallet already holds the other team", async () => {
    api.position.mockResolvedValue(position({ has_position: true, side: 0, stake_base: 5e6, stake_usd: "5.00" }));
    renderBet();
    await openForm();
    await waitFor(() => expect(screen.getByRole("radio", { name: /^Arizona Cardinals/ })).toBeDisabled());
    expect(screen.getByRole("radio", { name: /^Seattle Seahawks/ })).toBeEnabled();
    expect(screen.getByRole("radio", { name: /^Seattle Seahawks/ })).toBeChecked();
    expect(screen.getAllByText(/You already picked Seattle Seahawks in this bet/).length).toBeGreaterThan(0);
  });

  it("says the bet is full to a new wallet before they try, and still lets members top up", async () => {
    api.market.mockResolvedValue(market({ participant_count: 20 }));
    renderBet();
    await openForm();
    expect((await screen.findAllByText(/This bet is full/)).length).toBeGreaterThan(0);
    await fill(/^Seattle Seahawks/, "5");
    expect(screen.getByRole("button", { name: /Deposit/ })).toBeDisabled();
  });

  it("members of a full bet can still add", async () => {
    api.market.mockResolvedValue(market({ participant_count: 20 }));
    api.position.mockResolvedValue(position({ has_position: true, side: 1, stake_base: 5e6, stake_usd: "5.00" }));
    renderBet();
    await openForm();
    await waitFor(() => expect(screen.getByRole("radio", { name: /^Arizona Cardinals/ })).toBeChecked());
    await userEvent.type(screen.getByLabelText("Amount (USD)"), "3");
    expect(screen.queryByText(/This bet is full/)).toBeNull();
    expect(await screen.findByRole("button", { name: /Deposit \$3 on Arizona Cardinals/ })).toBeEnabled();
  });
});

describe("airdrop", () => {
  it("offers test USDC when the wallet has no token account, then shows the balance", async () => {
    api.wallet.mockResolvedValue({ ok: true, wallet: "w", has_token_account: false, usdc_base: 0, usdc_usd: "0.00" });
    api.airdrop.mockResolvedValue({ ...funded, minted_base: 25e6, minted_usd: "25.00", signature: "AIR", explorer: "x" });
    renderBet();
    await screen.findByRole("radiogroup");
    await userEvent.click(screen.getByRole("radio", { name: /^Arizona Cardinals/ }));
    await userEvent.type(screen.getByLabelText("Amount (USD)"), "5");
    await userEvent.click(await screen.findByRole("button", { name: "Get test USDC" }));
    expect(await screen.findByText("$25.00")).toBeInTheDocument();
    expect(api.airdrop).toHaveBeenCalledWith("tok", expect.any(String));
    expect(screen.queryByRole("button", { name: "Get test USDC" })).toBeNull();
  });

  it("shows friendly copy when the airdrop is unavailable", async () => {
    api.wallet.mockResolvedValue({ ok: true, wallet: "w", has_token_account: false, usdc_base: 0, usdc_usd: "0.00" });
    api.airdrop.mockRejectedValue(new ApiError("AIRDROP_LIMIT", 409));
    renderBet();
    await screen.findByRole("radiogroup");
    await userEvent.click(screen.getByRole("radio", { name: /^Arizona Cardinals/ }));
    await userEvent.type(screen.getByLabelText("Amount (USD)"), "5");
    await userEvent.click(await screen.findByRole("button", { name: "Get test USDC" }));
    expect(await screen.findByText("Test USDC is not available right now.")).toBeInTheDocument();
  });

  it("offers it when the balance is too low for the amount", async () => {
    api.wallet.mockResolvedValue({ ok: true, wallet: "w", has_token_account: true, usdc_base: 2e6, usdc_usd: "2.00" });
    renderBet();
    await screen.findByRole("radiogroup");
    await userEvent.click(screen.getByRole("radio", { name: /^Arizona Cardinals/ }));
    await userEvent.type(screen.getByLabelText("Amount (USD)"), "10");
    expect(await screen.findByRole("button", { name: "Get test USDC" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Deposit/ })).toBeDisabled();
  });

  it("tells the user when deposits are unavailable on this server (mock mode)", async () => {
    api.wallet.mockRejectedValue(new ApiError("NOT_FOUND", 404));
    renderBet();
    await screen.findByRole("radiogroup");
    expect(await screen.findAllByText("Deposits are not available on this server right now.")).not.toHaveLength(0);
  });
});

describe("untrusted text and wallet", () => {
  it("renders hostile and very long display names as plain text", async () => {
    const long = "A".repeat(500);
    api.participants.mockResolvedValue([
      { wallet: "Aaaa1111111111111111111111111111111111111111", side: 0, stake_base: 1e6, stake_usd: "1.00", display_name: "<img src=x onerror=alert(1)>" },
      { wallet: "Bbbb2222222222222222222222222222222222222222", side: 1, stake_base: 1e6, stake_usd: "1.00", display_name: "<script>alert(1)</script>" },
      { wallet: "Cccc3333333333333333333333333333333333333333", side: 1, stake_base: 1e6, stake_usd: "1.00", display_name: long },
    ]);
    api.market.mockResolvedValue(market({
      teams: { side0: { name: "<b>Evil</b> FC", role: "away" }, side1: { name: "Arizona Cardinals", role: "home" } },
    }));
    const { container } = renderBet();
    await openForm();
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument();
    expect(screen.getByText(long)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(within(screen.getByRole("heading", { level: 1 })).getByText("<b>Evil</b> FC")).toBeInTheDocument();
  });

  it("shows the test wallet notice and lets the user reset it", async () => {
    renderBet();
    await openWallet();
    const addr = () => document.querySelector(".addr")!.textContent;
    const before = addr();
    expect(before).toMatch(/…/);
    await userEvent.click(screen.getByRole("button", { name: "Reset wallet" }));
    await userEvent.click(screen.getByRole("button", { name: "Yes, make a new wallet" }));
    await waitFor(() => expect(addr()).not.toBe(before));
    expect(addr()).toMatch(/…/);
  });

  it("shows a Log in button and a disabled deposit for a login wallet that is not signed in", async () => {
    renderBet(new PrivyWalletProvider());
    await screen.findByRole("radiogroup");
    await userEvent.click(screen.getByRole("radio", { name: /^Arizona Cardinals/ }));
    expect(screen.queryByRole("button", { name: "Log in" })).toBeNull(); // the sign in box waits for an amount
    await userEvent.type(screen.getByLabelText("Amount (USD)"), "5");
    expect(screen.getAllByRole("button", { name: /Log in/ })).toHaveLength(1); // one log in button, in the log in box
    expect(screen.queryByLabelText(/Display name/)).toBeNull(); // the name box comes after logging in
    expect(screen.queryByRole("button", { name: /Deposit/ })).toBeNull();
  });
});

describe("login wallet (Privy) in the page", () => {
  /** A provider wired to a fake Privy: login makes a real key and signs for real. */
  async function setup() {
    const seed = ed.utils.randomSecretKey();
    const pub = await ed.getPublicKeyAsync(seed);
    const address = base58Encode(pub);
    const message = new Uint8Array([2, 0, 1, 3, ...new Uint8Array(32).fill(7), ...pub, ...new Uint8Array(32).fill(9), ...new Uint8Array(32).fill(5), 0]);
    const wire = new Uint8Array([2, ...new Uint8Array(128), ...message]);
    const provider = new PrivyWalletProvider();
    const st = { authenticated: false, address: null as string | null };
    const bridge: PrivyBridge = {
      sdkReady: true, authenticated: false, address: null,
      login: () => { st.authenticated = true; st.address = address; provider.attach({ ...bridge, ...st }); },
      logout: async () => { st.authenticated = false; st.address = null; provider.attach({ ...bridge, ...st }); },
      signTransaction: async (w) => { const o = w.slice(); o.set(await ed.signAsync(message, seed), 65); return o; },
    };
    provider.attach(bridge);
    return { provider, address, wire, message };
  }

  it("asks the user to log in, then shows a shortened address and a Log out button", async () => {
    const { provider, address } = await setup();
    renderBet(provider);
    await screen.findByRole("radiogroup");
    expect(screen.queryByText(/Test wallet on devnet/)).toBeNull();
    await userEvent.click(screen.getByRole("radio", { name: /^Arizona Cardinals/ }));
    await userEvent.type(screen.getByLabelText("Amount (USD)"), "5");
    await userEvent.click(screen.getByRole("button", { name: "Log in" }));
    await waitFor(() => expect(screen.getByText("Signed in")).toBeInTheDocument());
    const shown = screen.getByTitle(address);
    expect(shown.textContent).toMatch(/…/);
    expect(shown.textContent).not.toBe(address);
    expect(api.wallet).toHaveBeenCalledWith("tok", address);
    await userEvent.click(screen.getByRole("button", { name: "Log out" }));
    expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument();
  });

  it("gets test USDC and deposits with the Privy address exactly like the local wallet", async () => {
    const { provider, address, wire, message } = await setup();
    api.wallet.mockResolvedValue({ ok: true, wallet: address, has_token_account: false, usdc_base: 0, usdc_usd: "0.00" });
    api.airdrop.mockResolvedValue({ ...funded, wallet: address, minted_base: 25e6, minted_usd: "25.00", signature: "AIR", explorer: "x" });
    api.depositTx.mockResolvedValue({ ...txFor(1, "5"), transaction_b64: toBase64(wire), message_b64: toBase64(message) });
    api.depositSubmit.mockImplementation(async () => ({ ok: true, signature: "SIG9", explorer: "x", already: false, market: market() }));
    renderBet(provider);
    await userEvent.click(await screen.findByRole("radio", { name: /^Arizona Cardinals/ }));
    await userEvent.type(await screen.findByLabelText("Amount (USD)"), "5");
    await userEvent.click(await screen.findByRole("button", { name: "Log in" }));
    await userEvent.click(await screen.findByRole("button", { name: "Get test USDC" }));
    expect(await screen.findByText("$25.00")).toBeInTheDocument();
    expect(api.airdrop).toHaveBeenCalledWith("tok", address);
    await userEvent.click(await screen.findByRole("button", { name: /Deposit \$5 on Arizona Cardinals/ }));
    expect(await screen.findByTestId("success")).toBeInTheDocument();
    expect(api.depositTx).toHaveBeenCalledWith("tok", address, 1, "5");
    const args = api.depositSubmit.mock.calls[0];
    expect(args[1]).toBe(address);
    expect(args[4]).toMatchObject({ message_b64: toBase64(message) });
  });

  it("shows plain copy when the signature is rejected, and sends nothing", async () => {
    const { provider, address, wire, message } = await setup();
    const real = provider.signDeposit.bind(provider);
    void real;
    api.depositTx.mockResolvedValue({ ...txFor(1, "5"), transaction_b64: toBase64(wire), message_b64: toBase64(message) });
    renderBet(provider);
    await userEvent.click(await screen.findByRole("radio", { name: /^Arizona Cardinals/ }));
    await userEvent.type(await screen.findByLabelText("Amount (USD)"), "5");
    await userEvent.click(await screen.findByRole("button", { name: "Log in" }));
    await waitFor(() => expect(screen.getByText("$25.00")).toBeInTheDocument());
    // make the next sign attempt fail like a rejected prompt
    const spy = vi.spyOn(provider, "signDeposit").mockRejectedValue(new (await import("../src/wallet")).WalletError("WALLET_REJECTED"));
    await userEvent.click(await screen.findByRole("button", { name: /Deposit \$5 on Arizona Cardinals/ }));
    expect(await screen.findByText("The signature was not approved, so nothing was sent. Please try again.")).toBeInTheDocument();
    expect(api.depositSubmit).not.toHaveBeenCalled();
    spy.mockRestore();
    void address;
  });
});
