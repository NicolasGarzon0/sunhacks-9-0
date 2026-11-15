import type { DepositStep } from "../hooks/useDeposit";

const STEPS: { key: DepositStep; label: string }[] = [
  { key: "preparing", label: "Preparing" },
  { key: "signing", label: "Waiting for signature" },
  { key: "sending", label: "Sending" },
  { key: "confirmed", label: "Confirmed" },
];

export function DepositProgress({ step, retried }: { step: DepositStep; retried: boolean }) {
  const idx = STEPS.findIndex((s) => s.key === step);
  return (
    <div className="progress" role="status" aria-live="polite">
      <ol>
        {STEPS.map((s, i) => (
          <li key={s.key} className={i < idx || step === "confirmed" ? "done" : i === idx ? "now" : ""} aria-current={i === idx ? "step" : undefined}>
            {s.label}
          </li>
        ))}
      </ol>
      {retried ? <p className="muted">That took a bit long, so we started it again.</p> : null}
    </div>
  );
}
