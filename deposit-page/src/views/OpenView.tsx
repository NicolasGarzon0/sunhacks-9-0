import { useEffect, useMemo, useState } from "react";
import type { MarketView, Participant, Position } from "../api/types";
import { AmountInput } from "../components/AmountInput";
import { Card } from "../components/Basics";
import { DepositProgress } from "../components/DepositProgress";
import { Matchup } from "../components/Matchup";
import { ParticipantsList } from "../components/Participants";
import { PayoutPreview } from "../components/PayoutPreview";
import { SideButton } from "../components/SideButton";
import { SuccessScreen } from "../components/SuccessScreen";
import { WalletPanel } from "../components/WalletPanel";
import { YourPosition } from "../components/YourPosition";
import { useDeposit } from "../hooks/useDeposit";
import { usePreview } from "../hooks/usePreview";
import { useWalletInfo } from "../hooks/useWalletInfo";
import { msLeft, phase } from "../lib/countdown";
import { errorCopy } from "../lib/errors";
import { formatBase, parseAmount, sharePercent } from "../lib/money";
import type { WalletState } from "../wallet/WalletContext";

interface Props {
  token: string; market: MarketView; participants: Participant[]; position: Position | null;
  wallet: WalletState; now: number; applyMarket: (m: MarketView) => void; refresh: () => void;
}

const CHIPS = ["1", "5", "10", "20"];

