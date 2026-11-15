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
    const btn = () => screen.getByRole("button", { name: /Pick a team|Enter an amount|Deposit/ });
    expect(btn()).toBeDisabled();
    await userEvent.click(screen.getByRole("radio", { name: /^Arizona Cardinals/ }));
    expect(btn()).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Amount (USD)"), "0.5");
    expect(screen.getByText("The minimum is $1.00.")).toBeInTheDocument();
    expect(btn()).toBeDisabled();
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
    await openForm();
    const addr = () => document.querySelector(".addr")!.textContent;
    const before = addr();
    expect(before).toMatch(/…/);
    await userEvent.click(screen.getByRole("button", { name: "Reset wallet" }));
    await userEvent.click(screen.getByRole("button", { name: "Yes, make a new wallet" }));
    await waitFor(() => expect(addr()).not.toBe(before));
    expect(addr()).toMatch(/…/);
  });

  it("surfaces the privy stub's error instead of crashing", async () => {
    renderBet(new PrivyWalletProvider());
    await screen.findByRole("radiogroup");
    expect(await screen.findByText(/not implemented in chunk 4/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Pick a team/ })).toBeDisabled();
  });
});
