import type { SessionInfo, WsEvent } from "./types";

declare const __COWORKER_DEV_TOKEN__: string;

// Endpoint resolution order: runtime-injected globals (Tauri sets `window.__COWORKER_HTTP__`
// for its dynamically-chosen sidecar port) 鈫?Vite env 鈫?the 127.0.0.1:8765 dev default. This
// keeps a single codebase: browser `npm run dev` hits 8765; the desktop shell hits its sidecar.
const httpBase = (): string =>
  (globalThis as any).__COWORKER_HTTP__ ||
  (import.meta as any).env?.VITE_COWORKER_HTTP ||
  "http://127.0.0.1:8765";
const wsBase = (): string =>
  (globalThis as any).__COWORKER_WS__ ||
  (import.meta as any).env?.VITE_COWORKER_WS ||
  "ws://127.0.0.1:8765";
const apiToken = (): string =>
  (globalThis as any).__COWORKER_API_TOKEN__ ||
  (import.meta as any).env?.VITE_COWORKER_API_TOKEN ||
  (typeof __COWORKER_DEV_TOKEN__ === "string" ? __COWORKER_DEV_TOKEN__ : "");

// All local REST calls pass through this module, so a module-local wrapper applies launch
// authentication without asking every endpoint helper to remember the security header.
const fetch = (
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> => {
  const headers = new Headers(init.headers);
  const token = apiToken();
  if (token) headers.set("X-QunWork-Token", token);
  return globalThis.fetch(input, { ...init, headers });
};

// The authenticated fetch: every api.ts helper goes through the module-local
// `fetch` above; components that hand-write requests must use this instead of
// the global fetch (which lacks the launch token and 401s on the desktop).
export const authedFetch = fetch;

const openWebSocket = (url: string): WebSocket => {
  const token = apiToken();
  return token
    ? new WebSocket(url, ["qunwork", token])
    : new WebSocket(url);
};

/**
 * 401/5xx 绛夊紓甯稿搷搴旀鍓嶈褰撴暟鎹敤锛堥敊璇綋 {"error":...} 鐩存帴杩?state锛夛紝
 * 缁勪欢娣卞鐨?.includes/[0]/.map 璁块棶 undefined 瀛楁 鈫?鏁撮〉 error boundary
 * 鐧藉睆锛堛€屾棤娉曡繛鎺ユ湰鍦板紩鎿庛€嶅満鏅殑閬楃暀娓叉煋鎶ラ敊锛?026-09-02 瀹氫綅锛夈€?
 * apiFetch 缁熶竴鍦ㄤ紶杈撳眰鎷︽埅锛氶潪 2xx 鎶?ApiError锛岃皟鐢ㄦ柟鐨?.catch(() => {})
 * 鍏滃簳鐢熸晥锛岄敊璇綋姘歌繙涓嶈繘娓叉煋鏁版嵁銆?38 澶?fetch 涓?212 澶勭粺涓€璧版鍖呰銆?
 */
export class ApiError extends Error {
  status: number;
  path: string;
  constructor(status: number, path: string) {
    super(`API ${status}: ${path}`);
    this.status = status;
    this.path = path;
  }
}

async function apiFetch(url: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(url, init);
  if (!res.ok) throw new ApiError(res.status, url.replace(/^https?:\/\/[^/]+/, ""));
  return res;
}

export interface Health {
  status: string;
  default_workspace: string | null;
  model: string;
}

export interface RecentWorkspace {
  path: string;
  name: string;
  exists: boolean;
}

export interface WorkspaceCommandTrust {
  workspace: string;
  requested_commands: string[];
  trusted: boolean;
  required: boolean;
  exists?: boolean;
}

export async function getHealth(): Promise<Health> {
  const res = await apiFetch(`${httpBase()}/v1/health`);
  return res.json();
}

export async function getRecentWorkspaces(): Promise<RecentWorkspace[]> {
  const res = await apiFetch(`${httpBase()}/v1/workspaces/recent`);
  return (await res.json()).workspaces ?? [];
}

/** Ask the LOCAL sidecar to open the OS folder picker 鈥?the browser GUI can't obtain absolute
 * paths from web file dialogs. Blocks until the user picks or cancels; null on cancel/unavailable. */
export async function pickFolderViaServer(): Promise<string | null> {
  try {
    const res = await apiFetch(`${httpBase()}/v1/workspaces/pick`, { method: "POST" });
    const d = await res.json();
    return d.ok && d.path ? d.path : null;
  } catch {
    return null;
  }
}

export async function openWorkspace(
  path: string,
  create = false,
): Promise<{
  path: string;
  ok: boolean;
  error?: string;
  git_branch?: string | null;
  command_trust?: WorkspaceCommandTrust;
}> {
  const res = await apiFetch(`${httpBase()}/v1/workspaces/open`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, create }),
  });
  return res.json();
}

export async function getTrustedWorkspaces(): Promise<WorkspaceCommandTrust[]> {
  const res = await apiFetch(`${httpBase()}/v1/workspaces/trusted`);
  return (await res.json()).workspaces ?? [];
}

export async function setWorkspaceTrusted(
  path: string,
  trusted: boolean,
): Promise<{ ok: boolean; error?: string } & WorkspaceCommandTrust> {
  const res = await apiFetch(`${httpBase()}/v1/workspaces/trust`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, trusted }),
  });
  return res.json();
}

export async function getSessions(workspace?: string): Promise<SessionInfo[]> {
  const q = workspace ? `?workspace=${encodeURIComponent(workspace)}` : "";
  const res = await apiFetch(`${httpBase()}/v1/sessions${q}`);
  return (await res.json()).sessions ?? [];
}

// A structured connector-delivered inbound message (搂3.1). Attached to the user message it framed,
// for display only 鈥?the model still sees the framed `content`; this drives the ConnectorMessageCard.
export interface MessageSource {
  connector: string; // platform id, e.g. "slack"
  kind: "channel" | "dm";
  channel_id: string; // e.g. "C0BD7KZ1AH5"
  channel_name: string; // resolved; may equal the id (e.g. "#ocw-test")
  sender_id: string;
  sender_name: string; // resolved; may equal the id
  ts: number; // epoch seconds
  text: string; // the RAW message (what the card shows)
}

// A transcript message from GET /v1/sessions/{id}/messages. Kept permissive (open shape) because
// itemsFromMessages reads several role-specific fields; `source` is the optional connector sidecar.
export interface ConversationMessage {
  role: string;
  content?: any;
  tool_calls?: any[];
  tool_call_id?: string;
  source?: MessageSource;
  [key: string]: any;
}

export async function getSessionMessages(sessionId: string): Promise<ConversationMessage[]> {
  const res = await apiFetch(`${httpBase()}/v1/sessions/${sessionId}/messages`);
  return (await res.json()).messages ?? [];
}

export async function renameSession(sessionId: string, title: string): Promise<{ ok: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  return res.json();
}

export async function setSessionFlags(
  sessionId: string,
  flags: { pinned?: boolean; archived?: boolean },
): Promise<{ ok: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(flags),
  });
  return res.json();
}

export async function deleteSession(sessionId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  return res.json();
}

export interface ArtifactInfo {
  path: string; // workspace-relative (the display/API identifier)
  abs_path?: string; // absolute 鈥?what "Copy path" copies
  name: string;
  kind: "markdown" | "html" | "image" | "code" | "text" | string;
  size: number;
  modified_at: number;
}

export interface ArtifactContent {
  ok: boolean;
  error?: string;
  path: string;
  kind: string;
  content?: string;
  data_url?: string;
  truncated?: boolean;
}

export async function getArtifacts(sessionId: string): Promise<ArtifactInfo[]> {
  const res = await apiFetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/artifacts`);
  return (await res.json()).artifacts ?? [];
}

export async function readArtifact(sessionId: string, path: string): Promise<ArtifactContent> {
  const q = new URLSearchParams({ path });
  const res = await apiFetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/artifacts/read?${q.toString()}`);
  return res.json();
}

/** Show the artifact in the OS file manager ("reveal") or open it with its default app ("open"). */
export async function revealArtifact(
  sessionId: string,
  path: string,
  mode: "reveal" | "open" = "reveal",
): Promise<{ ok: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/artifacts/reveal`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, mode }),
  });
  return res.json();
}

// -- session roots (orphan Cowork: scratch + added folders) -------------------
export interface RootInfo {
  path: string;
  writable: boolean;
  label: string;
  primary: boolean;
  exists: boolean;
}

export async function getRoots(sessionId: string): Promise<RootInfo[]> {
  const res = await apiFetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/roots`);
  return (await res.json()).roots ?? [];
}

export async function addRoot(
  sessionId: string,
  path: string,
  writable: boolean,
): Promise<{ ok: boolean; error?: string; roots?: RootInfo[] }> {
  const res = await apiFetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/roots`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, writable }),
  });
  return res.json();
}

export async function removeRoot(
  sessionId: string,
  path: string,
): Promise<{ ok: boolean; error?: string; roots?: RootInfo[] }> {
  const q = new URLSearchParams({ path });
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/roots?${q.toString()}`,
    { method: "DELETE" },
  );
  return res.json();
}

// -- MCP servers --------------------------------------------------------------
export interface McpServer {
  name: string;
  enabled: boolean;
  transport: string;
  requires_approval: boolean;
  // "connected" | "configured" | "disabled" | and for auth:"oauth" servers:
  // "needs_auth" (no tokens yet) | "authorizing" (browser sign-in in flight)
  status: string;
  auth?: "oauth" | null;
  last_error?: string | null;
  tool_count: number | null;
  config: Record<string, any>;
}

export async function getMcpServers(): Promise<McpServer[]> {
  const res = await apiFetch(`${httpBase()}/v1/mcp`);
  return (await res.json()).servers ?? [];
}

export async function addMcpServer(name: string, config: Record<string, any>) {
  const res = await apiFetch(`${httpBase()}/v1/mcp`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, config }),
  });
  return res.json();
}

export async function patchMcpServer(name: string, changes: Record<string, any>) {
  const res = await apiFetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  return res.json();
}

export async function deleteMcpServer(name: string) {
  const res = await apiFetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}`, { method: "DELETE" });
  return res.json();
}

export async function getMcpTools(
  name: string,
): Promise<{ ok: boolean; error?: string; tools: { name: string; description: string }[] }> {
  const res = await apiFetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}/tools`);
  return res.json();
}

export async function reloadMcp() {
  const res = await apiFetch(`${httpBase()}/v1/mcp/reload`, { method: "POST" });
  return res.json();
}

/** Connect one MCP server now. For OAuth servers this opens the system browser;
 * poll getMcpServers() for the status flip (authorizing 鈫?connected / needs_auth). */
export async function connectMcp(name: string): Promise<{ ok: boolean; started?: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}/connect`, {
    method: "POST",
  });
  return res.json();
}

/** Drop the connection and forget the stored OAuth tokens. */
export async function signoutMcp(name: string): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}/signout`, {
    method: "POST",
  });
  return res.json();
}

// -- connectors ---------------------------------------------------------------
export interface ConnectorField {
  key: string;
  label: string;
  secret: boolean;
  required: boolean;
  help: string;
  placeholder: string;
}

// A message from a sender not (yet) on the allow-list 鈥?parked instead of dropped (搂19).
export interface ParkedMessage {
  id: string;
  platform: string;
  chat_id: string;
  chat_name: string | null;
  user_id: string;
  user_name: string | null;
  chat_type: string;
  text: string;
  ts: number;
  team_id?: string | null; // workspace (managed Slack relay); null on manual Socket Mode
}

// One connected Slack workspace (managed relay is multi-workspace; ids are workspace-scoped,
// so each workspace carries its OWN allow-list).
export interface SlackWorkspace {
  team_id: string;
  account: string;
  domain?: string; // slack.com subdomain 鈥?unique even when display names collide
  allowed_users: string[];
  allow_all: boolean;
  allowed_user_names?: Record<string, string | null>;
  // Who installed this workspace (authed_user) 鈥?pre-added to the allow-list on
  // connect (UX-027); the GUI marks their chip "you" and keys the setup card copy.
  installer_user_id?: string;
  installer_name?: string;
}

// One connected GitHub App installation (managed relay is multi-installation;
// sender logins are global but each installation keeps its OWN allow-list).
export interface GithubInstallation {
  installation_id: string;
  account_login: string; // the org/user the App is installed on
  account_type: string; // "Organization" | "User"
  repo_selection: string; // "all" | "selected"
  github_login: string; // the connecting user's own login
  allowed_users: string[]; // sender logins allowed to trigger work
  allow_all: boolean;
}

// One connected HubSpot portal (multi-portal: `hubspot:portal:<hub_id>` profiles).
export interface HubSpotPortal {
  hub_id: string;
  name: string;
  sandbox: boolean;
  default: boolean;
  managed: boolean;
  access: "read" | "write" | ""; // consent tier granted ("" = manual token, unknown)
}

// One connected Google account (multi-account: `gmail:account:<email>` /
// `google_calendar:account:<email>` profiles 鈥?same shape for both).
export interface GmailAccount {
  email: string;
  default: boolean;
  managed: boolean;
  scopes: string;
  needs_reauth: boolean;
}

// "Never show agents" 鈥?enforced locally in the tool layer; agents see silent
// omissions, the user sees counts on tool cards + Activity rows.
export interface GmailFilters {
  senders: string[];
  labels: string[];
}

// One account of a generic multi-account connector (`<name>:account:<id>`
// profiles 鈥?Notion workspaces, PostHog projects, 鈥?. Gmail/Calendar predate
// the generic layer and keep their email-keyed shape above.
export interface AccountRow {
  account_id: string;
  name: string; // display identity captured at connect (workspace name, email, 鈥?
  default: boolean;
  managed: boolean;
}

