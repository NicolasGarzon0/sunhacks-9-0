import type { ReactNode } from "react";
import type { MarketView, Participant, Position } from "../api/types";
import { Card } from "../components/Basics";
import { Matchup } from "../components/Matchup";
import { ParticipantsList } from "../components/Participants";
import { PoolSummary } from "../components/PoolSummary";
import { YourPosition } from "../components/YourPosition";
import { refundReasonCopy } from "../lib/status";

interface Props { market: MarketView; participants: Participant[]; position: Position | null; viewer: string | null; now: number }

function Who({ market, participants, viewer }: Pick<Props, "market" | "participants" | "viewer">) {
  return (
    <Card title={`Who is in (${participants.length})`}>
      <ParticipantsList participants={participants} market={market} viewer={viewer} />
    </Card>
  );
}

export function LockedView(p: Props) {
  return (
    <>
      <Matchup market={p.market} now={p.now} />
      <Card tone="soft">
        <p className="big" data-testid="locked-note">Betting is closed. Waiting for the game.</p>
        <p className="muted">The pools and odds below are final. Results are sent by text after the game.</p>
      </Card>
      <YourPosition position={p.position} market={p.market} />
      <Card title="Final pools"><PoolSummary market={p.market} /></Card>
      <Who {...p} />
    </>
  );
}

export function SettledView(p: Props) {
  const m = p.market;
  const winner = m.winning_side === 1 ? m.teams.side1.name : m.teams.side0.name;
  return (
    <>
      <Matchup market={m} now={p.now} />
      <Card tone="soft">
        <p className="big" data-testid="winner">{winner} won.</p>
        <dl className="facts">
          <div><dt>Final multiplier</dt><dd>{m.final_multiplier ? `${m.final_multiplier}x` : "–"}</dd></div>
          <div><dt>Fee</dt><dd>${m.fee_paid_usd ?? "0.00"}</dd></div>
          <div><dt>Paid out to winners</dt><dd>${m.distributable_usd ?? "0.00"}</dd></div>
        </dl>
      </Card>
      <YourPosition position={p.position} market={m} />
      <Card title="Final pools"><PoolSummary market={m} /></Card>
      <Who {...p} />
    </>
  );
}

export function RefundedView(p: Props) {
  return (
    <>
      <Matchup market={p.market} now={p.now} />
      <Card tone="soft">
        <p className="big" data-testid="refund-reason">{refundReasonCopy(p.market.refund_reason)}</p>
        <p className="muted">Everyone gets their full stake back. No fee is taken.</p>
      </Card>
      <YourPosition position={p.position} market={p.market} />
      <Who {...p} />
    </>
  );
}

function Message({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return (
    <Card tone="soft">
      <h1 className="big">{title}</h1>
      <p className="muted">{children}</p>
      {action}
    </Card>
  );
}

export function FailedView() {
  return (
    <Message title="This bet did not start">
      We are sorry, something went wrong while setting it up. Nothing was charged. You can ask for a new bet in the chat.
    </Message>
  );
}
export function InvalidView() {
  return <Message title="This link is not valid">Check that you copied the whole link from the text message, or ask for a new one in the chat.</Message>;
}
export function ExpiredView() {
  return <Message title="This bet is over">The link has expired because the bet finished a while ago. Any payouts were already sent.</Message>;
}
export function NetworkErrorView({ onRetry }: { onRetry: () => void }) {
  return (
    <Message title="We could not load this bet" action={<button type="button" className="btn" onClick={onRetry}>Try again</button>}>
      Check your connection. We will keep trying.
    </Message>
  );
}
