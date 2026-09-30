import type { Lead, Priority } from "../types";

interface Props {
  leads: Lead[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}

export const band = (pct: number): Priority => (pct >= 75 ? "hot" : pct >= 50 ? "warm" : "cold");
export const confidenceLabel = (c: number) => (c >= 0.75 ? "High" : c >= 0.5 ? "Medium" : "Low");

export default function LeadTable({ leads, selectedId, onSelect }: Props) {
  if (leads.length === 0) {
    return (
      <section className="list empty">
        <p>No leads yet. Enter a ZIP code above and click <strong>Find contractors</strong>.</p>
        <p className="muted">Make sure a worker is running: <code>python -m app.pipeline.worker</code></p>
      </section>
    );
  }
  return (
    <section className="list">
      <table>
        <thead>
          <tr>
            <th title="How well the contractor fits the manufacturer's program, scored from reviews, permits and licenses">Match</th>
            <th>Contractor</th>
            <th>Tier</th>
            <th className="num">Rating</th>
            <th className="num">Dist.</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {leads.map((l) => {
            const c = l.contractor;
            const m = l.match;
            return (
              <tr key={l.id} className={l.id === selectedId ? "selected" : ""} onClick={() => onSelect(l.id)}>
                <td>
                  {m?.match_pct != null ? (
                    <>
                      <span className={`score ${band(m.match_pct)}`}>{m.match_pct}%</span>
                      <div className={`conf conf-${confidenceLabel(m.confidence).toLowerCase()}`}>
                        {confidenceLabel(m.confidence)} conf.
                      </div>
                    </>
                  ) : (
                    <>
                      <span className="score none">—</span>
                      <div className="conf">{m ? "no data" : "pending"}</div>
                    </>
                  )}
                </td>
                <td>
                  <div className="name">{c.name}</div>
                  <div className="muted">{[c.city, c.state].filter(Boolean).join(", ")}</div>
                </td>
                <td>{c.cert_tier ?? "—"}</td>
                <td className="num">
                  {c.rating != null ? `${c.rating.toFixed(1)} ★` : "—"}
                  <div className="muted">{c.review_count ?? 0} reviews</div>
                </td>
                <td className="num">{c.distance_mi != null ? `${c.distance_mi.toFixed(1)} mi` : "—"}</td>
                <td>
                  <span className={`status ${l.status}`}>{l.status}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
