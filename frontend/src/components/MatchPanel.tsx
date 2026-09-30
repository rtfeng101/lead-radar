import { useEffect, useState } from "react";
import { getEvidence } from "../api";
import type { CriterionResult, Evidence, Lead } from "../types";
import { confidenceLabel } from "./LeadTable";

function chipLabel(ref: string, e?: Evidence): string {
  if (ref === "LISTING") return "GAF listing";
  if (!e) return ref;
  const p = e.payload;
  switch (e.kind) {
    case "review":
      return `Review · ${p.rating ?? "?"}★`;
    case "permit":
      return `Permit · ${p.issued ?? ""}`;
    case "permit_summary":
      return "Permit history";
    case "license":
      return `License · ${p.status ?? ""}`;
    case "license_search":
      return "License search";
    case "profile":
      return "GAF profile";
  }
}

function EvidenceCard({ ref_, e, lead }: { ref_: string; e?: Evidence; lead: Lead }) {
  if (ref_ === "LISTING") {
    const c = lead.contractor;
    return (
      <div className="ev">
        GAF directory: {c.cert_tier ?? "no tier"} · {c.rating ?? "no"} ★ ({c.review_count ?? 0} reviews) ·{" "}
        {c.years_in_business ?? "?"} yrs in business
      </div>
    );
  }
  if (!e) return <div className="ev muted">Evidence {ref_} is no longer available.</div>;
  const p = e.payload;
  return (
    <div className="ev">
      {e.kind === "review" && (
        <>
          <div className="muted">
            {p.platform} · {p.date}{p.author && <> · {p.author}.</>}
          </div>
          “{p.text}”
        </>
      )}
      {e.kind === "permit" && (
        <>
          <div className="muted">{p.work_type} · issued {p.issued}</div>{p.description}
        </>
      )}
      {e.kind === "permit_summary" &&
        (p.searched ? (
          <>
            {p.total_jobs === 0 ? (
              <>No NYC DOB permit filings found under {(p.searched_names ?? []).join(" / ") || "this name"} in the last {p.window_months} months</>
            ) : (
              <>
                {p.roofing_jobs} roofing jobs of {p.total_jobs} filed in the last {p.window_months} months
                {p.last_issued && <> · latest {p.last_issued}</>}
              </>
            )}
            <div className="muted">
              This is a lower bound, not proof of no work: many residential re-roofs are never filed with DOB, so check recent reviews too.
            </div>
            {p.match_note && <div className="muted">{p.match_note}</div>}
          </>
        ) : (
          <>Not searched: {p.reason}</>
        ))}
      {e.kind === "license" && (
        <>
          {p.license_type} {p.license_nbr} · <strong>{p.status}</strong> · expires {p.expires ?? "?"}
          <div className="muted">
            matched by {p.match?.method} (similarity {p.match?.similarity})
            {p.superseded_by_active && <> · older record, replaced by an active license</>}
          </div>
        </>
      )}
      {e.kind === "profile" && (
        <>
          {p.legal_name && <div><strong>{p.legal_name}</strong></div>}
          {p.address && <div className="muted">{p.address}</div>}
          In business since {p.in_business_since ?? "unknown"}
          {p.employees && <> · {p.employees} employees</>}
        </>
      )}
      {e.kind === "license_search" &&
        (p.searched ? <>Searched {p.dataset}; {p.found} found.</> : <>Not searched: {p.reason}</>)}
      {e.url && (
        <div className="muted"><a href={e.url} target="_blank" rel="noreferrer">View source</a></div>
      )}
    </div>
  );
}