export interface Connector {
  name: string;
  title: string;
  icon: string;
  blurb: string;
  // Pre-connect detail page copy (UX-DECISIONS 搂38): optional About paragraph
  // (empty 鈫?group omitted) + honest Access bullets.
  about?: string;
  access?: string[];
  auth: string;
  two_way: boolean;
  // Chat-platform capability, narrower than two_way: sessions can subscribe to channels.
  channels: boolean;
  available: boolean;
  fields: ConnectorField[];
  instructions: string[];
  connected: boolean;
  account: string | null;
  enabled: boolean;
  brand_color: string; // hex brand color, e.g. "#611f69" (fallback gray "#6b7280")
  logo: string; // stable logo id keyed into the frontend registry (empty 鈫?fallback glyph)
  aliases?: string[]; // extra typeahead terms ("calendar" surfaces Outlook)
  mcp?: boolean; // MCP-backed one-click (vendor-hosted MCP + local OAuth 鈥?no cloud sign-in)
  allowed_users: string[]; // the allow-list (managed inline in the Connectors tab)
  allowed_user_names?: Record<string, string | null>; // id 鈫?display name (people directory)
  recent?: RecentSender[]; // recently-seen senders on a connected two-way connector
  unauthorized?: ParkedMessage[]; // parked messages from unallowed senders (搂19)
  tools: ConnectorTool[];
  managed: boolean; // one-click managed OAuth available (needs cloud sign-in)
  managed_paused?: boolean; // one-click temporarily off (e.g. Google CASA pending) 鈥?badge "Coming soon"
  managed_profile: boolean; // current profile came from managed OAuth (vs manual paste)
  mode?: string; // "relay" for the managed cloud path; "" for manual/token connect
  workspaces?: SlackWorkspace[]; // Slack only: connected workspaces (managed relay)
  // Gmail/Calendar: email-keyed rows; generic account connectors (notion,
  // attio, posthog, 鈥?: AccountRow. The detail pages narrow by connector.
  accounts?: GmailAccount[] | AccountRow[];
  filters?: GmailFilters; // Gmail only: "Never show agents" senders/labels
  portals?: HubSpotPortal[]; // HubSpot only: connected portals (multi-portal)
  hidden_fields?: string[]; // HubSpot only: properties stripped from agent reads
  installations?: GithubInstallation[]; // GitHub only: App installations (managed relay)
}

// --- QunWork Cloud (optional sign-in; manual token paste always works) ---

export interface CloudStatus {
  signed_in: boolean;
  account: string;
  user_id: string;
  telemetry_enabled?: boolean; // Phase 5 opt-out; signed-out users send nothing regardless
}

/** Flip the product-telemetry preference (local; only meaningful when signed in). */
export async function setCloudTelemetry(
  enabled: boolean,
): Promise<{ ok: boolean; telemetry_enabled?: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/cloud/telemetry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  return res.json();
}

export async function getCloudStatus(): Promise<CloudStatus> {
  const res = await apiFetch(`${httpBase()}/v1/cloud/status`);
  return res.json();
}

export async function cloudLogin(): Promise<{ ok: boolean }> {
  // The sidecar opens the system browser; the GUI just polls status after.
  const res = await apiFetch(`${httpBase()}/v1/cloud/login`, { method: "POST" });
  return res.json();
}

/** Poll cloud status until the browser sign-in lands (or the bound runs out).
 *
 * Fast 500ms polls for the first 20s 鈥?the moment the user finishes in the
 * browser they're staring at the app waiting for it to flip, and a 2s interval
 * reads as "sign-in is slow" (owner complaint, 2026-07-16) 鈥?then relaxes to 2s
 * for the long tail (~2min total). Calls `onDone` with the signed-in status, or
 * null when it timed out. Returns a cancel function (call on unmount). */
export function waitForCloudSignIn(
  onDone: (s: CloudStatus | null) => void,
): () => void {
  let cancelled = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let polls = 0;
  const tick = async () => {
    polls += 1;
    const s = await getCloudStatus().catch(() => null);
    if (cancelled) return;
    if (s?.signed_in) return onDone(s);
    if (polls >= 90) return onDone(null); // 40脳500ms + 50脳2s 鈮?2min
    timer = setTimeout(tick, polls < 40 ? 500 : 2000);
  };
  timer = setTimeout(tick, 500);
  return () => {
    cancelled = true;
    if (timer) clearTimeout(timer);
  };
}

export async function cloudLogout(): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/cloud/logout`, { method: "POST" });
  return res.json();
}

export async function connectManaged(
  name: string,
  options?: { access?: "read" | "write" },
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(name)}/connect-managed`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // `access` names a broker-defined consent tier (hubspot read | write).
      // GitHub needs no flow choice: the broker is authorize-first 鈥?one connect
      // links an existing App installation or redirects on to the install page.
      body: JSON.stringify({
        ...(options?.access ? { access: options.access } : {}),
      }),
    },
  );
  return res.json();
}

/** One-click connect for an MCP-backed connector (monday, asana, jira): the sidecar
 * opens the vendor's sign-in in the browser (local OAuth, no cloud account needed);
 * poll getConnectors until the card flips to connected. */
export async function connectMcpBacked(name: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(name)}/mcp-connect`,
    { method: "POST" },
  );
  return res.json();
}

export interface ConnectorTool {
  name: string;
  label: string;
  kind: "read" | "write" | string;
  description: string;
  enabled: boolean;
  requires_approval: boolean;
}

export async function getConnectors(): Promise<Connector[]> {
  const res = await apiFetch(`${httpBase()}/v1/connectors`);
  return (await res.json()).connectors ?? [];
}

// -- multi-agent orchestration (swarm) --------------------------------------

export interface OrchestratedTask {
  id: string;
  description: string;
  deps: string[];
  status: string;
  confidence: number;
  result: string;
}

export interface OrchestrationResponse {
  ok: boolean;
  status: string;
  runs: number;
  error?: string;
  run_id?: string;
  async?: boolean;
  report_path?: string;
  tasks: OrchestratedTask[];
  governance_report: string;
  session_id?: string;
}

export async function orchestrate(
  intent: string,
  opts?: {
    workspace?: string;
    maxParallel?: number;
    memoryScope?: string;
    sync?: boolean;
    timeoutSeconds?: number;
    executorAgent?: "cowork" | "code";
    templateId?: number;
  },
): Promise<OrchestrationResponse> {
  const res = await apiFetch(`${httpBase()}/v1/orchestrate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      intent,
      workspace: opts?.workspace,
      max_parallel: opts?.maxParallel,
      memory_scope: opts?.memoryScope,
      sync: opts?.sync,
      timeout_seconds: opts?.timeoutSeconds,
      executor_agent: opts?.executorAgent,
      template_id: opts?.templateId,
    }),
  });
  return await res.json();
}

// -- skills marketplace ------------------------------------------------------

export interface SkillInfo {
  name: string;
  description: string;
  version: string;
  category: string;
  author: string;
  tags: string[];
  updated_at: string | null;
  install_count: number;
  rating: number | null;
  rating_count: number;
  // 淇′换鍩虹鍏冩暟鎹?
  security_score?: number | null;
  security_level?: "low" | "medium" | "high" | "critical";
  lock_exists?: boolean;
  lock_generated_at?: number | null;
  compatible?: boolean;
  compat_severity?: "none" | "low" | "medium" | "high";
  // P1-6: 鏄惁涓鸿崏绋?(鏉ヨ嚜 HORNET 娑岀幇鑷姩鐢熸垚, 寰呭鏍?
  draft?: boolean;
  // P1-8: 鏉ユ簮 ("manual" / "hornet_emergence")
  source?: string;
  // 鍙敤鐗堟湰鍒楄〃 (鏉ヨ嚜鍚庣 aggregate_stats / versions)
  available_versions?: string[];
}

export async function getSkillsPath(): Promise<{ ok: boolean; path?: string; exists?: boolean; skill_count?: number }> {
  const res = await apiFetch(`${httpBase()}/v1/skills/path`);
  return res.json();
}

export async function setSkillsPath(
  path: string,
  migrate: boolean = false,
): Promise<{ ok: boolean; error?: string; path?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/skills/path`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, migrate }),
  });
  return res.json();
}

export async function listSkills(): Promise<{ skills: SkillInfo[] }> {
  const res = await apiFetch(`${httpBase()}/v1/skills`);
  return await res.json();
}

export async function rateSkill(
  name: string,
  score: number,
): Promise<{ ok: boolean; rating?: number; rating_count?: number; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/skills/${encodeURIComponent(name)}/rate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ score }),
  });
  return await res.json();
}

export async function exportSkill(name: string): Promise<{ ok: boolean; zip_base64?: string; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/skills/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  return await res.json();
}

export async function importSkill(
  zipBase64: string,
): Promise<{ ok: boolean; name?: string; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/skills/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ zip_base64: zipBase64 }),
  });
  return await res.json();
}

export async function deleteSkill(name: string): Promise<{ ok: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/skills/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  return await res.json();
}

// -- Skill 淇′换鍩虹 (鐗堟湰甯傚満 + lock + 瀹夊叏 + 鍏煎 + 鑷姩淇) ---------------

export interface SkillLockTool {
  name: string;
  schema_hash: string;
  params: string[];
  required?: boolean;
  source?: "allowed_tools" | "heuristic";
}

export interface SkillLockScript {
  path: string;
  integrity: string;
  size: number;
}

export interface SkillLock {
  skill_name: string;
  skill_version: string;
  generated_at: number;
  lock_version: number;
  tools: SkillLockTool[];
  scripts?: SkillLockScript[];
  lock_hash?: string;
  note?: string;
}

export interface SkillVersionRow {
  version: string;
  install_count: number;
  rating: number | null;
  rating_count: number;
  last_installed_at: number | null;
}

export interface SkillVersions {
  ok: boolean;
  aggregate: {
    name: string;
    total_install_count: number;
    weighted_rating: number | null;
    total_rating_count: number;
    latest_version: string | null;
    versions_count: number;
  };
  versions: SkillVersionRow[];
}

export async function getSkillVersions(name: string): Promise<SkillVersions> {
  const res = await apiFetch(`${httpBase()}/v1/skills/${encodeURIComponent(name)}/versions`);
  return await res.json();
}

export async function generateSkillLock(
  name: string,
): Promise<{ ok: boolean; lock?: SkillLock; lock_path?: string; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/skills/${encodeURIComponent(name)}/lock`, {
    method: "POST",
  });
  return await res.json();
}

export interface SkillLockInfo {
  ok: boolean;
  lock?: SkillLock | null;
  lock_exists: boolean;
  lock_path?: string;
  integrity_ok?: boolean;
  mismatched_scripts?: Array<{ path: string; expected: string; actual: string }>;
  missing_scripts?: string[];
  error?: string;
}

export async function getSkillLock(name: string): Promise<SkillLockInfo> {
  const res = await apiFetch(`${httpBase()}/v1/skills/${encodeURIComponent(name)}/lock`);
  return await res.json();
}

export interface SkillSecurityFinding {
  pattern: string;
  weight: number;
  label: string;
  count: number;
  file?: string;
  lines?: number[];
  category?: "shell" | "path" | "network" | "data" | "process" | "secret" | "privilege" | "other";
}

export interface SkillSecurityReport {
  ok: boolean;
  skill_name?: string;
  score: number;
  level: "low" | "medium" | "high" | "critical";
  findings: SkillSecurityFinding[];
  recommendation: string;
  breakdown?: Record<string, number>;
  scanned_files?: number;
  scan_time_ms?: number;
  error?: string;
}

export async function getSkillSecurity(name: string, rescan = false): Promise<SkillSecurityReport> {
  const q = rescan ? "?rescan=1" : "";
  const res = await apiFetch(`${httpBase()}/v1/skills/${encodeURIComponent(name)}/security${q}`);
  return await res.json();
}

export async function rescanSkillSecurity(name: string): Promise<SkillSecurityReport> {
  const res = await apiFetch(`${httpBase()}/v1/skills/${encodeURIComponent(name)}/security/rescan`, {
    method: "POST",
  });
  return await res.json();
}

export interface SkillCompatibilityReport {
  ok: boolean;
  skill_name?: string;
  skill_version?: string;
  has_lock: boolean;
  compatible: boolean;
  severity: "none" | "low" | "medium" | "high";
  missing_tools?: Array<{ name: string; schema_hash?: string; params?: string[] }>;
  changed_tools?: Array<{
    name: string;
    before_hash?: string;
    after_hash?: string;
    params_added?: string[];
    params_removed?: string[];
  }>;
  new_tools?: Array<{ name: string; schema_hash?: string }>;
  summary?: string;
  autofix_plan?: {
    title?: string;
    intent?: string;
    reviewer_rubric?: Array<[string, string]>;
    step_plan?: Array<{ skill_name: string; action: string; note?: string }>;
    affected_skills?: string[];
  };
  recommendation?: string;
  error?: string;
}

export async function checkSkillCompatibility(name: string): Promise<SkillCompatibilityReport> {
  const res = await apiFetch(`${httpBase()}/v1/skills/${encodeURIComponent(name)}/compatibility`);
  return await res.json();
}

export async function checkAllSkillsCompatibility(): Promise<{
  ok: boolean;
  reports: SkillCompatibilityReport[];
}> {
  const res = await apiFetch(`${httpBase()}/v1/skills/compatibility/all`);
  return await res.json();
}

export async function buildSkillAutofix(skillNames?: string[]): Promise<{
  ok: boolean;
  title?: string;
  intent?: string;
  reviewer_rubric?: Array<[string, string]>;
  step_plan?: Array<{ skill_name: string; action: string; note?: string }>;
  affected_skills?: string[];
}> {
  const res = await apiFetch(`${httpBase()}/v1/skills/autofix`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skill_names: skillNames ?? null }),
  });
  return await res.json();
}

// -- knowledge file library --------------------------------------------------

export interface KnowledgeItem {
  id: number;
  kind: "manual" | "file";
  source_path: string | null;
  title: string;
  created_at: number;
  updated_at: number;
}

