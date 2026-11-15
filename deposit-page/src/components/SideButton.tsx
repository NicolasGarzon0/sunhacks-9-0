import { hueFor } from "../lib/format";
import { TeamBadge } from "./Basics";

interface Props {
  team: string; role: "away" | "home"; poolUsd: string; share: number; multiplier: string | null;
  selected: boolean; disabled?: boolean; reason?: string; onSelect: () => void;
}

export function SideButton({ team, role, poolUsd, share, multiplier, selected, disabled, reason, onSelect }: Props) {
  return (
    <button
      type="button" role="radio" aria-checked={selected} disabled={disabled}
      className={`side${selected ? " selected" : ""}`}
      style={{ ["--hue" as string]: hueFor(team) }}
      onClick={onSelect}
      aria-label={`${team}, ${role}. Pool $${poolUsd}, ${share} percent. ${multiplier ? `Pays about ${multiplier} times your stake.` : ""}${disabled && reason ? ` ${reason}` : ""}`}
    >
      <span className="side-head"><TeamBadge name={team} /><span className="side-role">{role}</span></span>
      <span className="side-name clip" title={team}>{team}</span>
      <span className="side-mult">{multiplier ? `${multiplier}x` : "–"}</span>
      <span className="side-meta"><span>${poolUsd}</span><span>{share}%</span></span>
      <span className="side-bar" aria-hidden="true"><span style={{ width: `${share}%` }} /></span>
      {disabled && reason ? <span className="side-reason">{reason}</span> : null}
    </button>
  );
}

