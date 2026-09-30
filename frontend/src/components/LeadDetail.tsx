import { useEffect, useState } from "react";
import { STATUSES, type Lead, type LeadStatus } from "../types";
import { band } from "./LeadTable";
import MatchPanel from "./MatchPanel";
import RefreshBar from "./RefreshBar";

interface Props {
  lead: Lead | null;
  onUpdate: (id: number, patch: { status?: LeadStatus; notes?: string }) => Promise<void>;
  onRefreshed: () => void;
}

// The match knows about the evidence; the older rule-based insight only sees directory stats.
const nextAction = (l: Lead) => l.match?.next_action ?? l.insights?.next_best_action ?? null;

function briefText(l: Lead): string {
  const i = l.insights;
  const c = l.contractor;
  const m = l.match;
  const head = m?.match_pct != null ? `${c.name} — ${m.match_pct}% GAF match` : `${c.name} — fit ${l.score}/100`;
  return [
    head,
    m?.summary ?? i?.summary ?? "",
    "",
    ...(i ? ["Talking points:", ...i.talking_points.map((t) => `• ${t}`), ""] : []),
    ...(m?.gaps.length ? ["Verify:", ...m.gaps.map((g) => `• ${g}`), ""] : []),
    ...(nextAction(l) ? [`Next: ${nextAction(l)}`] : []),
  ].join("\n");
}

export default function LeadDetail({ lead, onUpdate, onRefreshed }: Props) {
  const [notes, setNotes] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setNotes(lead?.notes ?? "");
    setCopied(false);
  }, [lead?.id]); // eslint-disable-line react-hooks/exhaustive-deps -- reset only when the selection changes

  if (!lead) {
    return (
      <aside className="detail empty">
        <p>Select a lead to see the brief.</p>
      </aside>
    );
  }
  const c = lead.contractor;
  const ins = lead.insights;
  const pct = lead.match?.match_pct;
  const ringPct = pct ?? lead.score;
  const ringBand = pct != null ? band(pct) : lead.priority;

  return (
    <aside className="detail">
      <div className="detail-head">
        <div className={`ring ${ringBand}`} style={{ ["--pct" as string]: `${ringPct}%` }}>
          <span>{pct != null ? `${pct}%` : lead.score}</span>
        </div>
        <div>
          <h2>{c.name}</h2>
          <div className="muted">{[c.address, c.city, c.state].filter(Boolean).join(", ")}</div>
          {(c.legal_name && c.legal_name !== c.name) || c.years_in_business != null || c.employees ? (
            <div className="muted">
              {[
                c.legal_name && c.legal_name !== c.name ? c.legal_name : null,
                c.years_in_business != null ? `${c.years_in_business} yrs in business` : null,
                c.employees ? `${c.employees} employees` : null,
              ].filter(Boolean).join(" · ")}
            </div>
          ) : null}
          <div className="links">
            {c.phone && <a href={`tel:${c.phone}`}>{c.phone}</a>}
            {c.website && <a href={c.website} target="_blank" rel="noreferrer">Website</a>}
            {c.profile_url && <a href={c.profile_url} target="_blank" rel="noreferrer">GAF profile</a>}
            {c.google_place_id && (
              <a href={`https://search.google.com/local/reviews?placeid=${encodeURIComponent(c.google_place_id)}`} target="_blank" rel="noreferrer">
                Google reviews
              </a>
            )}
          </div>
        </div>
      </div>

      {nextAction(lead) && (
        <div className="next">
          <span>Next best action</span>
          {nextAction(lead)}
        </div>
      )}

      <h3>Manufacturer match</h3>
      <MatchPanel lead={lead} />

      {ins && (
        <>
          <h3>Talking points</h3>
          <ul>{ins.talking_points.map((t) => <li key={t}>{t}</li>)}</ul>
        </>
      )}

      <details className="fit">
        <summary>Rule-based fit score · {lead.score}/100</summary>
        <div className="factors">
          {lead.score_breakdown.map((f) => (
            <div key={f.factor} className="factor">
              <div className="factor-row">
                <span>{f.factor}</span>
                <span className="muted">{f.note} · {f.points}/{f.max_points}</span>
              </div>
              <div className="bar"><i style={{ width: `${(f.points / f.max_points) * 100}%` }} /></div>
            </div>
          ))}
        </div>
      </details>

      <h3>Your notes</h3>
      <div className="actions">
        <select value={lead.status} onChange={(e) => onUpdate(lead.id, { status: e.target.value as LeadStatus })} aria-label="Status">
          {STATUSES.map((s) => <option key={s}>{s}</option>)}
        </select>
        <button
          className="ghost"
          onClick={async () => {
            await navigator.clipboard.writeText(briefText(lead));
            setCopied(true);
          }}
        >
          {copied ? "Copied ✓" : "Copy brief"}
        </button>
      </div>
      <textarea
        value={notes}
        placeholder="Call notes, objections, follow-ups…"
        onChange={(e) => setNotes(e.target.value)}
        onBlur={() => notes !== (lead.notes ?? "") && onUpdate(lead.id, { notes })}
      />
      <div className="muted foot">
        Generated {new Date(lead.generated_at).toLocaleString()} · {lead.insights_model ?? "n/a"}
      </div>
      <RefreshBar key={lead.id} leadId={lead.id} onDone={onRefreshed} />
    </aside>
  );
}