export function OpenView({ token, market, participants, position, wallet, now, applyMarket, refresh }: Props) {
  const [picked, setPicked] = useState<0 | 1 | null>(null);
  const [amount, setAmount] = useState("");
  const [name, setName] = useState("");
  const [dismissed, setDismissed] = useState(false);
  const info = useWalletInfo(token, wallet.address);
  const dep = useDeposit(token, wallet.provider, wallet.address);

  const held = position?.has_position && position.side !== null ? position.side : null;
  const heldBase = position?.has_position ? position.stake_base ?? 0 : 0;
  const side: 0 | 1 | null = held ?? picked;
  const { min_stake_base: minBase, max_stake_base: maxBase, max_participants: maxPeople } = market.limits;
  const remaining = Math.max(0, maxBase - heldBase);
  const parsed = useMemo(() => parseAmount(amount, { minBase, maxBase: remaining }), [amount, minBase, remaining]);

  useEffect(() => { if (position?.display_name) setName((n) => n || position.display_name || ""); }, [position?.display_name]);

  const ph = phase(msLeft(market.funding_closes_at, now));
  const full = market.participant_count >= maxPeople && !position?.has_position;
  const teams = [market.teams.side0.name, market.teams.side1.name] as const;
  const total = market.pools.side0_base + market.pools.side1_base;

  let amountError: string | null = null;
  if (amount.trim() !== "" && !parsed.ok) {
    if (parsed.reason === "format") amountError = errorCopy("BAD_REQUEST");
    else if (parsed.reason === "below_min") amountError = errorCopy("BELOW_MIN", { minUsd: market.limits.min_stake_usd });
    else if (parsed.reason === "above_max") {
      amountError = heldBase > 0
        ? `You can add up to $${formatBase(remaining)} more (the limit is $${market.limits.max_stake_usd} per wallet).`
        : errorCopy("ABOVE_MAX", { maxUsd: market.limits.max_stake_usd });
    }
  }

  const walletInfo = info.info;
  const needsUsdc = walletInfo !== null && (!walletInfo.has_token_account || walletInfo.usdc_base < (parsed.ok ? parsed.base : minBase));
  const preview = usePreview(token, side, parsed.ok ? parsed.text : null, market.total_pool_base);

  let blocked: string | null = null;
  if (ph !== "open") blocked = ph === "closing" ? "Betting is about to close." : errorCopy("FUNDING_CLOSED");
  else if (full) blocked = "This bet is full. Only people who are already in can add more.";
  else if (info.unavailable) blocked = errorCopy("NOT_FOUND");
  else if (heldBase >= maxBase) blocked = `You already have the most one wallet can put in ($${market.limits.max_stake_usd}).`;

  const busy = dep.state.step === "preparing" || dep.state.step === "signing" || dep.state.step === "sending";
  const canDeposit = wallet.ready && !busy && blocked === null && side !== null && parsed.ok && !needsUsdc && walletInfo !== null;

  async function submit() {
    if (!canDeposit || !parsed.ok || side === null) return;
    const res = await dep.run({ side, amountUsd: parsed.text, displayName: name.trim() || undefined });
    if (res) {
      applyMarket(res.market);
      setDismissed(false);
      setAmount("");
      refresh();
      void info.refresh();
    } else {
      refresh();
      void info.refresh();
    }
  }

  const showSuccess = dep.state.step === "confirmed" && dep.state.result && !dismissed;
  const errCode = dep.state.error?.code;
  const errCtx = { minUsd: market.limits.min_stake_usd, maxUsd: market.limits.max_stake_usd };

  const label = busy ? "Working…" : !wallet.ready && wallet.provider.requiresLogin ? "Log in to deposit" : side === null ? "Pick a team" : !parsed.ok ? "Enter an amount" : `Deposit $${parsed.text} on ${teams[side]}`;

  return (
    <>
      <Matchup market={market} now={now} />
      <YourPosition position={position} market={market} />
      {showSuccess && dep.state.result ? (
        <SuccessScreen result={dep.state.result} summary={dep.state.summary} onView={() => { setDismissed(true); dep.reset(); }} />
      ) : (
        <form className="card deposit" onSubmit={(e) => { e.preventDefault(); void submit(); }} aria-label="Deposit">
          <h2 className="card-title">{held !== null ? "Add more" : "Pick your side"}</h2>
          <div className="sides" role="radiogroup" aria-label="Pick a team">
            {([0, 1] as const).map((s) => {
              const other = held !== null && held !== s;
              return (
                <SideButton
                  key={s} team={teams[s]} role={s === 0 ? "away" : "home"}
                  poolUsd={s === 0 ? market.pools.side0_usd : market.pools.side1_usd}
                  share={sharePercent(s === 0 ? market.pools.side0_base : market.pools.side1_base, total)}
                  multiplier={s === 0 ? market.multipliers?.side0 ?? null : market.multipliers?.side1 ?? null}
                  selected={side === s} disabled={other || busy}
                  reason={other ? `You already picked ${teams[held as 0 | 1]} in this bet.` : undefined}
                  onSelect={() => setPicked(s)}
                />
              );
            })}
          </div>
          {held !== null ? <p className="muted">A wallet can hold one team in a bet, so you can only add to {teams[held]}.</p> : null}
          {full ? <p className="notice" role="status">This bet is full. Only people who are already in can add more.</p> : null}
          <AmountInput value={amount} onChange={setAmount} chips={CHIPS} error={amountError} disabled={busy} />
          <PayoutPreview
            team={side === null ? null : teams[side]} preview={preview.preview} loading={preview.loading}
            errorCode={preview.error} feeBps={market.fee_bps} hasInput={parsed.ok}
          />
          <div className="field">
            <label htmlFor="display-name" className="label">Display name <span className="muted">(optional)</span></label>
            <input
              id="display-name" className="text" maxLength={24} autoComplete="nickname" placeholder="e.g. Dana"
              value={name} disabled={busy} onChange={(e) => setName(e.target.value)}
            />
          </div>
          {wallet.provider.requiresLogin && !wallet.address && !parsed.ok ? null : (
            <WalletPanel
              wallet={wallet} info={walletInfo} loading={info.loading} unavailable={info.unavailable} infoError={info.error}
              needsUsdc={needsUsdc} airdropping={info.airdropping} airdropError={info.airdropError} onAirdrop={() => void info.airdrop()}
              hasPosition={held !== null}
            />
          )}
          {blocked ? <p className="notice" role="status">{blocked}</p> : null}
          {busy || dep.state.step === "confirmed" ? <DepositProgress step={dep.state.step} retried={dep.state.retried} /> : null}
          {dep.state.step === "error" && errCode ? (
            <p className="error-box" role="alert">{errorCopy(errCode, errCtx)}</p>
          ) : null}
          <button type="submit" className="btn primary" disabled={!canDeposit}>{busy ? <span className="spinner" aria-hidden="true" /> : null}{label}</button>
        </form>
      )}
      <Card title={`Who is in (${participants.length})`}>
        <ParticipantsList participants={participants} market={market} viewer={wallet.address} />
      </Card>
    </>
  );
}