export interface KnowledgeHit {
  item_id: number;
  chunk_index: number;
  content: string;
  score: number;
  kind: string;
  title: string;
  source_path: string | null;
}

export async function getKnowledgePath(): Promise<{ ok: boolean; db_path?: string; exists?: boolean; size_bytes?: number }> {
  const res = await apiFetch(`${httpBase()}/v1/knowledge/path`);
  return res.json();
}

export async function setKnowledgePath(
  path: string,
  migrate: boolean = false,
): Promise<{ ok: boolean; error?: string; db_path?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/knowledge/path`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, migrate }),
  });
  return res.json();
}

export async function listKnowledge(
  limit = 100,
  offset = 0,
): Promise<{ items: KnowledgeItem[]; total?: number }> {
  const res = await apiFetch(`${httpBase()}/v1/knowledge?limit=${limit}&offset=${offset}`);
  return await res.json();
}

export async function scanKnowledge(): Promise<{
  ok: boolean;
  added?: number;
  skipped?: number;
  failed?: number;
  error?: string;
  workspaces_scanned?: number;
  failures?: { path?: string; reason?: string }[];
}> {
  const res = await apiFetch(`${httpBase()}/v1/knowledge/scan`, { method: "POST" });
  return await res.json();
}

export async function importKnowledgeFolder(
  path: string,
): Promise<{
  ok: boolean;
  added?: number;
  skipped?: number;
  failed?: number;
  truncated?: boolean;
  folder?: string;
  error?: string;
}> {
  const res = await apiFetch(`${httpBase()}/v1/knowledge/import-folder`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  return await res.json();
}

export async function addKnowledge(
  title: string,
  content: string,
): Promise<{ ok: boolean; id?: number; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/knowledge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, content }),
  });
  return await res.json();
}

export async function deleteKnowledge(id: number): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/knowledge/${id}`, { method: "DELETE" });
  return await res.json();
}

export async function localSearch(
  query: string,
  maxResults = 10,
): Promise<{ ok: boolean; results?: Array<{ title: string; url: string; snippet: string; source: string; score: number }>; total?: number; cached?: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/local-search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, max_results: maxResults }),
  });
  return res.json();
}

export async function searchKnowledge(
  query: string,
): Promise<{ ok: boolean; results: KnowledgeHit[]; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/knowledge/search?q=${encodeURIComponent(query)}`);
  return await res.json();
}

// --- local voice chat (VAD + streaming ASR + LLM + TTS) -------------------------

export interface VoiceModelStatus {
  installed: boolean;
  label: string;
  size: number;
  expected_size: number;
}

export interface VoiceStatus {
  models: Record<string, VoiceModelStatus>;
  running: boolean;
  install: { active: boolean; key: string; done: number; total: number; error: string };
  model: string;
}

export interface VoiceEvent {
  index: number;
  type: "state" | "partial" | "final" | "reply" | "error";
  payload: string;
  detail: string;
}

export async function getVoiceStatus(): Promise<VoiceStatus | null> {
  try {
    const res = await apiFetch(`${httpBase()}/v1/voice/status`);
    return await res.json();
  } catch {
    return null;
  }
}

export async function installVoiceModels(): Promise<{ ok: boolean; started?: boolean; reason?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/voice/install`, { method: "POST" });
  return await res.json();
}

export async function startVoiceChat(): Promise<{ ok: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/voice/start`, { method: "POST" });
  return await res.json();
}

export async function stopVoiceChat(): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/voice/stop`, { method: "POST" });
  return await res.json();
}

export async function getVoiceEvents(after = 0): Promise<{ events: VoiceEvent[]; next: number }> {
  const res = await apiFetch(`${httpBase()}/v1/voice/events?after=${after}`);
  return await res.json();
}

export interface TaskTemplate {
  id: number;
  title: string;
  prompt: string;
  created_at?: string;
}

export async function listTaskTemplates(): Promise<{ templates: TaskTemplate[] }> {
  const res = await apiFetch(`${httpBase()}/v1/task-templates`);
  return await res.json();
}

export async function addTaskTemplate(
  title: string,
  prompt: string,
): Promise<{ ok: boolean; template?: TaskTemplate; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/task-templates`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, prompt }),
  });
  return await res.json();
}

export async function deleteTaskTemplate(id: number): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/task-templates/${id}`, { method: "DELETE" });
  return await res.json();
}

export interface SwarmTemplate {
  id: number;
  title: string;
  intent: string;
  plan?: Array<{ id: string; description: string; deps?: string[] }>;
  created_at?: string;
  runs_count?: number;
  success_count?: number;
}

export async function listSwarmTemplates(): Promise<{ templates: SwarmTemplate[] }> {
  const res = await apiFetch(`${httpBase()}/v1/swarm-templates`);
  return await res.json();
}

export async function addSwarmTemplate(
  title: string,
  intent: string,
  plan?: unknown,
): Promise<{ ok: boolean; template?: SwarmTemplate; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/swarm-templates`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, intent, plan }),
  });
  return await res.json();
}

export async function deleteSwarmTemplate(id: number): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/swarm-templates/${id}`, { method: "DELETE" });
  return await res.json();
}

export async function recordSwarmTemplateRun(
  id: number,
  success: boolean,
): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/swarm-templates/${id}/record`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ success }),
  });
  return await res.json();
}

// -- swarm lessons (Refine 鏈哄埗: 铚傜兢缁忛獙杩涘寲闂幆) ----------------------------
export interface SwarmLesson {
  id: number;
  kind: "lesson" | "skill_hint" | "task_template";
  title: string;
  body: string;
  source_run_id?: string;
  intent?: string;
  tags?: string[];
  use_count?: number;
  version?: number;
  created_at?: string;
  updated_at?: string;
}

export async function listSwarmLessons(
  kind?: string,
  limit = 50,
  workspace?: string,
): Promise<{ lessons: SwarmLesson[] }> {
  const q = new URLSearchParams();
  if (kind) q.set("kind", kind);
  q.set("limit", String(limit));
  if (workspace) q.set("workspace", workspace);
  const res = await apiFetch(`${httpBase()}/v1/swarm-lessons?${q.toString()}`);
  return await res.json();
}

export async function deleteSwarmLesson(
  id: number,
  workspace?: string,
): Promise<{ ok: boolean }> {
  const q = workspace ? `?workspace=${encodeURIComponent(workspace)}` : "";
  const res = await apiFetch(`${httpBase()}/v1/swarm-lessons/${id}${q}`, {
    method: "DELETE",
  });
  return await res.json();
}

// G2 command deck: live control over a running swarm run.
// P0 寤鸿3 adds task_inject (fork a sub-task) + retarget (reassign agent).
export type OrchestrateControlAction =
  | "pause"
  | "resume"
  | "message"
  | "requeue_approve"
  | "requeue_reject"
  | "task_inject"
  | "retarget";

export interface OrchestrateControlStatus {
  ok: boolean;
  error?: string;
  paused?: boolean;
  requeues?: Array<{ task_id: string; attempt?: number; reason?: string }>;
}

export async function orchestrateControlStatus(
  runId: string,
): Promise<OrchestrateControlStatus> {
  const res = await apiFetch(`${httpBase()}/v1/orchestrate/${runId}/control`);
  return await res.json();
}

export async function orchestrateControl(
  runId: string,
  action: OrchestrateControlAction,
  body: {
    text?: string;
    task_id?: string;
    description?: string;
    deps?: string[];
    agent?: string;
  } = {},
): Promise<{ ok: boolean; error?: string; paused?: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/orchestrate/${runId}/control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...body }),
  });
  return await res.json();
}

// Benchmark showcase: render a finished run into a "coordination report" (Markdown).
export interface CoordinationReport {
  ok: boolean;
  error?: string;
  run_id?: string;
  status?: string;
  intent?: string;
  duration_s?: number;
  markdown?: string;
  report_path?: string;
}

export async function getCoordinationReport(runId: string): Promise<CoordinationReport> {
  const res = await apiFetch(`${httpBase()}/v1/orchestrate/${runId}/report`);
  return await res.json();
}

// Team workspace (P2): export/import a team package (templates + knowledge + skills).
export async function exportTeamPackage(): Promise<{ ok: boolean; path?: string; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/team/export`);
  return await res.json();
}

export async function importTeamPackage(
  path: string,
): Promise<{ ok: boolean; imported?: Record<string, number>; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/team/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  return await res.json();
}

export interface OrchestrationDegradation {
  task_id: string;
  level: number;
  action: string;
  fidelity: number;
  error?: string;
  ts: number;
}

export interface ConvergenceReport {
  gap: number;
  theoretical_rounds: number;
  iterations: number;
  convergence_curve: number[];
  final_convergence: number;
  converged: boolean;
  stalled: boolean;
}

export interface OrchestrationRunSnapshot {
  ok: boolean;
  error?: string;
  run_id: string;
  intent: string;
  status: string;
  final?: string;
  created_at?: number;
  updated_at?: number;
  events: { kind: string; payload: Record<string, unknown> }[];
  // 7x24 闀跨▼浠诲姟: 闄嶇骇杞ㄨ抗 (绐佺牬浜? + 鏀舵暃鎶ュ憡 (绐佺牬浜?銆?
  degradations?: OrchestrationDegradation[];
}

export async function getOrchestrateRun(runId: string): Promise<OrchestrationRunSnapshot> {
  const res = await apiFetch(`${httpBase()}/v1/orchestrate/${runId}`);
  return await res.json();
}

export interface OrchestrationHistoryItem {
  run_id: string;
  intent: string;
  status: string;
  created_at: number;
  updated_at: number;
  // P0 寤鸿3 淇濈暀鍒嗘敮 A/B: non-null when this run is a fork of another run.
  parent_run_id?: string | null;
}

export async function getOrchestrateHistory(): Promise<{ runs: OrchestrationHistoryItem[] }> {
  const res = await apiFetch(`${httpBase()}/v1/orchestrate/history`);
  return await res.json();
}

// 鈹€鈹€ 7x24 闀跨▼浠诲姟绠＄悊 (鍋ュ悍鎺у埗鍙?/ 閬ユ祴 / 妫€鏌ョ偣 / 瀛樺偍) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

export interface LongrunHealth {
  heartbeat: {
    tasks: number;
    alive: string[];
    unhealthy: string[];
    detection_time_seconds?: number;
  };
  automation: {
    total: number;
    enabled: number;
    failed_recent: number;
    run_count_total: number;
  };
  wakes: { pending: number; due: number };
  detection_time_seconds?: number;
}

export interface LongrunTelemetry {
  degradations: OrchestrationDegradation[];
  convergence_history: {
    run_id: string;
    intent: string;
    status: string;
    report: ConvergenceReport;
  }[];
}

export interface LongrunCheckpointSession {
  session_id: string;
  title: string;
  message_count: number;
  archived: boolean;
}

export interface LongrunCheckpointDetail {
  ok: boolean;
  error?: string;
  session_id?: string;
  chain?: {
    seq: number;
    n_layer: number;
    granularity: string;
    created_at: number;
    expires_at: number | null;
  }[];
  count?: number;
  latest_restorable?: boolean;
}

export interface LongrunCheckpointRestore {
  ok: boolean;
  error?: string;
  session_id?: string;
  restore_ms?: number;
  applied?: boolean;
  backup_seq?: string | null;
  backup_key?: string | null;
  rolled_back_messages?: number;
  summary?: {
    messages?: number | null;
    tasks?: number | null;
    result?: string | null;
    phase?: string | null;
    keys?: string[];
  };
}

/** 鈶?鍛婅鑱氬悎鏉＄洰 (鍚屼换鍔¤繛缁崱姝诲悎骞?銆?*/
export interface LongrunAlertAggregation {
  id: number;
  task_id: string;
  kind: string;
  level: string;
  started_at: number;
  updated_at: number;
  count: number;
  resolved: number;
  silenced_until?: number | null;
}

/** 鈶?鑱氬悎鍘嗗彶缁熻: 姣忔棩瓒嬪娍銆?*/
export interface AggregationStats {
  ok: boolean;
  error?: string;
  days: { alerts: number; resolved: number; tasks: number }[];
  top_tasks: [string, number][];
}

/** 鈶?鎿嶄綔瀹¤鏉＄洰銆?*/
export interface LongrunAuditEntry {
  id: number;
  kind: string;
  task_id?: string | null;
  message: string;
  ts: number;
  payload?: Record<string, unknown>;
}

/** 鈶?鍛婅娓犻亾閰嶇疆 (鑴辨晱)銆?*/
export interface AlertChannelConfig {
  enabled: boolean;
  levels?: string[];
  smtp_host?: string;
  smtp_port?: number;
  username?: string;
  password?: string;
  to?: string[];
  from?: string;
  use_tls?: boolean;
  bot_token?: string;
  chat_id?: string;
  webhook_url?: string;
  secret?: string;
}

/** 鈶?鍛婅鍘嗗彶鏉＄洰銆?*/
export interface LongrunAlert {
  id: number;
  kind: string;
  task_id?: string | null;
  message: string;
  ts: number;
  payload?: Record<string, unknown>;
}

/** /ws/events 鎺ㄩ€佺殑 7x24 鍛婅浜嬩欢銆?*/
export interface LongrunAlertEvent {
  type: "7x24_alert";
  payload: {
    kind: string;
    task_id?: string;
    message: string;
    ts: number;
  };
}

export interface LongrunStorage {
  sessions: {
    count: number;
    jsonl_total_bytes: number;
    archived_sessions: number;
    archive_bytes: number;
    top: { session_id: string; jsonl_bytes: number; archived: boolean }[];
  };
  memory: { count: number; stale: number };
}

