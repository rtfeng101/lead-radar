import { useEffect, useRef, useState } from "react";
import { ApiError, leadActivity, refreshLead } from "../api";
import type { Activity } from "../types";

interface Props {
  leadId: number;
  onDone: () => void; // called when a refresh finishes, so the list and panel show the new data
}

/** Refresh one contractor: re-fetch its data and re-grade if anything changed. Rendered with key={leadId}. */
export default function RefreshBar({ leadId, onDone }: Props) {
  const [act, setAct] = useState<Activity | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [justDone, setJustDone] = useState(false);
  const wasBusy = useRef(false);
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  const busy = act !== null && act.queued + act.running > 0;
  const running = act !== null && act.running > 0;

  // Pick up a refresh that is already in flight when the card is opened.
  useEffect(() => {
    let live = true;
    leadActivity(leadId).then((a) => live && setAct(a), () => {});
    return () => {
      live = false;
    };
  }, [leadId]);

  // Poll while work is queued or running.
  useEffect(() => {
    if (!busy) return;
    const id = setInterval(() => leadActivity(leadId).then(setAct, () => {}), 2000);
    return () => clearInterval(id);
  }, [busy, leadId]);

  // Busy -> idle means the refresh finished.
  useEffect(() => {
    if (busy) {
      wasBusy.current = true;
    } else if (wasBusy.current) {
      wasBusy.current = false;
      setJustDone(true);
      onDoneRef.current();
    }
  }, [busy]);

  const start = async () => {
    setError(null);
    setJustDone(false);
    try {
      await refreshLead(leadId);
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 409)) {
        setError(e instanceof Error ? e.message : "Could not start the refresh");
        return;
      }
    }
    setAct(await leadActivity(leadId));
  };

  let note: React.ReactNode = "Re-fetches this contractor’s GAF profile, permits and licenses. Re-grades only if something changed.";
  if (busy) note = running ? "Refreshing… usually under a minute." : "Queued. Waiting for a worker to pick it up.";
  else if (error) note = <span className="bad">{error}</span>;
  else if (act?.last_error) note = <span className="bad">Last refresh failed: {act.last_error}</span>;
  else if (justDone) note = <span className="good">Updated just now ✓</span>;

  return (
    <div className="refresh-bar">
      <button className="ghost" onClick={start} disabled={busy || act === null}>
        {busy ? "Refreshing…" : "↻ Refresh this contractor"}
      </button>
      <div className="muted">{note}</div>
    </div>
  );
}
