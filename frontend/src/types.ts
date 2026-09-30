// Mirrors backend/app/schemas.py

export type Priority = "hot" | "warm" | "cold";
export type LeadStatus = "new" | "reviewing" | "contacted" | "qualified" | "won" | "dismissed";
export const STATUSES: LeadStatus[] = ["new", "reviewing", "contacted", "qualified", "won", "dismissed"];

export interface Contractor {
  id: number;
  source: string;
  name: string;
  legal_name: string | null;
  employees: string | null;
  phone: string | null;
  website: string | null;
  address: string | null;
  city: string | null;
  state: string | null;
  zip_code: string | null;
  distance_mi: number | null;
  rating: number | null;
  review_count: number | null;
  cert_tier: string | null;
  years_in_business: number | null;
  services: string[];
  profile_url: string | null;
  google_place_id: string | null;
}

export interface ScoreFactor {
  factor: string;
  points: number;
  max_points: number;
  note: string;
}

export interface Insights {
  summary: string;
  talking_points: string[];
  risks: string[];
  next_best_action: string;
}

export interface CriterionResult {
  id: string;
  name: string;
  weight: number;
  score: number | null;
  confidence: number;
  rationale: string;
  evidence_ids: string[];
  uncited: boolean;
}

export interface Match {
  manufacturer: string;
  match_pct: number | null;
  confidence: number;
  criteria: CriterionResult[];
  summary: string;
  gaps: string[];
  next_action: string | null;
  model: string;
  generated_at: string;
}

export interface Evidence {
  ref: string;
  kind: "review" | "permit" | "permit_summary" | "license" | "license_search" | "profile";
  source: string;
  url: string | null;
  occurred_at: string | null;
  payload: Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  facts: Record<string, unknown> | null;
}

export interface Lead {
  id: number;
  score: number;
  priority: Priority;
  score_breakdown: ScoreFactor[];
  insights: Insights | null;
  insights_model: string | null;
  status: LeadStatus;
  assigned_to: string | null;
  notes: string | null;
  generated_at: string;
  updated_at: string;
  contractor: Contractor;
  match: Match | null;
}

export interface LeadList {
  items: Lead[];
  total: number;
}

export interface PipelineStatus {
  queued: number;
  running: number;
  done: number;
  failed: number;
}

export interface Activity {
  queued: number;
  running: number;
  last_error: string | null;
}

export type SortKey = "match" | "score" | "distance" | "reviews" | "recent";

export interface Filters {
  priority: Priority | "";
  status: LeadStatus | "";
  q: string;
  sort: SortKey;
}