export async function getLongrunHealth(): Promise<LongrunHealth> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/health`);
  return await res.json();
}

// ── OIR longrun 集成（握手任务配置/状态，经 QunWork 7×24 tick 驱动）──

export interface OirLongrunConfig {
  enabled: boolean;
  doc_dir: string;
  glob: string;
  batch: number;
  goal_id: string | null;
}

export interface OirLongrunRemoteTask {
  goal_id?: string;
  oir_task_id?: string;
  phase?: string;
  completed_documents?: number;
  total_documents?: number;
  progress_percent?: number;
}

export interface OirLongrunTelemetrySnapshot {
  generated_at?: string;
  source?: string;
  task?: OirLongrunRemoteTask;
  trend?: { days?: Record<string, unknown> } | null;
  growth_report?: { direction?: string; note?: string } | null;
  telemetry_file?: string;
}

export async function getOirLongrunConfig(): Promise<OirLongrunConfig> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/oir-longrun`);
  return await res.json();
}

export async function setOirLongrunConfig(
  patch: Partial<OirLongrunConfig>,
): Promise<OirLongrunConfig> {
  const res = await fetch(`${httpBase()}/v1/7x24/oir-longrun`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new ApiError(res.status, "/v1/7x24/oir-longrun");
  return await res.json();
}

export async function getOirLongrunTelemetry(): Promise<OirLongrunTelemetrySnapshot> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/oir-longrun/telemetry`);
  return await res.json();
}

export async function getLongrunTelemetry(limit = 20): Promise<LongrunTelemetry> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/telemetry?limit=${limit}`);
  return await res.json();
}

export async function getLongrunCheckpoints(): Promise<{ sessions: LongrunCheckpointSession[] }> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/checkpoints`);
  return await res.json();
}

export async function getLongrunCheckpointDetail(
  sessionId: string,
): Promise<LongrunCheckpointDetail> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/checkpoints/${encodeURIComponent(sessionId)}`);
  return await res.json();
}

export async function restoreLongrunCheckpoint(
  sessionId: string,
  apply = false,
): Promise<LongrunCheckpointRestore> {
  const res = await fetch(
    `${httpBase()}/v1/7x24/checkpoints/${encodeURIComponent(sessionId)}/restore${apply ? "?apply=true" : ""}`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
  );
  return await res.json();
}

export async function rollbackLongrunCheckpoint(
  sessionId: string,
): Promise<LongrunCheckpointRestore> {
  const res = await fetch(
    `${httpBase()}/v1/7x24/checkpoints/${encodeURIComponent(sessionId)}/rollback`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
  );
  return await res.json();
}

export async function getLongrunAlertAggregations(
  limit = 50,
): Promise<{ ok: boolean; aggregations: LongrunAlertAggregation[]; count: number; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/alerts/aggregations?limit=${limit}`);
  return await res.json();
}

export async function getLongrunAggregationStats(
  days = 14,
): Promise<AggregationStats> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/alerts/aggregations/stats?days=${days}`);
  return await res.json();
}

export async function getLongrunAudit(
  limit = 50,
): Promise<{ ok: boolean; audit: LongrunAuditEntry[]; count: number; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/audit?limit=${limit}`);
  return await res.json();
}

/** 鈶?瀹¤瀵煎嚭 (CSV/JSON)銆傝繑鍥?{ok, content, format} 鎴栫洿鎺ユ枃鏈€?*/
export async function exportLongrunAudit(
  format: "csv" | "json" = "json",
  limit = 500,
): Promise<string> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/audit/export?format=${format}&limit=${limit}`);
  return await res.text();
}

/** 鈶?鍛婅璁剧疆 (闈欓粯闃堝€?鏃堕暱 + 褰掓。淇濈暀澶╂暟 + 鍋ュ悍鍒嗛槇鍊?+ 鎺㈤拡鍘嗗彶淇濈暀绐楀彛)銆?*/
export interface AlertSettings {
  silence_after: number;
  silence_seconds: number;
  archive_keep_days?: number;
  /** 娓犻亾鍋ュ悍鍒嗛槇鍊?(good/warn 杈圭晫, 0-100, warn <= good)銆?*/
  health_thresholds?: { good: number; warn: number };
  /** 鎺㈤拡鍘嗗彶淇濈暀绐楀彛: 瓒呰繃 keep_days 澶╃殑璁板綍娓呯悊銆?*/
  probe_history_keep_days?: number;
  /** 鎺㈤拡鍘嗗彶淇濈暀绐楀彛: 鏈€澶氫繚鐣?keep_count 鏉°€?*/
  probe_history_keep_count?: number;
}

export interface ProbeHistoryPruneResult {
  ok: boolean;
  removed?: number;
  kept?: number;
  keep_days?: number;
  keep_count?: number;
  error?: string;
}

/** 鈶?鎵嬪姩瑙﹀彂鎺㈤拡鍘嗗彶娓呯悊 (鎸夐厤缃繚鐣欑獥鍙?銆?*/
export async function pruneProbeHistory(): Promise<ProbeHistoryPruneResult> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/probe-history/prune`, { method: "POST" });
  return await res.json();
}

export async function getLongrunAlertSettings(): Promise<{ ok: boolean; settings: AlertSettings; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/alert-settings`);
  return await res.json();
}

export async function setLongrunAlertSettings(
  settings: Partial<AlertSettings>,
): Promise<{ ok: boolean; settings: AlertSettings; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/alert-settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings }),
  });
  return await res.json();
}

/** 鈶?璺ㄥ懆瀵规瘮: 鏈懆 vs 涓婂懆姣忔棩鍛婅鏁般€?*/
export interface WeekCompare {
  ok: boolean;
  error?: string;
  labels: string[];
  this_week: number[];
  last_week: number[];
  total_this: number;
  total_last: number;
  delta_pct: number;
}

export async function getLongrunWeekCompare(): Promise<WeekCompare> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/alerts/aggregations/week-compare`);
  return await res.json();
}

export async function getLongrunAlerts(
  limit = 50,
  taskId?: string,
  since?: number,
  until?: number,
): Promise<{ ok: boolean; alerts: LongrunAlert[]; count: number; error?: string }> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (taskId) params.set("task_id", taskId);
  if (since != null) params.set("since", String(since));
  if (until != null) params.set("until", String(until));
  const res = await apiFetch(`${httpBase()}/v1/7x24/alerts?${params.toString()}`);
  return await res.json();
}

export async function getLongrunAlertChannels(): Promise<{
  ok: boolean;
  channels: Record<string, AlertChannelConfig>;
  enabled: string[];
  error?: string;
}> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/alert-channels`);
  return await res.json();
}

export async function setLongrunAlertChannels(
  channels: Record<string, Partial<AlertChannelConfig>>,
): Promise<{ ok: boolean; channels: Record<string, AlertChannelConfig>; enabled: string[]; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/alert-channels`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ channels }),
  });
  return await res.json();
}

export async function testLongrunAlertChannels(): Promise<{
  ok: boolean;
  results: Record<string, { ok: boolean; error?: string }>;
  error?: string;
}> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/alert-channels/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  return await res.json();
}

/** 鈶?娓犻亾鍋ュ悍鎺㈤拡: 鍚勫惎鐢ㄦ笭閬撹繛閫氭€?+ 寤惰繜銆?*/
export async function probeLongrunAlertChannels(): Promise<{
  ok: boolean;
  results: Record<string, { ok: boolean; ms?: number; error?: string }>;
  healthy: string[];
  error?: string;
}> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/alert-channels/probe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  return await res.json();
}

/** 鈶?鍛婅/瀹¤鑷姩褰掓。: 瓒呰繃 keep_days 澶╃殑璁板綍褰掓。銆?*/
export async function archiveLongrunAlerts(keepDays = 30): Promise<{
  ok: boolean;
  archived: number;
  kept: number;
  archived_total?: number;
  error?: string;
}> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/alerts/archive`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keep_days: keepDays }),
  });
  return await res.json();
}

/** 鈶?娓犻亾鍋ュ悍鍒?鍘嗗彶瓒嬪娍銆?*/
export interface ChannelHealth {
  ok_count: number;
  total: number;
  success_rate: number;
  health_score: number;
  rating: "good" | "warn" | "bad";
  avg_ms?: number | null;
  last_ts?: number | null;
  trend: { ts: number; ok: boolean; ms?: number | null }[];
}

export async function getLongrunChannelHealth(): Promise<{
  ok: boolean;
  channels: Record<string, ChannelHealth>;
  thresholds?: { good: number; warn: number };
  error?: string;
}> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/channel-health`);
  return await res.json();
}

/** 鈶?瀵煎嚭鎺㈤拡鍘嗗彶 (CSV/JSON)銆?*/
export async function exportLongrunChannelHealth(
  format: "csv" | "json" = "json",
  limit = 500,
): Promise<string> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/channel-health/export?format=${format}&limit=${limit}`);
  return await res.text();
}

/** 鈶?褰掓。 keep_days 鍒嗘笭閬撻厤缃€?*/
export async function getLongrunArchivedAlerts(
  limit = 50,
  taskId?: string,
): Promise<{
  ok: boolean;
  archived: LongrunAlert[];
  count: number;
  total_archived: number;
  error?: string;
}> {
  const q = taskId ? `?limit=${limit}&task_id=${encodeURIComponent(taskId)}` : `?limit=${limit}`;
  const res = await apiFetch(`${httpBase()}/v1/7x24/alerts/archived${q}`);
  return await res.json();
}

/** 鈶?褰掓。鏁版嵁鎭㈠: 鎶婁竴鏉″綊妗ｈ褰曟仮澶嶅埌娲昏穬琛?(鎾ら攢褰掓。)銆?*/
export async function restoreLongrunArchivedAlert(
  archiveId: number,
): Promise<{ ok: boolean; restored?: LongrunAlert; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/alerts/archived/${archiveId}/restore`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  return await res.json();
}

/** 鈶?鎺㈤拡瀹氭椂鍖栬皟搴﹁缃€?*/
export interface ProbeSchedule {
  enabled: boolean;
  interval_minutes: number;
  last_probe_ts?: number | null;
}

export async function getLongrunProbeSchedule(): Promise<{ ok: boolean; schedule: ProbeSchedule; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/probe-schedule`);
  return await res.json();
}

export async function setLongrunProbeSchedule(
  schedule: Partial<ProbeSchedule>,
): Promise<{ ok: boolean; schedule: ProbeSchedule; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/probe-schedule`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ schedule }),
  });
  return await res.json();
}

export async function getLongrunStorage(): Promise<LongrunStorage> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/storage`);
  return await res.json();
}

// QunMesh M1/M2: 淇℃伅绱犳€荤嚎鍥涗俊閬撴€昏 (/v1/pheromone, manager.pheromone_status)銆?
export interface PheromoneChannels {
  [channel: string]: { signals: number; intensity: number };
}

// QunMesh M4: 缃戞牸鎷撴墤鍋ュ悍 (位鈧?浠ｆ暟杩為€氬害 + 鐑偣杩佸緳寤鸿)銆?
export interface MeshTopology {
  agents?: string[];
  edges?: number;
  lambda2?: number;
  hotspots?: string[];
  migrations?: { hotspot: string; load: number; neighbor_avg: number; target: string }[];
}

export interface PheromoneStatus {
  levels?: Record<string, number>;
  total_load?: number;
  bus?: "stigmergy" | "field";
  channels?: PheromoneChannels;
  topology?: MeshTopology;
}

export async function getPheromoneStatus(): Promise<PheromoneStatus> {
  const res = await apiFetch(`${httpBase()}/v1/pheromone`);
  return await res.json();
}

// QunMesh M4 鍚庣画椤? mesh_mode 杩愯鏃舵。浣?(GET 璇婚粯璁? PUT 鎸佷箙鍖?瀹¤)銆?
export async function getMeshMode(): Promise<{ ok: boolean; mesh_mode: string }> {
  const res = await apiFetch(`${httpBase()}/v1/mesh/mode`);
  return await res.json();
}

export async function setMeshMode(
  mesh_mode: string,
): Promise<{ ok: boolean; mesh_mode?: string; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/mesh/mode`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mesh_mode }),
  });
  return await res.json();
}

export async function runLongrunMaintenance(
  dryRun = false,
): Promise<Record<string, unknown>> {
  const res = await apiFetch(`${httpBase()}/v1/7x24/maintenance`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dry_run: dryRun }),
  });
  return await res.json();
}

// P0 澧為噺2 (浠诲姟缁勭敓鍛藉懆鏈?: dissolve a finished run (瑙ｆ暎铚傜兢,鍥炴敹璧勬簮).
export async function dissolveRun(
  runId: string,
): Promise<{ ok: boolean; error?: string; already?: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/orchestrate/${encodeURIComponent(runId)}/dissolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  return await res.json();
}

export async function connectConnector(
  name: string,
  fields: Record<string, string>,
): Promise<{ ok: boolean; account?: string; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/connect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fields }),
  });
  return res.json();
}

export async function disconnectConnector(name: string): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/disconnect`, {
    method: "POST",
  });
  return res.json();
}

export async function updateConnectorTools(
  name: string,
  enabled: Record<string, boolean>,
): Promise<{ ok: boolean; error?: string; tools?: Record<string, boolean> }> {
  const res = await apiFetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/tools`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  return res.json();
}

export interface AuditEvent {
  id: number;
  timestamp: string;
  session_id: string;
  agent: string;
  workspace: string;
  connector: string;
  tool: string;
  stage: string;
  status: string;
  approval: string;
  args: Record<string, any>;
  result_preview: string;
  reason: string;
  resource: string;
}

export async function getAudit(params: {
  limit?: number;
  session_id?: string;
  connector?: string;
  tool?: string;
} = {}): Promise<AuditEvent[]> {
  const q = new URLSearchParams();
  if (params.limit) q.set("limit", String(params.limit));
  if (params.session_id) q.set("session_id", params.session_id);
  if (params.connector) q.set("connector", params.connector);
  if (params.tool) q.set("tool", params.tool);
  const res = await apiFetch(`${httpBase()}/v1/audit${q.toString() ? "?" + q.toString() : ""}`);
  return (await res.json()).events ?? [];
}

export interface BrowserState {
  open: boolean;
  url: string;
  title: string;
  status: string;
  last_action: string;
  last_result: string;
  last_error: string;
  screenshot_data_url: string;
  updated_at: string | null;
  controls: any[];
}

export async function getBrowserState(): Promise<BrowserState> {
  const res = await apiFetch(`${httpBase()}/v1/browser/state`);
  return res.json();
}

export async function takeBrowserScreenshot(): Promise<BrowserState & { ok?: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/browser/screenshot`, { method: "POST" });
  return res.json();
}

