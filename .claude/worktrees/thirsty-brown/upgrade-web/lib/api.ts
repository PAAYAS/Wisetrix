/**
 * Thin fetch wrapper that targets the FastAPI service via the Next.js
 * rewrite at `/api/*` → `http://localhost:8000/*`.
 */

/**
 * Absolute base URL for SSE streams and long-running direct calls.
 *
 * EventSource MUST bypass the Next dev rewrites: the rewrites proxy gzips
 * responses for any browser that sends `Accept-Encoding: gzip` (which Chrome
 * always does), and Chrome's gzip decoder holds SSE events in a buffer until
 * the stream closes — making the UI appear frozen during long merges.
 *
 * Reads `NEXT_PUBLIC_API_BASE` (same env var the Next config uses); falls back
 * to localhost:8000 for dev. CORS is open on the FastAPI side.
 */
const STREAM_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

/** Send a structured log entry to the backend so it lands in wisetrix.log. */
function logToBackend(
  level: "debug" | "info" | "warn" | "error",
  message: string,
  context?: Record<string, unknown>,
): void {
  // Fire-and-forget — never let logging errors surface to the user
  fetch(`${STREAM_BASE}/log`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ level, message, context: context ?? {} }),
  }).catch(() => undefined);
}

/**
 * Extract a human-readable string from a FastAPI error response payload.
 *
 * FastAPI/Pydantic validation errors return `detail` as an array of objects
 * like `[{type, loc, msg, input, url}]`. Calling String() on an array of
 * objects produces "[object Object]", so we handle both shapes here.
 */
function extractDetail(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") return fallback;
  const p = payload as Record<string, unknown>;
  if (!("detail" in p)) return fallback;

  const detail = p.detail;

  if (typeof detail === "string") return detail;

  if (Array.isArray(detail)) {
    // Pydantic v2 validation error array — join the human-readable msg fields
    const messages = detail
      .map((d) => {
        if (typeof d === "string") return d;
        if (d && typeof d === "object") {
          const obj = d as Record<string, unknown>;
          const loc = Array.isArray(obj.loc) ? obj.loc.join(" → ") : "";
          const msg = typeof obj.msg === "string" ? obj.msg : JSON.stringify(obj);
          return loc ? `${loc}: ${msg}` : msg;
        }
        return JSON.stringify(d);
      })
      .filter(Boolean)
      .join("; ");
    return messages || fallback;
  }

  return String(detail) || fallback;
}

export class ApiError extends Error {
  status: number;
  payload: unknown;
  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

export type SourceType = "git" | "local";
export type TargetType = "artifactory" | "local";
export type BaselineType = "artifactory" | "local" | "none";

export interface ProjectConfig {
  source_type: SourceType;
  target_type: TargetType;
  baseline_type: BaselineType;

  // source
  git_url?: string | null;
  git_branch?: string | null;
  source_subpath?: string | null;
  source_root?: string | null;

  // target
  artifactory_url?: string | null;
  target_version?: string | null;
  target_system?: string | null;

  // baseline
  baseline_url?: string | null;
  baseline_version?: string | null;
  baseline_system?: string | null;

