import { useId } from "react";

interface Props {
  value: string; onChange: (v: string) => void; chips: string[]; error: string | null; disabled?: boolean;
}

export function AmountInput({ value, onChange, chips, error, disabled }: Props) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id} className="label">Amount (USD)</label>
      <div className={`amount${error ? " invalid" : ""}`}>
        <span className="amount-sign" aria-hidden="true">$</span>
        <input
          id={id} inputMode="decimal" autoComplete="off" placeholder="0.00" value={value} disabled={disabled}
          aria-invalid={error ? true : undefined} aria-describedby={`${id}-err`}
          onChange={(e) => onChange(e.target.value)}
        />
      </div>
      <div className="chips" role="group" aria-label="Quick amounts">
        {chips.map((c) => (
          <button key={c} type="button" className={`pill${value.trim() === c ? " on" : ""}`} disabled={disabled} onClick={() => onChange(c)}>${c}</button>
        ))}
      </div>
      <p id={`${id}-err`} className="field-error" role={error ? "alert" : undefined}>{error ?? " "}</p>
    </div>
  );
}