export async function closeBrowser(): Promise<{ ok?: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/browser/close`, { method: "POST" });
  return res.json();
}

// -- settings (model API key, default model, onboarding) ----------------------
export interface SurfaceVisibility {
  cowork: boolean; // always true
  chat: boolean;
  code: boolean;
}

export interface ModelSettings {
  provider: string;
  model: string;
  models: string[];
  has_key: boolean;
  model_ready: boolean; // can the default model's provider actually run (any provider)?
  source: "env" | "store" | null;
  onboarded: boolean;
  surfaces: SurfaceVisibility;
  scratch_base: string;
  secrets_path: string;  // OS-native on-disk location the server reports (not hardcoded)
  // Sidebar layout preference (搂7): "flat" = the persona accordions / today's list; "grouped" =
  // bounded per-persona cards. Defaults to "flat" (absent 鈫?flat) so the GUI is robust to an older
  // backend that hasn't shipped the field yet.
  nav_layout?: "flat" | "grouped";
  // Sidebar: sessions shown per group before "Show more" (default 5, 1鈥?0).
  sessions_peek?: number;
  // Curated-matrix display names ({full id 鈫?"GLM-5.2 路 via Together"}); custom models absent.
  model_labels?: Record<string, string>;
  // Token savings (PDF attachments): fallback for models without native PDF support,
  // and attach-time thresholds. Optional so the GUI is robust to an older backend.
  pdf_fallback?: "text" | "images";
  pdf_max_pages?: number; // default 20, 1鈥?00
  pdf_max_mb?: number; // default 10, 1鈥?0
}

export interface PdfSettings {
  pdf_fallback: "text" | "images";
  pdf_max_pages: number;
  pdf_max_mb: number;
}

/** Persist the Token-savings PDF settings (fallback mode + attach thresholds). */
export async function setPdfSettings(
  patch: Partial<PdfSettings>,
): Promise<{ ok: boolean; error?: string } & Partial<PdfSettings>> {
  const res = await apiFetch(`${httpBase()}/v1/settings/pdf`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return res.json();
}

/** Local page/size probe for a PDF data URL 鈥?the composer's attach-time threshold check. */
export async function inspectPdf(
  dataUrl: string,
): Promise<{ ok: boolean; pages?: number; bytes?: number; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/attachments/inspect-pdf`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data_url: dataUrl }),
  });
  return res.json();
}

/** Persist how many sessions a sidebar group shows before "Show more". */
export async function setSessionsPeek(
  n: number,
): Promise<{ ok: boolean; sessions_peek?: number; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/settings/sessions-peek`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessions_peek: n }),
  });
  return res.json();
}

export async function setScratchBase(
  path: string,
): Promise<{ ok: boolean; error?: string; scratch_base?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/settings/scratch-base`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  return res.json();
}

export async function setSurfaces(
  flags: { chat?: boolean; code?: boolean },
): Promise<{ ok: boolean; surfaces: SurfaceVisibility }> {
  const res = await apiFetch(`${httpBase()}/v1/settings/surfaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(flags),
  });
  return res.json();
}

/** Persist the sidebar layout preference (flat 鈫?grouped-by-persona); read back from getSettings. */
export async function setNavLayout(
  layout: "flat" | "grouped",
): Promise<{ ok: boolean; nav_layout?: "flat" | "grouped"; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/settings/nav-layout`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nav_layout: layout }),
  });
  return res.json();
}

// Fired after a cloud sign-in/out completes so the account row (搂26) refreshes without
// waiting for the next window focus.
export const CLOUD_CHANGED = "coworker:cloud-changed";
export function announceCloudChanged() {
  window.dispatchEvent(new CustomEvent(CLOUD_CHANGED));
}

// Fired the first time Inbox machinery is engaged (an item parks, or a session goes
// Unattended) 鈥?the account row's inbox chip unlocks stickily on it (搂26).
export const INBOX_UNLOCK = "coworker:inbox-unlock";
export function announceInboxUnlock() {
  window.dispatchEvent(new CustomEvent(INBOX_UNLOCK));
}

// -- Personas -----------------------------------------------------------------

// Fired after any persona mutation (enable/disable/install/delete) so always-mounted
// consumers (the sidebar's new-session picker) refetch instead of going stale.
export const PERSONAS_CHANGED = "coworker:personas-changed";
function announcePersonasChanged() {
  window.dispatchEvent(new CustomEvent(PERSONAS_CHANGED));
}

export interface Persona {
  id: string;
  name: string;
  icon: string;
  tagline: string;
  needs_workspace: boolean;
  builtin: boolean;
  family: string;
  workspace: string; // "git" | "project" | "deliverable" | "none" 鈥?drives project-scoping
  tools: string[];
  enabled: boolean;
  surfaced: boolean;
  default: boolean;
}

export interface PersonaConsent {
  id: string;
  name: string;
  description: string;
  tools: string[];
  risk: string[];
  connectors: boolean;
  mcp: string[];
  messaging: boolean;
  recommended_mode: string;
  recommended_models: string[];
  source: string | null;
  builtin: boolean;
}

export async function getPersonas(): Promise<Persona[]> {
  const res = await apiFetch(`${httpBase()}/v1/personas`);
  return (await res.json()).personas;
}

export async function updatePersona(
  id: string,
  body: { enabled?: boolean; surfaced?: boolean; default?: boolean },
): Promise<{ ok: boolean; personas?: Persona[]; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const out = await res.json();
  if (out.ok !== false) announcePersonasChanged();
  return out;
}

/** Uninstall a non-builtin persona (its snapshot + state). Local; works signed out. */
export async function deletePersona(
  id: string,
): Promise<{ ok: boolean; personas?: Persona[]; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  const out = await res.json();
  if (out.ok) announcePersonasChanged();
  return out;
}

// A curated persona card from the cloud gallery (metadata only 鈥?the manifest
// is fetched server-side at install and runs through the normal consent flow).
export interface GalleryPersona {
  slug: string;
  version: number;
  name: string;
  icon: string;
  tagline: string;
  description: string;
  family: string;
  workspace: string;
  publisher: string;
  recommended_connectors: string[];
  risk_summary: string;
  featured?: boolean; // publisher-flagged for the gallery's featured carousel
}

export async function getCloudGallery(): Promise<{
  ok: boolean;
  personas: GalleryPersona[];
  error?: string;
}> {
  const res = await apiFetch(`${httpBase()}/v1/cloud/gallery`);
  return res.json();
}

// Solo page for one gallery coworker. `capabilities` is the desktop's own
// consent summary derived from the manifest (same parser as install), so the
// page shows exactly what installing would ask the user to approve.
export interface GalleryDetail {
  ok: boolean;
  error?: string;
  card?: GalleryPersona & { pitch_markdown: string };
  capabilities?: {
    tools: string[];
    risk: string[];
    connectors: boolean;
    mcp: string[];
    messaging: boolean;
    recommended_mode: string;
    recommended_models: string[];
  };
  recommends?: { kind: string; ref: string; reason: string; tier: string }[];
}

export async function getCloudGalleryDetail(slug: string): Promise<GalleryDetail> {
  const res = await apiFetch(`${httpBase()}/v1/cloud/gallery/${encodeURIComponent(slug)}`);
  return res.json();
}

export async function installPersona(
  body: { dir?: string; git_url?: string; gallery_slug?: string },
): Promise<{ ok: boolean; consent?: PersonaConsent[]; personas?: Persona[]; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/personas/install`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const out = await res.json();
  if (out.ok) announcePersonasChanged();
  return out;
}

// -- Persona detail + connection defaults (搂5) --------------------------------
// A persona's declared recommendation (manifest `recommends`): a connector or MCP server it works
// best with, with a reason + tier (core/optional). `connected` is annotated server-side from the
// connector list so the detail page can show connect state without a second round-trip.
export interface PersonaRecommendation {
  kind: string; // "connector" | "mcp" | 鈥?
  ref: string; // connector id (e.g. "github") or mcp/server name
  reason: string;
  tier: string; // "core" | "optional"
  connected: boolean;
}

// A persona-default connection (the middle of the 搂4 hierarchy): for a connected connector, whether
// new sessions of this persona get it enabled by default.
export interface PersonaDefaultConnection {
  connector: string; // connector id
  enabled: boolean; // persona-default on/off
  connected: boolean; // is the account actually connected (else the toggle is disabled)
}

export interface PersonaDetail {
  id: string;
  name: string;
  icon: string;
  tagline: string;
  description: string;
  enabled: boolean; // persona on/off (shown in the picker)
  tools: string[];
  recommended_models: string[];
  default_permission_mode: string;
  workspace: string;
  recommends: PersonaRecommendation[];
  default_connections: PersonaDefaultConnection[];
}

export async function getPersonaDetail(id: string): Promise<PersonaDetail> {
  const res = await apiFetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}`);
  return res.json();
}

/** Set a persona-default connection (new sessions of this persona get it on/off by default). */
export async function setPersonaConnection(
  id: string,
  connector: string,
  enabled: boolean,
): Promise<{ ok: boolean; default_connections?: PersonaDefaultConnection[]; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}/connections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ connector, enabled }),
  });
  return res.json();
}

/** Enable/disable the persona (whether it surfaces in the new-session picker). */
export async function setPersonaEnabled(
  id: string,
  enabled: boolean,
): Promise<{ ok: boolean; personas?: Persona[]; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}/enable`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  const out = await res.json();
  if (out.ok) announcePersonasChanged();
  return out;
}

// -- Per-session connections (Sources bar + drawer, 搂6) -----------------------
// An effective-enabled connector for a session, with a short human detail (e.g. "#ocw-test 路 DMs").
// `enabled` reflects the session override/persona default so the drawer toggle shows correct state.
export interface SessionConnectedConnector {
  connector: string;
  enabled: boolean;
  detail: string;
}

// A persona-recommended connector not yet connected (drives the `鈿?N` attention count).
export interface SessionRecommendedConnector {
  connector: string;
  reason: string;
  tier: string;
  connected: boolean;
}

export interface SessionConnections {
  connected: SessionConnectedConnector[];
  recommended: SessionRecommendedConnector[];
  attention: number; // 鈿?count = recommended connectors not yet connected
}

/** `persona` = the active persona hint 鈥?required for brand-new sessions (no server-side
 * record yet), otherwise the view resolves to the default persona's defaults/recommends. */
export async function getSessionConnections(
  sessionId: string,
  persona?: string,
): Promise<SessionConnections> {
  const q = persona ? `?persona=${encodeURIComponent(persona)}` : "";
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/connections${q}`,
  );
  return res.json();
}

/**
 * Set a per-session connection override (mute/unmute a connector for THIS session). Pass
 * `clear: true` to drop the override and inherit the persona default again.
 */
export async function setSessionConnection(
  sessionId: string,
  connector: string,
  enabled: boolean,
  clear = false,
): Promise<{ ok: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/connections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ connector, enabled, ...(clear ? { clear: true } : {}) }),
  });
  return res.json();
}

// -- Inbox + Unattended -------------------------------------------------------
export interface InboxItem {
  id: string;
  session_id: string;
  kind: "approval" | "question" | "notification" | "directory" | "plan";
  title: string;
  body: string;
  state: "pending" | "resolved";
  resolution: string | null;
  inbox: string;
  created_at: string;
  resolved_at: string | null;
  visibility?: "inline" | "inbox";
  // Question metadata (ask_user): quick-reply choices + a free-text escape.
  options?: string[];
  allow_text?: boolean;
  multi?: boolean;
  // Kind-specific payload (directory: {path, writable}; 鈥?.
  data?: Record<string, any>;
  // Originating-session context (server-joined) so the Inbox is self-contained.
  session_title?: string;
  session_agent?: string | null;
  session_workspace?: string | null;
  session_exists?: boolean;
}

export async function getInbox(sessionId?: string, state?: string): Promise<InboxItem[]> {
  const q = new URLSearchParams();
  if (sessionId) q.set("session_id", sessionId);
  if (state) q.set("state", state);
  const res = await apiFetch(`${httpBase()}/v1/inbox?${q.toString()}`);
  return (await res.json()).items;
}

export async function resolveInboxItem(
  id: string,
  resolution: string,
): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/inbox/${encodeURIComponent(id)}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resolution }),
  });
  return res.json();
}

// -- P2: Inbox compliance annotation (鏉冮檺鐭╅樀鍚堣鏍囨敞) ------------------------
export interface ComplianceInfo {
  tool_name: string;
  capability: string | null;
  member_role: string | null;
  role_has_capability: boolean | null;
  fund_tier: { amount: number; limit: number; role: string; label: string } | null;
  fund_approval: {
    allowed: boolean;
    role: string;
    required_tier?: string;
    tier_label?: string;
    amount?: number;
    escalation?: string;
  } | null;
  escalation: string | null;
  compliance_level: "routine" | "elevated" | "board";
}

export interface InboxComplianceView {
  pending_count: number;
  member_role: string | null;
  items: {
    item_id: string;
    title: string;
    state: string;
    compliance: ComplianceInfo;
    member_role: string | null;
  }[];
}

export async function getInboxCompliance(): Promise<InboxComplianceView> {
  const res = await apiFetch(`${httpBase()}/v1/inbox/compliance`);
  return res.json();
}

// -- channel subscriptions (view-only) ----------------------------------------
export interface Subscription {
  session_id: string;
  session_title: string;
  agent: string;
  channel: string;
  channel_name?: string | null; // resolved display name ("ocw-test"); address stays the id
  routing_target: string | null;
  collision: boolean; // inbound subscription == outbound Inbox routing on the same channel
}