function Criterion({ c, evidence, lead }: { c: CriterionResult; evidence: Map<string, Evidence>; lead: Lead }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="crit">
      <div className="factor-row">
        <span>
          {c.name} <span className="muted">· {c.weight}%</span>
        </span>
        <span className={c.score == null ? "muted" : ""}>{c.score == null ? "No data" : `${c.score}%`}</span>
      </div>
      <div className="bar">
        <i style={{ width: `${c.score ?? 0}%`, opacity: 0.35 + 0.65 * c.confidence }} />
      </div>
      <p className="rationale">{c.rationale}</p>
      <div className="chips-row">
        {c.evidence_ids.map((id) => (
          <button key={id} className={`ev-chip ${open === id ? "on" : ""}`} onClick={() => setOpen(open === id ? null : id)}>
            {chipLabel(id, evidence.get(id))}
          </button>
        ))}
        {c.uncited && <span className="ev-chip warn" title="The model gave a score but cited no verifiable evidence, so confidence was capped">uncited</span>}
        <span className="muted conf-note">{Math.round(c.confidence * 100)}% sure</span>
      </div>
      {open && <EvidenceCard ref_={open} e={evidence.get(open)} lead={lead} />}
    </div>
  );
}

// Dated reviews are the only signal that a contractor is working now: permit data lags and misses most
// residential re-roofs. Shown beside the permit count so a "0 filings" is never read as "no recent jobs".
function RecentWork({ evidence }: { evidence: Map<string, Evidence> }) {
  const rows = [...evidence.values()].filter((e) => e.kind === "review" && typeof e.payload.date === "string");
  if (rows.length === 0) return null;
  const dates = rows.map((e) => new Date(e.payload.date as string)).filter((d) => !Number.isNaN(d.getTime()));
  if (dates.length === 0) return null;
  const now = Date.now();
  const within = (days: number) => dates.filter((d) => now - d.getTime() <= days * 864e5).length;
  const newest = new Date(Math.max(...dates.map((d) => d.getTime())));
  const permit = [...evidence.values()].find((e) => e.kind === "permit_summary");
  const filed = permit?.payload.searched ? (permit.payload.roofing_jobs as number) : null;
  return (
    <div className="recent-work">
      <strong>Recent work</strong> · latest review {newest.toISOString().slice(0, 10)} · {within(180)} of {dates.length} sampled
      reviews in the last 6 months
      {filed !== null && <> · {filed} roofing permits filed with DOB</>}
    </div>
  );
}

export default function MatchPanel({ lead }: { lead: Lead }) {
  const [evidence, setEvidence] = useState<Map<string, Evidence>>(new Map());
  const m = lead.match;

  useEffect(() => {
    let live = true;
    getEvidence(lead.id)
      .then((rows) => live && setEvidence(new Map(rows.map((e) => [e.ref, e]))))
      .catch(() => live && setEvidence(new Map()));
    return () => {
      live = false;
    };
  }, [lead.id, m?.generated_at]);

  if (!m) {
    return <p className="muted">GAF match is still being computed. It appears once evidence has been collected and scored.</p>;
  }
  const synthetic = [...evidence.values()].some((e) => e.payload.synthetic);
  const conf = confidenceLabel(m.confidence);

  return (
    <>
      {synthetic && <div className="demo-note">Demo data: the reviews, permits and licenses below are synthetic.</div>}
      <div className="match-head">
        <div>
          <div className="big">{m.match_pct != null ? `${m.match_pct}%` : "—"}</div>
          <div className="muted">GAF match</div>
        </div>
        <div className={`conf-badge conf-${conf.toLowerCase()}`}>
          {conf} confidence · {Math.round(m.confidence * 100)}%
        </div>
      </div>
      <p className="summary">{m.summary}</p>
      <RecentWork evidence={evidence} />
      {m.criteria.map((c) => (
        <Criterion key={c.id} c={c} evidence={evidence} lead={lead} />
      ))}
      {m.gaps.length > 0 && (
        <>
          <h3>Verify before you call</h3>
          <ul className="risks">
            {m.gaps.map((g) => <li key={g}>{g}</li>)}
          </ul>
        </>
      )}
      <div className="muted foot">Scored by {m.model} · {new Date(m.generated_at).toLocaleString()}</div>
    </>
  );
}

