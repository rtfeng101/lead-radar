import type { Activity, Evidence, Filters, Lead, LeadList, LeadStatus, PipelineStatus } from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!res.ok) {
    const detail = await res.json().then((b) => b?.detail, () => undefined);
    throw new ApiError(res.status, typeof detail === "string" ? detail : `${res.status} ${res.statusText}`);
  }
  return res.json();
}

export function listLeads(f: Filters): Promise<LeadList> {
  const params = new URLSearchParams({ sort: f.sort, limit: "200" });
  if (f.priority) params.set("priority", f.priority);
  if (f.status) params.set("status", f.status);
  if (f.q) params.set("q", f.q);
  return request(`/api/leads?${params}`);
}

export function getEvidence(leadId: number): Promise<Evidence[]> {
  return request(`/api/leads/${leadId}/evidence`);
}

export function refreshLead(id: number): Promise<{ job_id: number }> {
  return request(`/api/leads/${id}/refresh`, { method: "POST" });
}

export function leadActivity(id: number): Promise<Activity> {
  return request(`/api/leads/${id}/activity`);
}

export function updateLead(
  id: number,
  patch: { status?: LeadStatus; notes?: string; assigned_to?: string },
): Promise<Lead> {
  return request(`/api/leads/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
}

export function startRun(zip_code: string, radius_mi = 25, refresh = false): Promise<{ job_id: number }> {
  return request("/api/pipeline/runs", { method: "POST", body: JSON.stringify({ zip_code, radius_mi, refresh }) });
}

export function pipelineStatus(): Promise<PipelineStatus> {
  return request("/api/pipeline/status");
}