export interface RecentChannel {
  channel: string;
  name?: string | null; // resolved display name, e.g. "ocw-test" (falls back to the address)
  last_from: string | null;
  last_text: string | null;
}

export async function getSubscriptions(): Promise<Subscription[]> {
  const res = await apiFetch(`${httpBase()}/v1/subscriptions`);
  return (await res.json()).subscriptions ?? [];
}

// -- inbox routing (where Unattended approvals/questions get mirrored) ---------
export interface InboxBinding {
  name: string;
  channel: string | null; // platform, e.g. "slack" (null = in-app Inbox only)
  target: string; // chat_id, e.g. "C0BEJNCQQ8Y"
}

export async function getInboxRouting(): Promise<InboxBinding[]> {
  const res = await apiFetch(`${httpBase()}/v1/inbox/routing`);
  return (await res.json()).bindings ?? [];
}

export async function setInboxBinding(
  name: string,
  channel: string | null,
  target: string,
): Promise<{ ok: boolean; bindings?: InboxBinding[]; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/inbox/routing/binding`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, channel, target }),
  });
  return res.json();
}

export interface UnroutedItem {
  source: string;
  sender: string;
  text: string;
  reason: string;
  ts: number;
}

export async function getUnrouted(): Promise<UnroutedItem[]> {
  const res = await apiFetch(`${httpBase()}/v1/unrouted`);
  return (await res.json()).items ?? [];
}

export async function getRecentChannels(): Promise<RecentChannel[]> {
  const res = await apiFetch(`${httpBase()}/v1/channels/recent`);
  return (await res.json()).channels ?? [];
}

export async function subscribeChannel(
  sessionId: string,
  channel: string,
): Promise<{ ok: boolean; channel?: string; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/subscriptions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, channel }),
  });
  return res.json();
}

export async function unsubscribeChannel(
  sessionId: string,
  channel: string,
): Promise<{ ok: boolean; removed?: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/subscriptions/remove`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, channel }),
  });
  return res.json();
}

export async function getUnattended(sessionId: string): Promise<boolean> {
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/unattended`,
  );
  return (await res.json()).unattended;
}

export async function setUnattended(
  sessionId: string,
  unattended: boolean,
): Promise<{ ok: boolean; unattended: boolean }> {
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/unattended`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ unattended }),
    },
  );
  return res.json();
}

export async function getSettings(): Promise<ModelSettings> {
  const res = await apiFetch(`${httpBase()}/v1/settings`);
  return res.json();
}

export async function setModelKey(
  apiKey: string,
): Promise<{ ok: boolean; error?: string; has_key?: boolean; source?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/settings/model-key`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: apiKey }),
  });
  return res.json();
}

export async function setDefaultModel(
  model: string,
): Promise<{ ok: boolean; error?: string; model?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/settings/default-model`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  return res.json();
}

export async function addModel(model: string): Promise<ModelSettings & { ok: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/settings/models/add`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  return res.json();
}

export async function removeModel(model: string): Promise<ModelSettings & { ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/settings/models/remove`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  return res.json();
}

export async function setOnboarded(value: boolean): Promise<{ ok: boolean; onboarded: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/settings/onboarded`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
  return res.json();
}

// -- model providers (OpenAI, Ollama, 鈥? --------------------------------------
export interface ProviderField {
  key: string;
  label: string;
  secret: boolean;
  required: boolean;
  help: string;
  placeholder: string;
  default?: string; // pre-filled editable value (e.g. an OpenAI-compatible vendor's endpoint)
}

export interface ProviderInfo {
  name: string;
  title: string;
  needs_key: boolean;
  fields: ProviderField[];
  configured: boolean;
  values: Record<string, string>; // non-secret stored values (e.g. base_url), for prefilling
  suggested_models: string[]; // bare model-name suggestions for the "add model" datalist
  recommended_model: string | null; // pre-filled default for this provider (e.g. qwen3-coder:30b)
  blurb?: string; // one-line note under the title ("Uses X's OpenAI-compatible API鈥?)
  key_set_at?: string | null; // ISO date the key was last (re)saved 鈥?absent for env-only config
  last_used_at?: number | null; // epoch secs the provider last served a completion
}

export async function getProviders(): Promise<ProviderInfo[]> {
  const res = await apiFetch(`${httpBase()}/v1/providers`);
  return res.json();
}

export async function setProvider(
  name: string,
  fields: Record<string, string>,
): Promise<{ ok: boolean; error?: string; provider?: string; recommended_model?: string | null }> {
  const res = await apiFetch(`${httpBase()}/v1/providers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, fields }),
  });
  return res.json();
}

/** Forget a provider's stored config (Settings 鈻?Models "Remove key鈥?). */
export async function removeProvider(name: string): Promise<{ ok: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/providers/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  return res.json();
}

/** Live read-only credential check (does NOT save the key). Triggered by the user's "Test" click. */
export async function verifyProvider(
  name: string,
  fields: Record<string, string>,
): Promise<{ ok: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/providers/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, fields }),
  });
  return res.json();
}

/** Client-side provider guess from an API key's shape (mirrors the server's detect_provider). */
export function detectProvider(apiKey: string): string | null {
  const key = (apiKey || "").trim();
  if (!key) return null;
  if (key.startsWith("sk-ant-")) return "anthropic";
  if (key.startsWith("AIza")) return "gemini";
  if (key.startsWith("sk-") || key.startsWith("sk_")) return "openai";
  return null;
}

// -- super-agent --------------------------------------------------------------
export interface RecentSender {
  user_id: string;
  user_name: string | null;
  chat_id: string;
  chat_type: string;
  target: string;
  authorized: boolean;
  team_id?: string | null; // workspace (managed relay); null on manual Socket Mode
}

// -- direct-message routing ---------------------------------------------------
export async function getDmRoute(): Promise<string | null> {
  const res = await apiFetch(`${httpBase()}/v1/messaging/dm-route`);
  return (await res.json()).dm_session ?? null;
}

export async function setDmRoute(sessionId: string): Promise<{ ok: boolean; dm_session: string | null }> {
  const res = await apiFetch(`${httpBase()}/v1/messaging/dm-route`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  return res.json();
}

// -- automations (scheduled tasks) --------------------------------------------
export interface Automation {
  id: string;
  title: string;
  instructions: string;
  schedule: string;
  schedule_raw?: { kind: string; cron?: string | null; fire_at?: string | null; timezone?: string };
  workspace: string;
  agent: string;
  enabled: boolean;
  priority: string;
  next_run: number | null;
  last_run: number | null;
  last_status: string | null;
  run_count: number;
  notify_on_completion: boolean;
  // UX-023 sidebar badges: runs started since the user last opened this automation's
  // detail; `unseen_failed` = the newest unseen run errored (danger tint).
  unseen_runs?: number;
  unseen_failed?: boolean;
  seen_runs_at?: number;
  // Standing scoped approvals (搂25): target-bound rules this automation may exercise
  // without asking. `entry` is the raw record entry 鈥?the revoke handle; `target` is
  // null for legacy name-only entries.
  always_allowed: { entry: string; tool: string; target: string | null }[];
}

export interface AutomationRun {
  run_id: string;
  task_id: string;
  session_id: string;
  started_at: number;
  finished_at: number | null;
  status: string;
  result_text: string | null;
  artifacts: string[];
  error: string | null;
  trigger: string;
}

export async function getAutomations(): Promise<Automation[]> {
  const res = await apiFetch(`${httpBase()}/v1/automations`);
  return (await res.json()).tasks ?? [];
}

// Fired after any automation mutation the sidebar should reflect immediately
// (mark-seen, create, delete) 鈥?its poll covers the rest.
export const AUTOMATIONS_CHANGED = "coworker:automations-changed";
export function announceAutomationsChanged() {
  window.dispatchEvent(new CustomEvent(AUTOMATIONS_CHANGED));
}

/** App-wide event stream (/ws/events): session-independent server pushes 鈥?today
 * automation_run_started (the UX-026 toast). Quietly reconnects while the app is
 * open; the returned cleanup stops it for good. */
export function connectEvents(
  onEvent: (msg: { type: string; data?: Record<string, unknown> }) => void
): () => void {
  let ws: WebSocket | null = null;
  let timer: number | null = null;
  let closed = false;
  const open = () => {
    if (closed) return;
    ws = openWebSocket(`${wsBase()}/ws/events`);
    ws.onmessage = (e) => {
      try {
        onEvent(JSON.parse(e.data));
      } catch {
        /* malformed frame 鈥?ignore */
      }
    };
    ws.onclose = () => {
      if (!closed) timer = window.setTimeout(open, 5000);
    };
  };
  open();
  return () => {
    closed = true;
    if (timer !== null) window.clearTimeout(timer);
    ws?.close();
  };
}

/** Advance the automation's seen mark 鈥?clears its unseen-runs badge (UX-023). */
export async function markAutomationSeen(id: string): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/automations/${id}/seen`, { method: "POST" });
  return res.json();
}

export async function createAutomation(payload: {
  title: string;
  instructions: string;
  cron?: string;
  fire_at?: string;
  timezone?: string;
  priority?: "low" | "normal" | "high";
  // 搂25 standing grants (the creating surface rendered them; submit IS the consent).
  // Only target-bound write entries survive server-side validation.
  permissions?: { tool: string; target: string; access: "read" | "write" }[];
}): Promise<{ ok: boolean; error?: string; task?: Automation }> {
  const res = await apiFetch(`${httpBase()}/v1/automations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return res.json();
}

export async function getAutomation(id: string): Promise<{ task: Automation; runs: AutomationRun[] }> {
  const res = await apiFetch(`${httpBase()}/v1/automations/${encodeURIComponent(id)}`);
  return res.json();
}

export async function updateAutomation(id: string, changes: Record<string, any>) {
  const res = await apiFetch(`${httpBase()}/v1/automations/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  return res.json();
}

export async function deleteAutomation(id: string) {
  const res = await apiFetch(`${httpBase()}/v1/automations/${encodeURIComponent(id)}`, { method: "DELETE" });
  return res.json();
}

export interface PreparedRun {
  ok: boolean;
  error?: string;
  run_id: string;
  session_id: string;
  workspace: string;
  agent: string;
  prompt: string;
}

/** Prepare a live manual run: returns the session to open + the opening prompt to send. */
export async function runAutomation(id: string): Promise<PreparedRun> {
  const res = await apiFetch(`${httpBase()}/v1/automations/${encodeURIComponent(id)}/run`, { method: "POST" });
  return res.json();
}

/** Mark a manual run complete after its first turn finished. */
export async function finalizeAutomationRun(id: string, runId: string) {
  const res = await fetch(
    `${httpBase()}/v1/automations/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/finalize`,
    { method: "POST" },
  );
  return res.json();
}

export async function allowUser(
  name: string,
  userId: string,
  teamId?: string | null,
  displayName?: string,
) {
  const res = await apiFetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/allow`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user_id: userId,
      ...(teamId ? { team_id: teamId } : {}),
      // Directory picks carry the display name so the chip is readable at once.
      ...(displayName ? { name: displayName } : {}),
    }),
  });
  return res.json();
}

// One workspace member from the roster (people picker; users:read, cached locally).
export interface SlackMember {
  id: string;
  name: string;
  handle: string;
  guest: boolean;
}

// One channel from the workspace roster. Private channels appear only where the
// bot is a member (Slack API constraint); is_member=false 鈫?"invite @QunWork" hint.
export interface SlackChannelEntry {
  id: string;
  name: string;
  is_private: boolean;
  is_member: boolean;
}

/** Workspace member roster for the people picker (teamId "default" = manual Socket Mode). */
export async function getSlackDirectory(
  teamId: string,
  q = "",
): Promise<{ ok: boolean; error?: string; members?: SlackMember[] }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/slack/workspaces/${encodeURIComponent(teamId)}/directory?q=${encodeURIComponent(q)}`,
  );
  return res.json();
}

/** Channel roster for the channel typeahead (name 鈫?id resolution). */
export async function getSlackChannels(
  teamId: string,
  q = "",
): Promise<{ ok: boolean; error?: string; channels?: SlackChannelEntry[] }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/slack/workspaces/${encodeURIComponent(teamId)}/channels?q=${encodeURIComponent(q)}`,
  );
  return res.json();
}

/** Resolve a parked unauthorized message (搂19): dismiss / allow / allow_deliver. */
export async function resolveUnauthorized(
  name: string,
  itemId: string,
  action: "dismiss" | "allow" | "allow_deliver",
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(name)}/unauthorized/${encodeURIComponent(itemId)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    },
  );
  return res.json();
}

export async function disallowUser(name: string, userId: string, teamId?: string | null) {
  const res = await apiFetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/disallow`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(teamId ? { user_id: userId, team_id: teamId } : { user_id: userId }),
  });
  return res.json();
}

/** Stop relaying one managed Slack workspace (the app stays installed in Slack). */
export async function disconnectSlackWorkspace(teamId: string): Promise<{ ok: boolean; error?: string; remaining_workspaces?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/slack/workspaces/${encodeURIComponent(teamId)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

/** Drop ONE Gmail mailbox; the default pointer moves to the next account. */
export async function disconnectGmailAccount(email: string): Promise<{ ok: boolean; error?: string; remaining_accounts?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/gmail/accounts/${encodeURIComponent(email)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

export async function setGmailDefaultAccount(email: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/gmail/accounts/${encodeURIComponent(email)}/default`,
    { method: "POST" },
  );
  return res.json();
}

