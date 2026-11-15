import type { ReactNode } from "react";
import { hueFor, initials } from "../lib/format";

/** A generated colored initial. No logos or league marks. */
export function TeamBadge({ name, size = "md" }: { name: string; size?: "sm" | "md" | "lg" }) {
  return (
    <span className={`badge badge-${size}`} style={{ ["--hue" as string]: hueFor(name) }} aria-hidden="true">
      {initials(name)}
    </span>
  );
}

const LABEL: Record<string, string> = { open: "Open", locked: "Locked", settled: "Settled", refunded: "Refunded", failed: "Failed" };

export function StatusChip({ status }: { status: string }) {
  return <span className={`chip chip-${LABEL[status] ? status : "locked"}`}>{LABEL[status] ?? "Waiting"}</span>;
}

export function Skeleton() {
  return (
    <div className="skeleton" aria-busy="true" aria-label="Loading the bet">
      <div className="sk sk-line w60" /><div className="sk sk-title" /><div className="sk sk-line w40" />
      <div className="sk-row"><div className="sk sk-card" /><div className="sk sk-card" /></div>
      <div className="sk sk-block" />
    </div>
  );
}

export function Card({ title, children, tone }: { title?: string; children: ReactNode; tone?: "soft" | "accent" }) {
  return (
    <section className={`card${tone ? ` card-${tone}` : ""}`}>
      {title ? <h2 className="card-title">{title}</h2> : null}
      {children}
    </section>
  );
}

export function ReconnectBanner() {
  return <div className="banner" role="status">Reconnecting… showing the last update we received.</div>;
}

export function LiveRegion({ message }: { message: string }) {
  return <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{message}</div>;
}

export function Footer() {
  return <footer className="footer"><p>Test money on devnet. Fees are sponsored.</p></footer>;
}
