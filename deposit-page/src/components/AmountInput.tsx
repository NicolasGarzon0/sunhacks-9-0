import { useId } from "react";

/** A chip adds its dollars to what is already typed. Whole cents only, so no float drift. */
function addChip(value: string, chip: string): string {
  const m = /^\$?(\d{1,9})(?:\.(\d{1,2}))?$/.exec(value.trim());
  const cents = (m ? Number(m[1]) * 100 + Number((m[2] ?? "").padEnd(2, "0")) : 0) + Number(chip) * 100;
  return cents % 100 === 0 ? String(cents / 100) : (cents / 100).toFixed(2);
}

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
          <button key={c} type="button" className={`pill${value.trim() === c ? " on" : ""}`} disabled={disabled} onClick={() => onChange(addChip(value, c))}>${c}</button>
        ))}
      </div>
      <p id={`${id}-err`} className="field-error" role={error ? "alert" : undefined}>{error ?? " "}</p>
    </div>
  );
}