/** Drop ONE Google Calendar account; the default pointer moves to the next one. */
export async function disconnectGcalAccount(email: string): Promise<{ ok: boolean; error?: string; remaining_accounts?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/google_calendar/accounts/${encodeURIComponent(email)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

export async function setGcalDefaultAccount(email: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/google_calendar/accounts/${encodeURIComponent(email)}/default`,
    { method: "POST" },
  );
  return res.json();
}

/** Drop ONE account of a generic multi-account connector (notion, attio,
 * posthog, 鈥?; the default pointer moves to the next account. */
export async function disconnectAccount(connector: string, accountId: string): Promise<{ ok: boolean; error?: string; remaining_accounts?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(connector)}/accounts/${encodeURIComponent(accountId)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

export async function setDefaultAccount(connector: string, accountId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(connector)}/accounts/${encodeURIComponent(accountId)}/default`,
    { method: "POST" },
  );
  return res.json();
}

/** Replace the "Never show agents" lists (senders and/or labels; omit to keep). */
export async function setGmailFilters(filters: { senders?: string[]; labels?: string[] }): Promise<{ ok: boolean; filters?: GmailFilters; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/connectors/gmail/filters`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(filters),
  });
  return res.json();
}

// GitHub relay health, the Slack three-layer shape: shared relay socket /
// cloud sign-in / per-installation token health (+ missed-event counts).
export interface GithubStatus {
  ok: boolean;
  mode: string;
  relay: { state: string; reconnects: number; last_event_at: number | null; last_error: string };
  signed_in: boolean;
  installs: Record<string, { token_ok: boolean }>;
  missed: Record<string, number>;
}

export async function getGithubStatus(): Promise<GithubStatus> {
  const res = await apiFetch(`${httpBase()}/v1/connectors/github/status`);
  return res.json();
}

/** Stop relaying ONE GitHub App installation to this computer. */
export async function disconnectGithubInstallation(installationId: string): Promise<{ ok: boolean; error?: string; remaining_installs?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/github/installations/${encodeURIComponent(installationId)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

/** Drop ONE HubSpot portal; the default pointer moves to the next portal. */
export async function disconnectHubSpotPortal(hubId: string): Promise<{ ok: boolean; error?: string; remaining_portals?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/hubspot/portals/${encodeURIComponent(hubId)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

export async function setHubSpotDefaultPortal(hubId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/hubspot/portals/${encodeURIComponent(hubId)}/default`,
    { method: "POST" },
  );
  return res.json();
}

/** Replace the hidden-fields denylist (properties stripped from agent reads). */
export async function setHubSpotHiddenFields(fields: string[]): Promise<{ ok: boolean; hidden_fields?: string[]; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/connectors/hubspot/hidden-fields`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hidden_fields: fields }),
  });
  return res.json();
}

/** Slack health, three honest layers: relay socket / cloud sign-in / per-team tokens. */
export interface SlackStatus {
  mode: string; // "relay" | "" (manual/off)
  relay: {
    state: "live" | "reconnecting" | "offline";
    reconnects: number;
    last_event_at: number | null;
    last_error: string;
  };
  signed_in: boolean;
  teams: Record<string, { token_ok: boolean }>;
}

export async function getSlackStatus(): Promise<SlackStatus> {
  const res = await apiFetch(`${httpBase()}/v1/connectors/slack/status`);
  return res.json();
}

export type Handlers = {
  onEvent: (event: WsEvent) => void;
  onOpen?: () => void;
  onClose?: () => void;
};

export class Session {
  private ws!: WebSocket;
  private readonly sessionId: string;
  private readonly q: string;
  private readonly handlers: Handlers;
  // Payloads sent before the socket finished opening (or while disconnected), replayed on
  // `onopen`. Belt-and-suspenders against the first message being dropped if the user sends
  // in the connect window 鈥?and against a dropped socket silently eating later sends.
  private outbox: object[] = [];
  private reconnectTimer: number | undefined;
  private reconnectDelay = 2000; // 2s, doubling to 30s (owner bug 2026-08-18: a dropped
  // socket left the session permanently mute 鈥?every later message was silently discarded).
  private closed = false;

  constructor(sessionId: string, workspace: string, agent: string, handlers: Handlers) {
    this.sessionId = sessionId;
    this.q = `?workspace=${encodeURIComponent(workspace)}&agent=${encodeURIComponent(agent)}`;
    this.handlers = handlers;
    this.connect();
  }

  private connect() {
    this.ws = openWebSocket(`${wsBase()}/ws/session/${this.sessionId}${this.q}`);
    this.ws.onmessage = (e) => this.handlers.onEvent(JSON.parse(e.data));
    this.ws.onopen = () => {
      this.reconnectDelay = 2000;
      this.flush();
      this.handlers.onOpen?.();
    };
    this.ws.onclose = () => {
      this.handlers.onClose?.();
      if (this.closed) return;
      // Auto-reconnect with backoff, preserving the outbox 鈥?otherwise a single oversized
      // frame / server blip leaves the session permanently mute (owner bug 2026-08-18).
      const d = this.reconnectDelay;
      this.reconnectDelay = Math.min(d * 2, 30_000);
      this.reconnectTimer = window.setTimeout(() => this.connect(), d);
    };
  }

  private flush() {
    if (this.ws.readyState !== WebSocket.OPEN) return;
    const pending = this.outbox;
    this.outbox = [];
    for (const p of pending) this.ws.send(JSON.stringify(p));
  }

  private send(payload: object) {
    if (this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload));
      return;
    }
    // Connecting or temporarily disconnected: queue (bounded) and flush on (re)connect 鈥?
    // never silently drop the user's message.
    if (this.outbox.length < 50) this.outbox.push(payload);
  }

  /** `model` = the composer's CURRENT selection, carried on every message so the turn uses
   * exactly what the user sees 鈥?immune to set_model races across reconnects (a new cowork
   * session always reconnects once to adopt its scratch dir, which could drop a queued
   * set_model and leave the engine on a stale/resumed model; found 2026-07-04). */
  userMessage(text: string, attachments?: unknown[], model?: string) {
    this.send({
      type: "user_message",
      text,
      ...(model ? { model } : {}),
      ...(attachments?.length ? { attachments } : {}),
    });
  }

  approve(decision: string) {
    this.send({ type: "approval", decision });
  }

  // Reply to a `request_directory` prompt: grant a folder (with access level) or decline.
  respondDirectory(granted: boolean, path?: string, writable?: boolean) {
    this.send({ type: "directory_response", granted, ...(path ? { path } : {}), writable: !!writable });
  }

  // Reply to a `propose_plan` prompt: approve (choosing the execution mode) or reject with feedback.
  respondPlan(approved: boolean, mode?: string, feedback?: string) {
    this.send({
      type: "plan_response",
      approved,
      ...(mode ? { mode } : {}),
      ...(feedback ? { feedback } : {}),
    });
  }

  // Answer a live `ask_user` prompt (attended sessions; unattended ones answer via the Inbox).
  respondQuestion(answer: string) {
    this.send({ type: "question_response", answer });
  }

  interrupt() {
    this.send({ type: "interrupt" });
  }

  // Re-run a turn that ended in a provider error 鈥?no new user message; the server
  // guards on the history tail so a stray frame is a no-op.
  retry() {
    this.send({ type: "retry" });
  }

  setMode(mode: string) {
    this.send({ type: "set_mode", mode });
  }

  setModel(model: string) {
    this.send({ type: "set_model", model });
  }

  close() {
    this.closed = true;
    if (this.reconnectTimer !== undefined) window.clearTimeout(this.reconnectTimer);
    // Detach before closing: this socket's async `close` event may land AFTER the
    // successor session's `open` (observed when switching into an automation-run
    // session), and a torn-down socket must not clobber the new one's connected state.
    this.ws.onopen = null;
    this.ws.onmessage = null;
    this.ws.onclose = null;
    this.ws.close();
  }
}

// -- team memory panel (strategy report 5.2.2) -------------------------------
export interface MemoryItem {
  id: number;
  scope: string;
  content: string;
  key?: string | null;
  workspace?: string | null;
  created_at?: string | null;
}

export async function listMemories(): Promise<{ memory: MemoryItem[] }> {
  const res = await apiFetch(`${httpBase()}/v1/memory`);
  return await res.json();
}

export async function searchMemories(
  query: string,
  k = 10,
): Promise<{ query: string; results: MemoryItem[] }> {
  const res = await apiFetch(`${httpBase()}/v1/memory/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, k }),
  });
  return await res.json();
}

export async function addMemory(
  content: string,
  scope = "workspace",
): Promise<MemoryItem> {
  const res = await apiFetch(`${httpBase()}/v1/memory`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, scope }),
  });
  return await res.json();
}

export async function updateMemory(
  id: number,
  content: string,
): Promise<{ ok: boolean; item?: MemoryItem; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/memory/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  return await res.json();
}

export async function deleteMemory(id: number): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/memory/${id}`, { method: "DELETE" });
  return await res.json();
}

// -- unified organizational asset search (asset loop Phase 2) ----------------
export interface AssetResults {
  knowledge: Array<{ id?: number; kind?: string; title?: string; score?: number; source_run_id?: string; use_count?: number; content?: string }>;
  skills: Array<{ name?: string; description?: string }>;
  templates: Array<{ id?: number; title?: string; runs_count?: number; success_count?: number }>;
  memories: Array<{ id?: number; scope?: string; content?: string }>;
  runs: Array<{ run_id?: string; status?: string; intent?: string }>;
}

export async function searchAssets(query: string, k = 8): Promise<{ query: string } & AssetResults> {
  const res = await apiFetch(`${httpBase()}/v1/assets/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, k }),
  });
  return await res.json();
}

// -- organizational rhythm (asset loop Phase 3) ------------------------------
export interface RhythmForecast {
  period_days: number;
  rhythm: string;
  upcoming: Array<{ id: string; title: string; next_run: number; cron?: string | null }>;
  generated_at: number;
}

export async function rhythmForecast(): Promise<RhythmForecast> {
  const res = await apiFetch(`${httpBase()}/v1/rhythm/forecast`);
  return await res.json();
}

export interface RhythmRecommendation {
  task_id: string;
  title: string;
  priority: string;
  cron: string;
  current_hour: number | null;
  recommended_hour: number;
  valley_share: number;
  runs: number;
  reason: string;
}

export interface RhythmRecommendations {
  period_days: number;
  rhythm: string;
  recommendations: RhythmRecommendation[];
}

export async function rhythmRecommendations(): Promise<RhythmRecommendations> {
  const res = await apiFetch(`${httpBase()}/v1/rhythm/recommendations`);
  return await res.json();
}

export async function setKnowledgeRetired(
  id: number,
  retired: boolean,
): Promise<{ ok: boolean; id?: number; retired?: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/knowledge/${id}/retire`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ retired }),
  });
  return await res.json();
}

// -- HORNET (铚傚发鍏辨尟绁炵粡鎷撴墤) 2D layer --------------------------------------

export interface HornetNode {
  id: number;
  title: string;
  x: number;
  y: number;
  z: number;
  phase: number[];
  degree: number;
  freshness?: number;
}

export interface HornetEdge {
  src: number;
  dst: number;
  relation: string;
  channel: string;
  weight: number;
}

export interface HornetHit {
  node_id: number;
  kb_item_id?: number | null; // 闂3: 鍏宠仈鐨勫師鏂囩煡璇嗘潯鐩?
  title: string;
  amplitude: number;
  path: string[];
  x: number;
  y: number;
  similarity: number;
  // manager 灞傞檮鍔犵殑鍘熸枃鍏冩暟鎹?(闂3: 鏌ョ湅璇︽儏/鎵撳紑鍘熸枃)
  source_path?: string | null;
  source_kind?: string | null;
  kb_title?: string | null;
}

export async function hornetBuild(rebuild = true, topo = false): Promise<{ nodes: number; edges: number; topo?: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/hornet/build`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rebuild, topo }),
  });
  return await res.json();
}

export async function hornetResonate(
  query: string,
  k = 10,
): Promise<{ run_id?: number; hits: HornetHit[]; query_phase?: number[]; warnings?: string[] }> {
  const res = await apiFetch(`${httpBase()}/v1/hornet/resonate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, k }),
  });
  return await res.json();
}

export async function hornetEvolve(limit = 20): Promise<{ emerged: number; counts: Record<string, number>; items: unknown[] }> {
  const res = await apiFetch(`${httpBase()}/v1/hornet/evolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ limit }),
  });
  return await res.json();
}

export async function hornetGraph(): Promise<{ nodes: HornetNode[]; edges: HornetEdge[] }> {
  const res = await apiFetch(`${httpBase()}/v1/hornet/graph`);
  return await res.json();
}

export async function hornetStats(): Promise<{
  nodes: number;
  edges: number;
  resonance_runs: number;
  emergent: number;
  emergent_items: { id: number; kind: string; title: string; detail: unknown }[];
}> {
  const res = await apiFetch(`${httpBase()}/v1/hornet/stats`);
  return await res.json();
}

export async function hornetEmergence(limit = 20): Promise<{
  unread: number;
  items: { id: number; kind: string; title: string; detail: unknown; status: string }[];
}> {
  const res = await apiFetch(`${httpBase()}/v1/hornet/emergence?limit=${limit}`);
  return await res.json();
}

export async function hornetMarkEmergence(
  id: number,
  status: "accepted" | "dismissed" = "accepted",
): Promise<{ ok: boolean; unread: number }> {
  const res = await apiFetch(`${httpBase()}/v1/hornet/emergence/${id}/mark`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  return await res.json();
}

export async function hornetHealth(): Promise<{
  score: number;
  rating: string;
  dimensions: { structure: number; dynamics: number; evolution: number };
  metrics: Record<string, unknown>;
}> {
  const res = await apiFetch(`${httpBase()}/v1/hornet/health`);
  return await res.json();
}

export async function hornetHealthReport(): Promise<{ ok: boolean; path?: string; score?: number }> {
  const res = await apiFetch(`${httpBase()}/v1/hornet/health-report`, { method: "POST" });
  return await res.json();
}

// -- P1-8: HORNET 娑岀幇 鈫?鑷姩鐢熸垚 Draft Skill --------------------------------

export async function hornetEmergenceToSkill(
  emergenceIndex = -1,
): Promise<{
  ok: boolean;
  skill?: string;
  emergence?: { id: number; kind: string; title: string; detail: unknown };
  error?: string;
}> {
  const res = await apiFetch(`${httpBase()}/v1/hornet/emergence-to-skill`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ emergence_index: emergenceIndex }),
  });
  return await res.json();
}

