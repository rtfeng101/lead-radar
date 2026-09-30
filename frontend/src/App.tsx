import { useCallback, useEffect, useRef, useState } from "react";
import { listLeads, pipelineStatus, startRun, updateLead } from "./api";
import LeadDetail from "./components/LeadDetail";
import LeadTable from "./components/LeadTable";
import type { Filters, Lead, PipelineStatus, Priority } from "./types";

const DEFAULT_ZIP = "10013";
const PRIORITIES: Priority[] = ["hot", "warm", "cold"];

export default function App() {
  const [filters, setFilters] = useState<Filters>({ priority: "", status: "", q: "", sort: "match" });
  const [leads, setLeads] = useState<Lead[]>([]);
  const [total, setTotal] = useState(0);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [zip, setZip] = useState(DEFAULT_ZIP);
  const [pipeline, setPipeline] = useState<PipelineStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const wasBusy = useRef(false);

  const load = useCallback(async () => {
    try {
      const res = await listLeads(filters);
      setLeads(res.items);
      setTotal(res.total);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load leads");
    }
  }, [filters]);

  // Debounce so typing in the search box doesn't fire a request per keystroke.
  useEffect(() => {
    const t = setTimeout(load, filters.q ? 250 : 0);
    return () => clearTimeout(t);
  }, [load, filters.q]);

  // Poll pipeline status; refresh the list while work is in flight and once more when it drains.
  useEffect(() => {
    const tick = async () => {
      try {
        const s = await pipelineStatus();
        setPipeline(s);
        const busy = s.queued + s.running > 0;
        if (busy || wasBusy.current) load();
        wasBusy.current = busy;
      } catch {
        /* the list request surfaces connectivity errors */
      }
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => clearInterval(id);
  }, [load]);

  // Open with the top lead selected (like a mail client) rather than an empty detail pane.
  useEffect(() => {
    if (leads.length > 0 && !leads.some((l) => l.id === selectedId)) setSelectedId(leads[0].id);
  }, [leads, selectedId]);

  const selected = leads.find((l) => l.id === selectedId) ?? null;

  // j / k to move through the list, like a mail client.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement).tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (e.key !== "j" && e.key !== "k") return;
      const i = leads.findIndex((l) => l.id === selectedId);
      const next = leads[Math.max(0, Math.min(leads.length - 1, i + (e.key === "j" ? 1 : -1)))];
      if (next) setSelectedId(next.id);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [leads, selectedId]);

  const onUpdate = async (id: number, patch: Parameters<typeof updateLead>[1]) => {
    const updated = await updateLead(id, patch);
    setLeads((prev) => prev.map((l) => (l.id === id ? updated : l)));
  };

  const busy = pipeline ? pipeline.queued + pipeline.running > 0 : false;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" />
          Lead Radar
        </div>
        <form
          className="run"
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await startRun(zip);
            } catch (err) {
              setError(err instanceof Error ? err.message : "Failed to start run");
            }
          }}
        >
          <input value={zip} onChange={(e) => setZip(e.target.value)} inputMode="numeric" maxLength={5} aria-label="ZIP code" />
          <button type="submit" disabled={!/^\d{5}$/.test(zip)}>
            Find contractors
          </button>
          <button
            type="button"
            className="ghost"
            disabled={!/^\d{5}$/.test(zip)}
            title="Ignore saved data and re-fetch every source for every contractor"
            onClick={async () => {
              const ok = window.confirm(
                `Re-fetch every source for all contractors near ${zip}?\n\n` +
                  "This reloads each GAF profile page and permit lookup, so it takes a few minutes. " +
                  "Grades are only re-run where the data actually changed.",
              );
              if (!ok) return;
              try {
                await startRun(zip, 25, true);
              } catch (err) {
                setError(err instanceof Error ? err.message : "Failed to start refresh");
              }
            }}
          >
            Re-fetch all
          </button>
          <span className={`pill ${busy ? "busy" : ""} ${pipeline?.failed ? "warn" : ""}`}>
            {busy ? `Working · ${pipeline!.queued + pipeline!.running} jobs` : pipeline?.failed ? `${pipeline.failed} failed jobs` : "Idle"}
          </span>
        </form>
      </header>

      <div className="toolbar">
        <div className="chips" role="group" aria-label="Priority">
          <button className={!filters.priority ? "chip on" : "chip"} onClick={() => setFilters({ ...filters, priority: "" })}>
            All
          </button>
          {PRIORITIES.map((p) => (
            <button
              key={p}
              className={`chip ${p} ${filters.priority === p ? "on" : ""}`}
              onClick={() => setFilters({ ...filters, priority: filters.priority === p ? "" : p })}
            >
              {p}
            </button>
          ))}
        </div>
        <input
          className="search"
          placeholder="Search name or city…"
          value={filters.q}
          onChange={(e) => setFilters({ ...filters, q: e.target.value })}
        />
        <select value={filters.sort} onChange={(e) => setFilters({ ...filters, sort: e.target.value as Filters["sort"] })} aria-label="Sort">
          <option value="match">Best match</option>
          <option value="score">Best fit</option>
          <option value="distance">Nearest</option>
          <option value="reviews">Most reviews</option>
          <option value="recent">Recently updated</option>
        </select>
        <span className="count">{total} leads · <kbd>j</kbd>/<kbd>k</kbd> to navigate</span>
      </div>

      {error && <div className="banner">Couldn’t reach the API: {error}</div>}

      <main className="split">
        <LeadTable leads={leads} selectedId={selectedId} onSelect={setSelectedId} />
        <LeadDetail lead={selected} onUpdate={onUpdate} onRefreshed={load} />
      </main>
    </div>
  );
}
