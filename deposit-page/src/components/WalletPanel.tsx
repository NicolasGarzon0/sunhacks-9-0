import { useState } from "react";
import type { WalletInfo } from "../api/types";
import { errorCopy } from "../lib/errors";
import { shortAddr } from "../lib/format";
import type { WalletState } from "../wallet/WalletContext";

interface Props {
  wallet: WalletState;
  info: WalletInfo | null;
  loading: boolean;
  unavailable: boolean;
  infoError: string | null;
  needsUsdc: boolean;
  airdropping: boolean;
  airdropError: string | null;
  onAirdrop: () => void;
  hasPosition: boolean;
}

export function WalletPanel(p: Props) {
  const [confirmReset, setConfirmReset] = useState(false);
  const { wallet } = p;
  if (wallet.provider.requiresLogin && !wallet.address) {
    return (
      <section className="wallet" aria-label="Your wallet">
        <p className="label">Wallet</p>
        <p>Log in with your email to get a wallet. It takes a few seconds.</p>
        <button type="button" className="btn" disabled={wallet.connecting} onClick={wallet.retry}>
          {wallet.connecting ? "Logging in…" : "Log in"}
        </button>
        {wallet.error ? <p className="field-error" role="alert">{wallet.error}</p> : null}
      </section>
    );
  }
  return (
    <section className="wallet" aria-label="Your wallet">
      <div className="wallet-row">
        <div>
          <span className="label">{wallet.provider.requiresLogin ? "Signed in" : wallet.provider.name}</span>
          <p className="addr" title={wallet.address ?? undefined}>{wallet.address ? shortAddr(wallet.address) : "Preparing…"}</p>
        </div>
        <div className="wallet-bal">
          <span className="label">USDC balance</span>
          <p className="bal">{p.info ? `$${p.info.usdc_usd}` : p.loading ? "…" : "–"}</p>
        </div>
      </div>
      {wallet.provider.requiresLogin ? (
        <p className="testnote">
          <button type="button" className="link" onClick={() => void wallet.reset()}>Log out</button>
        </p>
      ) : null}
      {wallet.error ? (
        <p className="field-error" role="alert">{wallet.error}{wallet.provider.requiresLogin ? null : <> <button type="button" className="link" onClick={wallet.retry}>Try again</button></>}</p>
      ) : null}
      {wallet.provider.isTest ? (
        <p className="testnote">
          <strong>Test wallet on devnet.</strong> It holds test money only and lives in this browser.{" "}
          {confirmReset ? (
            <span className="reset-confirm">
              {p.hasPosition ? "You will lose access to your position here. " : ""}
              <button type="button" className="link danger" onClick={() => { setConfirmReset(false); void wallet.reset(); }}>Yes, make a new wallet</button>{" "}
              <button type="button" className="link" onClick={() => setConfirmReset(false)}>Cancel</button>
            </span>
          ) : (
            <button type="button" className="link" onClick={() => setConfirmReset(true)}>Reset wallet</button>
          )}
        </p>
      ) : null}
      {p.unavailable ? (
        <p className="field-error" role="alert">{errorCopy("NOT_FOUND")}</p>
      ) : p.infoError && !p.info ? (
        <p className="field-error" role="alert">{errorCopy(p.infoError)}</p>
      ) : null}
      {p.needsUsdc && !p.unavailable ? (
        <div className="airdrop">
          <p>{p.info && !p.info.has_token_account ? "This wallet has no test USDC yet." : "This wallet needs more test USDC."}</p>
          <button type="button" className="btn secondary" disabled={p.airdropping || !wallet.ready} onClick={p.onAirdrop}>
            {p.airdropping ? "Getting test USDC…" : "Get test USDC"}
          </button>
          {p.airdropError ? <p className="field-error" role="alert">{errorCopy(p.airdropError)}</p> : null}
        </div>
      ) : null}
    </section>
  );
}