// -- P1-5: 闆朵俊浠昏兘鍔涜 (scope 閰嶇疆 + 鏉冮檺瀹¤鐑姏鍥? ------------------------

/** 鍏ㄩ噺杩炴帴鍣?scope 澹版槑琛? { connector: { tool: [scope, ...] } } */
export type ConnectorScopeMatrix = Record<string, Record<string, string[]>>;

export async function getConnectorScopes(): Promise<{ connectors: ConnectorScopeMatrix }> {
  const res = await apiFetch(`${httpBase()}/v1/permissions/scopes`);
  return await res.json();
}

export async function getPersonaScopes(
  personaId: string,
): Promise<{ persona_id: string; scopes: Record<string, string[]> }> {
  const res = await fetch(
    `${httpBase()}/v1/permissions/persona-scopes?persona_id=${encodeURIComponent(personaId)}`,
  );
  return await res.json();
}

export async function setPersonaScopes(
  personaId: string,
  connector: string,
  scopes: string[],
): Promise<{ ok: boolean; persona_id: string; connector: string; scopes: string[] }> {
  const res = await apiFetch(`${httpBase()}/v1/permissions/persona-scopes`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ persona_id: personaId, connector, scopes }),
  });
  return await res.json();
}

export interface PermissionHeatmapCell {
  persona: string;
  connector: string;
  tool: string;
  call_count: number;
  required_scopes: string[];
  max_scope_level: number;
  scope_escalations: number;
}

export async function getPermissionsHeatmap(): Promise<{
  matrix: PermissionHeatmapCell[];
  total_tools_tracked: number;
}> {
  const res = await apiFetch(`${httpBase()}/v1/permissions/heatmap`);
  return await res.json();
}

// -- 13 Agent 褰卞瓙妯″紡: 鍐崇瓥鍥炴斁杞ㄨ抗 ----------------------------------------

/** 鍗曟潯鍐崇瓥杞ㄨ抗 entry 鈥?鐢?engine._record_decision 鍐欏叆銆?*/
export interface DecisionTraceEntry {
  ts: number;
  iteration: number;
  kind:
    | "tool_selection"
    | "permission"
    | "scope_escalation"
    | "approval_resolution"
    | "plan_decision"
    | "directory_decision"
    | "question_decision";
  agent: string;
  session_id: string;
  // tool_selection
  available_tools?: string[];
  candidates?: Array<{ name: string; arguments: Record<string, unknown> }>;
  choice?: string[];
  reason?: string;
  // permission / approval_resolution
  tool?: string;
  allowed?: boolean;
  needs_user?: boolean;
  rule?: string;
  outcome?: string;
  // scope_escalation
  persona?: string;
  connector?: string;
  required_scopes?: string[];
  granted_scopes?: string[];
}

/** SwarmView 閫氳繃 orchestration 浜嬩欢娴佹敹鍒扮殑 decision_trace 鍖呰銆?*/
export interface SwarmDecisionEvent {
  worker: string;
  task_id: string;
  agent_id?: string;
  entry: DecisionTraceEntry;
}

export async function getDecisionTrace(
  sessionId: string,
): Promise<{
  session_id: string;
  trace: DecisionTraceEntry[];
  source: "live_engine" | "audit_store";
}> {
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/decision-trace`,
  );
  return await res.json();
}

export async function knowledgeResumeContext(
  id: number,
): Promise<{ ok: boolean; related: { title: string; snippet: string; amplitude: number }[] }> {
  const res = await apiFetch(`${httpBase()}/v1/knowledge/${id}/resume-context`);
  return await res.json();
}

export async function revealKnowledgeSource(path: string): Promise<{ ok: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/knowledge/reveal-source`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  return await res.json();
}

export async function knowledgeResumePack(
  id: number,
): Promise<{ ok: boolean; pack?: { title: string; content: string; source?: string | null; related: { title: string; snippet: string; amplitude: number }[] } }> {
  const res = await apiFetch(`${httpBase()}/v1/knowledge/${id}/resume-pack`);
  return await res.json();
}

export async function knowledgeResumeByTitle(
  title: string,
): Promise<{ ok: boolean; pack?: { title: string; content: string; source?: string | null; related: { title: string; snippet: string; amplitude: number }[] } }> {
  const res = await apiFetch(`${httpBase()}/v1/knowledge/resume-by-title`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  return await res.json();
}

export interface UsageTotals {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cached_tokens: number;
  cache_hit_rate: number;
  turns: number;
}

export interface SteadyStats {
  prompt_tokens: number;
  cached_tokens: number;
  turns: number;
  cache_hit_rate: number;
}

export async function getUsage(days = 14): Promise<{
  totals: UsageTotals;
  steady: SteadyStats;
  by_day: Array<{
    day: string;
    prompt_tokens: number;
    completion_tokens: number;
    cached_tokens: number;
    cache_hit_rate: number;
  }>;
  by_session: Array<{
    session_id: string;
    model: string;
    turns: number;
    prompt_tokens: number;
    completion_tokens: number;
    cached_tokens: number;
    cache_hit_rate: number;
  }>;
}> {
  const res = await apiFetch(`${httpBase()}/v1/usage?days=${days}`);
  if (!res.ok) throw new Error(`usage ${res.status}`);
  return await res.json();
}

// -- cache warm-up (P0 寤鸿1) ------------------------------------------------
export interface CacheWarmStatus {
  enabled: boolean;
  min_hit_rate: number;
  max_items: number;
  interval_hours: number;
  last_warm_at: number;
  week: { prompt_tokens: number; cached_tokens: number; calls: number };
  org_hit_rate: number;
}

export async function getCacheWarmStatus(): Promise<CacheWarmStatus> {
  const res = await apiFetch(`${httpBase()}/v1/cache/warm`);
  return await res.json();
}

export async function setCacheWarmEnabled(enabled: boolean): Promise<{ ok: boolean; enabled: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/cache/warm/toggle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  return await res.json();
}

export async function triggerCacheWarm(maxItems?: number): Promise<{ ok: boolean; warmed: number; prompt_tokens?: number; status?: CacheWarmStatus }> {
  const res = await apiFetch(`${httpBase()}/v1/cache/warm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(maxItems ? { max_items: maxItems } : {}),
  });
  return await res.json();
}

// 鈹€鈹€ Team / Organization API (Phase 0: types + graceful fallback) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

export interface TeamInfo {
  id: string;
  name: string;
  my_member_id: string;
  member_count: number;
  online_count: number;
  sync_status: "single" | "connected" | "connecting" | "offline";
  last_sync: number | null;
}

export interface Member {
  id: string;
  name: string;
  role: string;
  status: "online" | "offline";
  current_task_group: string | null;
  last_seen: number | null;
}

export interface AgentInstance {
  id: string;
  role: string;
  persona_id: string;
  state: "idle" | "working" | "fault";
  current_task_group: string | null;
  load: number;
  last_heartbeat: number;
}

export interface TaskGroup {
  group_id: string;
  goal: string;
  owner_member: string;
  member_ids: string[];
  agent_ids: string[];
  state: "forming" | "active" | "reviewing" | "dissolved";
  created_at: number;
  dissolved_at: number | null;
}

export interface PermissionCell {
  allowed: boolean;
  scope?: string;
  max_amount?: number;
}

export interface ApprovalThreshold {
  min_amount: number;
  max_amount: number | null;
  approver_role: string;
  require_human: boolean;
  require_board: boolean;
}

export interface PermissionMatrix {
  roles: Record<string, Record<string, PermissionCell>>;
  thresholds: ApprovalThreshold[];
}

export interface SyncStatus {
  status: "single" | "connected" | "connecting" | "offline";
  last_sync: number | null;
  pending_changes: number;
  peers_online: number;
}

// P2P 鍥㈤槦鍚屾 (璁捐鏂规绗叚绔?: 椤舵爮鍚屾鐘舵€佹寚绀哄櫒鏁版嵁婧愩€?
export async function getSyncStatus(): Promise<SyncStatus> {
  try {
    const res = await apiFetch(`${httpBase()}/v1/team/sync/status`);
    if (!res.ok) return { status: "single", last_sync: null, pending_changes: 0, peers_online: 0 };
    const d = await res.json();
    return {
      status: d.status === "connected" ? "connected" : d.status === "connecting" ? "connecting" : d.status === "offline" ? "offline" : "single",
      last_sync: d.last_sync ?? null,
      pending_changes: d.pending_changes ?? 0,
      peers_online: d.peers_online ?? 0,
    };
  } catch {
    return { status: "single", last_sync: null, pending_changes: 0, peers_online: 0 };
  }
}

export async function runTeamSync(): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await apiFetch(`${httpBase()}/v1/team/sync/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    return await res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

// Phase 0: these gracefully return empty/null when the team module isn't loaded yet.
// Phase 1 will wire them to real /v1/team/* endpoints.

export async function getTeam(): Promise<TeamInfo | null> {
  try {
    const res = await apiFetch(`${httpBase()}/v1/team`);
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export async function listMembers(): Promise<Member[]> {
  try {
    const res = await apiFetch(`${httpBase()}/v1/team/members`);
    if (!res.ok) return [];
    return await res.json();
  } catch {
    return [];
  }
}

export async function addMember(
  name: string,
  role: Member["role"] = "worker",
): Promise<Member> {
  const res = await apiFetch(`${httpBase()}/v1/team/members`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, role }),
  });
  return await res.json();
}

export async function updateMember(
  id: string,
  fields: Partial<{ role: Member["role"]; status: string; current_task_group: string }>,
): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/team/members/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  return await res.json();
}

export async function removeMember(id: string): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/team/members/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  return await res.json();
}

export async function listAgents(): Promise<AgentInstance[]> {
  try {
    const res = await apiFetch(`${httpBase()}/v1/team/agents`);
    if (!res.ok) return [];
    return await res.json();
  } catch {
    return [];
  }
}

export async function addAgent(
  role: AgentInstance["role"] = "worker",
  persona_id?: string,
): Promise<AgentInstance> {
  const res = await apiFetch(`${httpBase()}/v1/team/agents`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role, persona_id: persona_id ?? role }),
  });
  return await res.json();
}

export async function removeAgent(id: string): Promise<{ ok: boolean; id: string }> {
  const res = await apiFetch(`${httpBase()}/v1/team/agents/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  return await res.json();
}

export async function getAgentLoad(): Promise<Record<string, number>> {
  const res = await apiFetch(`${httpBase()}/v1/team/agents/load`);
  return await res.json();
}

export async function listTaskGroups(include_dissolved = false): Promise<TaskGroup[]> {
  try {
    const res = await fetch(
      `${httpBase()}/v1/team/task-groups?include_dissolved=${include_dissolved ? "1" : "0"}`,
    );
    if (!res.ok) return [];
    return await res.json();
  } catch {
    return [];
  }
}

export async function createTaskGroup(params: {
  goal: string;
  owner_member?: string;
  member_ids?: string[];
  agent_ids?: string[];
  group_id?: string;
}): Promise<TaskGroup> {
  const res = await apiFetch(`${httpBase()}/v1/team/task-groups`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return await res.json();
}

export async function transitionTaskGroup(
  id: string,
  state: string,
): Promise<TaskGroup> {
  const res = await fetch(
    `${httpBase()}/v1/team/task-groups/${encodeURIComponent(id)}/transition`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state }),
    },
  );
  return await res.json();
}

export async function dissolveTaskGroup(id: string): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/team/task-groups/${encodeURIComponent(id)}/dissolve`, {
    method: "POST",
  });
  return await res.json();
}

export async function getPermissions(): Promise<PermissionMatrix | null> {
  try {
    const res = await apiFetch(`${httpBase()}/v1/team/permissions`);
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

// 鈹€鈹€ ROI 浠峰€煎綊鍥?(寤鸿10: AI 鍥㈤槦璐︽湰) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
export interface RoiConfig {
  rates: Record<string, { prompt_ppm: number; completion_ppm: number }>;
  hourly_rate: number;
}

export interface RoiReport {
  month: string;
  total_cost: number;
  cache_saved: number;
  labor_hours: number;
  labor_saved: number;
  per_model: Array<{
    model: string; prompt_tokens: number; completion_tokens: number;
    cached_tokens: number; cost: number; cache_saved: number;
  }>;
  by_tag: Record<string, { runs: number; hours: number; cost: number }>;
  missing_rates: string[];
  rates_configured: boolean;
  hourly_rate: number;
  skill_reuse: { total_saved_seconds: number; per_template: Array<{ template_id: string; runs: number; reuse_count: number; saved_seconds: number }> };
}

export async function getRoiConfig(): Promise<RoiConfig> {
  const res = await apiFetch(`${httpBase()}/v1/roi/config`);
  return await res.json();
}

export async function setRoiConfig(rates: RoiConfig["rates"], hourlyRate: number): Promise<{ ok: boolean }> {
  const res = await apiFetch(`${httpBase()}/v1/roi/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rates, hourly_rate: hourlyRate }),
  });
  return await res.json();
}

export async function getRoiReport(month: string): Promise<RoiReport> {
  const res = await apiFetch(`${httpBase()}/v1/roi/report?month=${encodeURIComponent(month)}`);
  return await res.json();
}

export async function generateRoiReport(month: string): Promise<{ ok: boolean; path?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/roi/report/html`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ month }),
  });
  return await res.json();
}

export async function tagOrchestrateRun(runId: string, valueTag: string): Promise<{ ok: boolean; error?: string }> {
  const res = await apiFetch(`${httpBase()}/v1/orchestrate/${encodeURIComponent(runId)}/tag`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value_tag: valueTag }),
  });
  return await res.json();
}