  // misc
  merge_output_dir?: string | null;
  jira_project_url?: string | null;
}

export interface ProjectSummary {
  id: string;
  config: ProjectConfig;
}

export interface TestConnectionResponse {
  success: boolean;
  message: string;
  details?: Record<string, unknown> | null;
}

export interface ArtifactInfo {
  bucket: string;
  category: string;
  subcategory: string;
  name: string;
  rel_path: string;
  source_rel: string;
  abs_path: string;
}

export interface ArtifactListResponse {
  project_id: string;
  source_root: string;
  target_system: string;
  baseline_system: string;
  git_metadata: Record<string, unknown> | null;
  buckets: string[];
  artifacts: ArtifactInfo[];
  count: number;
}

export type Decision = "Merge" | "Retain" | "Remove" | "ERROR";
export type RiskLevel = "HIGH" | "MEDIUM" | "LOW";

export interface ComparisonResult {
  bucket: string;
  category: string;
  name: string;
  rel_path: string;
  source_rel: string;
  decision: Decision;
  file_count?: number;
  file_decisions?: Record<string, string>;
  target_exists?: boolean;
  target_path?: string;
  analysis?: string;
  engine?: string;
  risk_level?: RiskLevel;
  risk_score?: number;
  decision_note?: string;
  error?: string;
  decided_at?: string;
}

export type ComparisonMap = Record<string, ComparisonResult>;

export type QualityVerdict = "PASS" | "WARN" | "FAIL";

export interface QualityFinding {
  severity: "ERROR" | "WARNING" | "INFO";
  category: string;
  file: string;
  line: number | null;
  message: string;
}

export interface QualityResult {
  verdict: QualityVerdict;
  findings?: QualityFinding[];
  blocking?: boolean;
}

export interface MergeRecord {
  bucket: string;
  rel_path: string;
  merged_at: string;
  files: string[];
  explanation: string;
  diff_generated: boolean;
  diff_error: string | null;
  out_dir: string;
  quality_result: QualityResult | null;
}

export interface MergeListResponse {
  pending: ComparisonMap;
  done: Record<string, MergeRecord>;
  pending_count: number;
  done_count: number;
}

export interface SummaryMetrics {
  total_artifacts: number;
  decision_counts: Record<string, number>;
  risk_counts: Record<string, number>;
  merged_count: number;
  verdict_counts: Record<string, number>;
}

export interface NarrativeRecord {
  generated_at?: string;
  content?: string;
}

export interface SummaryResponse {
  metrics: SummaryMetrics;
  narrative: NarrativeRecord;
}

export interface DiffResponse {
  key: string;
  bucket: string;
  rel_path: string;
  files: string[];
  aldi: Record<string, string>;
  system: Record<string, string>;
  merged: Record<string, string>;
  has_merge: boolean;
  target_label: string;
}

export interface ReviewFinding {
  severity: "ERROR" | "WARNING" | "INFO" | string;
  category: string;
  file: string;
  message: string;
}

export interface ReviewResponse {
  verdict: QualityVerdict | string;
  summary?: string;
  findings?: ReviewFinding[];
}

export interface JiraStatus {
  enabled: boolean;
  base_url: string;
  project_key: string;
  epic_key: string;
  subtasks: Record<string, string>;
  started_at: string;
  finalized_at: string;
}

export interface JiraIssueRecord {
  key: string;
  summary: string;
  status: string;
  issue_type: string;
  assignee?: string | null;
  url: string;
}

export interface JiraIssuesResponse {
  enabled: boolean;
  base_url?: string;
  epic_key?: string;
  issues: JiraIssueRecord[];
}

export interface JiraTestResponse {
  success: boolean;
  message: string;
  user?: string | null;
}

/** ticket_key → "git" | "keyword" for every artifact that has a match */
export type JiraMatchSources = Record<string, Record<string, "git" | "keyword">>;

export interface JiraMatchResponse {
  matched: Record<string, string>;
  sources?: JiraMatchSources;
  count: number;
  total?: number;
  git_enriched?: number;
}

/**
 * Long-running calls (anything backed by a Claude SDK invocation, e.g.,
 * narrative generation, JIRA match) must skip the Next dev rewrites: the
 * proxy resets sockets at ~30s, while these calls regularly take 60–180s.
 * Hits FastAPI directly via STREAM_BASE.
 */
async function requestDirect<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${STREAM_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
    ...init,
  });
  if (!res.ok) {
    let payload: unknown = null;
    try {
      payload = await res.json();
    } catch {
      /* fall through */
    }
    const detail = extractDetail(payload, `API ${res.status} on ${path}`);
    logToBackend("error", `[requestDirect] ${res.status} ${path}`, {
      status: res.status,
      detail,
      payload,
    });
    throw new ApiError(detail, res.status, payload);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
    ...init,
  });

  if (!res.ok) {
    let payload: unknown = null;
    try {
      payload = await res.json();
    } catch {
      // fall through
    }
    const detail = extractDetail(payload, `API ${res.status} on ${path}`);
    logToBackend("error", `[request] ${res.status} ${path}`, {
      status: res.status,
      detail,
      payload,
    });
    throw new ApiError(detail, res.status, payload);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  health: () =>
    request<{ status: string; service: string; time: string }>("/health"),

  // Projects
  listProjects: () => request<ProjectSummary[]>("/projects"),
  getProject: (id: string) =>
    request<ProjectSummary>(`/projects/${encodeURIComponent(id)}`),
  createProject: (id: string, config: ProjectConfig) =>
    request<ProjectSummary>("/projects", {
      method: "POST",
      body: JSON.stringify({ id, config }),
    }),
  updateProject: (id: string, config: ProjectConfig) =>
    request<ProjectSummary>(`/projects/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),
  deleteProject: (id: string) =>
    request<void>(`/projects/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  // Scan & Compare
  // Uses requestDirect (hits localhost:8000 directly) to bypass the Next.js
  // dev-proxy ~30s socket timeout — artifact resolution can take 60-120s on
  // a cold cache when pulling from git/artifactory.
  listArtifacts: (id: string) =>
    requestDirect<ArtifactListResponse>(
      `/projects/${encodeURIComponent(id)}/artifacts`,
    ),

  /**
   * URL for the artifact-load SSE stream.
   * Emits phase-progress events during git clone / Artifactory download so the
   * UI can show what is happening on a cold cache (new project / new version).
   * Bypasses the Next.js dev-proxy (same reason as compareStreamUrl).
   */
  artifactsStreamUrl: (id: string) =>
    `${STREAM_BASE}/projects/${encodeURIComponent(id)}/artifacts/stream`,
  getComparison: (id: string) =>
    request<ComparisonMap>(`/projects/${encodeURIComponent(id)}/comparison`),

  /** URL for the SSE compare stream — pass to `new EventSource(url)`. */
  compareStreamUrl: (id: string) =>
    `${STREAM_BASE}/projects/${encodeURIComponent(id)}/compare/stream`,

  // Merges
  listMerges: (id: string) =>
    request<MergeListResponse>(`/projects/${encodeURIComponent(id)}/merges`),
  mergeOne: (id: string, key: string) =>
    requestDirect<MergeRecord>(
      `/projects/${encodeURIComponent(id)}/merges/${encodeURIComponent(key)}`,
      { method: "POST" },
    ),
  mergeAllStreamUrl: (id: string) =>
    `${STREAM_BASE}/projects/${encodeURIComponent(id)}/merges/stream`,
  mergeOneStreamUrl: (id: string, key: string) =>
    `${STREAM_BASE}/projects/${encodeURIComponent(id)}/merges/${encodeURIComponent(key)}/stream`,
  mergeDownloadAllUrl: (id: string) =>
    `/api/projects/${encodeURIComponent(id)}/merges/download`,
  mergeDownloadOneUrl: (id: string, key: string) =>
    `/api/projects/${encodeURIComponent(id)}/merges/${encodeURIComponent(key)}/download`,

  // Summary + Report
  getSummary: (id: string) =>
    request<SummaryResponse>(`/projects/${encodeURIComponent(id)}/summary`),
  regenerateNarrative: (id: string) =>
    requestDirect<NarrativeRecord>(
      `/projects/${encodeURIComponent(id)}/summary/narrative`,
      { method: "POST" },
    ),
  buildReport: (id: string) =>
    requestDirect<{ path: string; bytes: number }>(
      `/projects/${encodeURIComponent(id)}/report`,
      { method: "POST" },
    ),
  reportPdfUrl: (id: string) =>
    `/api/projects/${encodeURIComponent(id)}/report/pdf`,
  reportMarkdownUrl: (id: string) =>
    `/api/projects/${encodeURIComponent(id)}/report/markdown`,

  // Diff + Review
  getDiff: (id: string, key: string) =>
    requestDirect<DiffResponse>(
      `/projects/${encodeURIComponent(id)}/diff/${encodeURIComponent(key)}`,
    ),
  runReview: (id: string, key: string) =>
    requestDirect<ReviewResponse>(
      `/projects/${encodeURIComponent(id)}/review/${encodeURIComponent(key)}`,
      { method: "POST" },
    ),

  // JIRA
  jiraStatus: (id: string) =>
    request<JiraStatus>(`/projects/${encodeURIComponent(id)}/jira/status`),
  jiraTest: (id: string) =>
    request<JiraTestResponse>(`/projects/${encodeURIComponent(id)}/jira/test`, {
      method: "POST",
    }),
  jiraIssues: (id: string) =>
    request<JiraIssuesResponse>(`/projects/${encodeURIComponent(id)}/jira/issues`),
  jiraStart: (id: string, metadata: Record<string, unknown> = {}) =>
    requestDirect<{ epic_key: string; summary: string; status: string; url: string }>(
      `/projects/${encodeURIComponent(id)}/jira/start`,
      { method: "POST", body: JSON.stringify({ metadata }) },
    ),
  jiraSync: (id: string) =>
    requestDirect<{ synced: number; issues: JiraIssueRecord[] }>(
      `/projects/${encodeURIComponent(id)}/jira/sync`,
      { method: "POST" },
    ),
  jiraMatch: (id: string, artifact_keys?: string[]) =>
    requestDirect<JiraMatchResponse>(`/projects/${encodeURIComponent(id)}/jira/match`, {
      method: "POST",
      body: JSON.stringify({ artifact_keys: artifact_keys ?? null }),
    }),
  jiraMatchCached: (id: string) =>
    request<JiraMatchResponse>(`/projects/${encodeURIComponent(id)}/jira/match`),
  jiraFinalize: (id: string) =>
    request<{ finalized: boolean; epic_key: string }>(
      `/projects/${encodeURIComponent(id)}/jira/finalize`,
      { method: "POST" },
    ),

  // Provider tests
  testGit: (url: string) =>
    requestDirect<TestConnectionResponse>("/providers/git/test", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
  testArtifactory: (url: string) =>
    requestDirect<TestConnectionResponse>("/providers/artifactory/test", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
};

export const emptyProject = (): ProjectConfig => ({
  source_type: "git",
  target_type: "artifactory",
  baseline_type: "none",
  git_url: "",
  git_branch: "main",
  source_subpath: "client_delivery/src/main/resources/app_root/repos",
  source_root: "",
  artifactory_url: "",
  target_version: "26.2",
  target_system: "",
  baseline_url: "",
  baseline_version: "24.4.11",
  baseline_system: "",
  jira_project_url: "",
});
