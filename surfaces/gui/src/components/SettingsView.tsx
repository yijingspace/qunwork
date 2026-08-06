import { useEffect, useState } from "react";
import {
  addMemory,
  deleteMemory,
  getSettings,
  getTrustedWorkspaces,
  listMemories,
  rhythmForecast,
  searchAssets,
  searchMemories,
  setKnowledgeRetired,
  setOnboarded,
  setPdfSettings,
  setScratchBase,
  setSessionsPeek,
  setWorkspaceTrusted,
  updateMemory,
  type AssetResults,
  type MemoryItem,
  type ModelSettings,
  type PdfSettings,
  type RhythmForecast,
  type WorkspaceCommandTrust,
} from "../api";
import {
  cancelDictationModelDownload,
  deleteDictationModel,
  downloadDictationModel,
  getAutostart,
  getDictationStatus,
  getKeepAwake,
  checkForUpdate,
  installUpdate,
  isTauri,
  listenDictationDownloadProgress,
  markDictationTestPassed,
  pickFolder,
  setAutostart,
  setKeepAwake,
  startDictation,
  stopDictation,
  verifyDictationModel,
  type DictationDownloadProgress,
  type DictationStatus,
} from "../tauri";
import { useThemePref } from "../theme";
import { Icon } from "./Icon";
import { PanelHead } from "./IntegrationsView";
import { ModelsTab } from "./ManageTabs";
import { GalleryModal } from "./GalleryModal";
import { PersonasTab } from "./PersonasTab";
import { showPersonas } from "../flags";
import { useT, useLanguage } from "../i18n";
import { exportTeamPackage, importTeamPackage } from "../api";

// Settings, restructured (Option 2) into a full-page surface that mirrors IntegrationsView's shell:
// a left sub-nav (Appearance · Files · Models · Personas) + centered panel, replacing the old
// top-tab ManageModal. Local/app concerns live here; anything external (Connectors, Messaging, MCP,
// Activity) stays under Integrations. Appearance + Files are re-skinned to the mock's Tailwind idiom;
// Models + Personas host the existing tab components inside the page shell (field re-skin to follow).
// "appearance" is the General tab's stable key — callers deep-link with it, so the
// rename (UX-021) changed only the label. "files" folded into General as a card.
type SetTab = "appearance" | "models" | "voice" | "personas";

const CARD = "rounded-xl2 border border-line bg-panel";
const FIELD_LABEL = "text-[12.5px] font-medium text-ink";
const FIELD_HELP = "text-[12px] text-muted mt-1.5 leading-relaxed";
const INPUT =
  "flex-1 min-w-0 px-3 py-2 rounded-lg border border-line bg-paper text-[13px] text-ink outline-none focus:border-accent";
const BTN_ACCENT = "text-[12.5px] px-3 py-2 rounded-lg bg-accent text-white shrink-0 disabled:opacity-40";
const BTN_BORDERED =
  "text-[12.5px] px-3 py-2 rounded-lg border border-line bg-paper hover:border-lineStrong shrink-0";

const SET_TABS: { key: SetTab; label: string; icon: "sliders" | "code" | "mic" | "sparkle" }[] = [
  { key: "appearance", label: "General", icon: "sliders" },
  { key: "models", label: "Models", icon: "code" },
  { key: "voice", label: "Voice input", icon: "mic" },
  { key: "personas", label: "Personas", icon: "sparkle" },
];

export function SettingsView({
  initialTab,
  onOpenPersona,
}: {
  initialTab?: SetTab;
  onOpenPersona?: (id: string) => void;
}) {
  const t = useT();
  // Personas is flag-gated (hidden for launch) — filter the tab AND coerce a stale
  // deep-link to it (openSettings("personas") callers) so the page never opens on a
  // section with no nav entry.
  const personas = showPersonas();
  const tabs = personas ? SET_TABS : SET_TABS.filter((t) => t.key !== "personas");
  const wanted = initialTab && (personas || initialTab !== "personas") ? initialTab : "appearance";
  const [tab, setTab] = useState<SetTab>(wanted);

  return (
    <main className="flex-1 min-w-0 flex bg-paper">
      <nav className="page-subnav w-[208px] shrink-0 border-r border-line bg-panel/40 px-3 py-4">
        <div className="px-2 text-[13.5px] font-semibold mb-3 flex items-center gap-2">
          <Icon name="gear" size={16} /> {t("Settings")}
        </div>
        {tabs.map((tabDef) => {
          const active = tab === tabDef.key;
          return (
            <button
              key={tabDef.key}
              className={
                "w-full text-left px-2.5 py-2 rounded-lg text-[13px] flex items-center gap-2 " +
                (active ? "bg-paper text-accent font-medium" : "text-muted hover:bg-paper hover:text-ink")
              }
              onClick={() => setTab(tabDef.key)}
            >
              <Icon name={tabDef.icon} size={15} /> {t(tabDef.label)}
            </button>
          );
        })}
      </nav>

      <div className="flex-1 min-w-0 overflow-y-auto hairline-scroll">
        <div className="max-w-3xl mx-auto px-7 py-6">
          {tab === "appearance" ? (
            <AppearanceSection />
          ) : tab === "models" ? (
            <section>
              <PanelHead
                title={t("Models")}
                sub={t("Providers and the models offered in the composer's picker. Keys are stored only on this computer.")}
              />
              <ModelsTab />
              {/* Token savings is model-spend behavior, so it lives here (UX-021),
                  not under General. */}
              <div className="mt-6">
                <TokenSavingsCard />
              </div>
            </section>
          ) : tab === "voice" ? (
            <VoiceInputSection />
          ) : (
            <PersonasSection onOpenPersona={onOpenPersona} />
          )}
        </div>
      </div>
    </main>
  );
}

// -- Voice input: deliberate model provisioning + compatibility + microphone test (§37) --------
const voiceError = (error: unknown) =>
  error instanceof Error ? error.message : typeof error === "string" ? error : "Voice Input could not complete that action.";

const formatBytes = (bytes: number) => {
  if (!bytes) return "0 MiB";
  return `${Math.round(bytes / 1024 / 1024)} MiB`;
};

function VoiceInputSection() {
  const t = useT();
  const [status, setStatus] = useState<DictationStatus | null>(null);
  const [progress, setProgress] = useState<DictationDownloadProgress | null>(null);
  const [phase, setPhase] = useState<"idle" | "downloading" | "verifying" | "testing" | "transcribing">("idle");
  const [error, setError] = useState<string | null>(null);
  const [testTranscript, setTestTranscript] = useState("");
  const desktop = isTauri();

  const publish = (next: DictationStatus) => {
    setStatus(next);
    window.dispatchEvent(new CustomEvent("coworker:voice-input-changed", { detail: next }));
  };

  useEffect(() => {
    if (!desktop) return;
    let active = true;
    let unlisten = () => {};
    void listenDictationDownloadProgress((next) => {
      if (active) setProgress(next);
    }).then((stop) => {
      unlisten = stop;
    });
    void getDictationStatus().then(async (initial) => {
      if (!active || !initial) return;
      publish(initial);
      // One-time migration for models installed by the first STT cut, before verification markers.
      if (initial.model_installed && !initial.model_verified) {
        setPhase("verifying");
        try {
          const verified = await verifyDictationModel();
          if (active) publish(verified);
        } catch (verifyError) {
          if (active) setError(voiceError(verifyError));
        } finally {
          if (active) setPhase("idle");
        }
      }
    });
    return () => {
      active = false;
      unlisten();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [desktop]);

  const download = async () => {
    setError(null);
    setProgress({ downloaded_bytes: 0, total_bytes: status?.model_bytes || 0 });
    setPhase("downloading");
    try {
      publish(await downloadDictationModel());
    } catch (downloadError) {
      setError(voiceError(downloadError));
      const latest = await getDictationStatus();
      if (latest) publish(latest);
    } finally {
      setPhase("idle");
    }
  };

  const cancelDownload = async () => {
    await cancelDictationModelDownload().catch(() => undefined);
  };

  const repair = async () => {
    setError(null);
    try {
      publish(await deleteDictationModel());
      await download();
    } catch (repairError) {
      setError(voiceError(repairError));
    }
  };

  const remove = async () => {
    if (!window.confirm(t("Delete the local Whisper model and disable Voice Input?"))) return;
    setError(null);
    try {
      publish(await deleteDictationModel());
      setTestTranscript("");
      setProgress(null);
    } catch (deleteError) {
      setError(voiceError(deleteError));
    }
  };

  const toggleTest = async () => {
    if (!status?.supported || !status.model_verified) return;
    setError(null);
    try {
      if (status.recording) {
        setPhase("transcribing");
        const transcript = (await stopDictation()).trim();
        setTestTranscript(transcript);
        if (!transcript) throw new Error(t("No speech was detected. Try again and speak for a little longer."));
        publish(await markDictationTestPassed());
      } else {
        setTestTranscript("");
        setPhase("testing");
        publish(await startDictation());
      }
    } catch (testError) {
      setError(voiceError(testError));
      const latest = await getDictationStatus();
      if (latest) publish(latest);
    } finally {
      setPhase("idle");
    }
  };

  const downloading = phase === "downloading" || !!status?.download_in_progress;
  const progressTotal = progress?.total_bytes || status?.model_bytes || 1;
  const progressPercent = Math.min(100, Math.round(((progress?.downloaded_bytes || 0) / progressTotal) * 100));
  const ready = !!status?.supported && !!status?.model_verified && !!status?.test_passed;

  return (
    <section>
      <PanelHead
        title={t("Voice input")}
        sub={t("Speak naturally in the composer. Recordings and transcripts stay on this device.")}
      />

      {!desktop ? (
        <div className={CARD + " p-4 text-[13px] text-muted"}>{t("Voice Input setup is available in the QunWork desktop app.")}</div>
      ) : (
        <div className="space-y-4">
          <div className="rounded-xl border border-green-200 bg-green-50/70 px-4 py-3 text-[12.5px] text-green-800">
            <span className="font-medium">{t("Private by design.")}</span> {t("Audio is held in memory only while you record and is transcribed locally.")}
          </div>

          <div className={CARD}>
            <div className="p-4 flex items-start gap-3">
              <Icon name="code" size={18} className="text-accent mt-0.5" />
              <div className="min-w-0 flex-1">
                <div className="text-[13.5px] font-medium">{t("This device")}</div>
                <div className="text-[12px] text-muted mt-1">{status?.device_summary || t("Checking compatibility…")}</div>
                {status?.compatibility_reason && <div className="text-[12px] text-red-600 mt-1.5">{status.compatibility_reason}</div>}
              </div>
              {status && (
                <span className={"text-[11.5px] px-2 py-1 rounded-full " + (status.supported ? "bg-green-50 text-green-700" : "bg-red-50 text-red-600")}>
                  {status.supported ? t("● Compatible") : t("Unsupported")}
                </span>
              )}
            </div>
            <div className="border-t border-line bg-paper/50 px-4 py-3 grid grid-cols-2 gap-3 text-[12px] text-muted">
              <div><span className="block text-ink font-medium">Mac</span>macOS 12+ · Apple Silicon M1+</div>
              <div><span className="block text-ink font-medium">Windows</span>Windows 10 22H2/11 · x64</div>
              <div><span className="block text-ink font-medium">{t("Memory")}</span>{t("8 GB recommended")}</div>
              <div><span className="block text-ink font-medium">{t("Processor")}</span>{t("4 CPU cores recommended")}</div>
            </div>
          </div>

          <div className={CARD}>
            <div className="p-4 flex items-center gap-3">
              <div className="w-9 h-9 rounded-lg bg-accentSoft text-accent grid place-items-center font-semibold">W</div>
              <div className="min-w-0 flex-1">
                <div className="text-[13.5px] font-medium">{t("Whisper Base · English")}</div>
                <div className="text-[12px] text-muted mt-0.5">
                  {status?.model_verified ? t("Installed and verified · {size}", { size: formatBytes(status.model_bytes) }) : t("Local voice model · {size}", { size: formatBytes(status?.model_bytes || 147_964_211) })}
                </div>
              </div>
              {status?.model_verified ? (
                <>
                  <span className="text-[11.5px] px-2 py-1 rounded-full bg-green-50 text-green-700">{t("Verified")}</span>
                  <button className={BTN_BORDERED} onClick={() => void repair()}>{t("Repair")}</button>
                  <button className="text-[12px] text-red-600 px-2 py-2" onClick={() => void remove()}>{t("Delete")}</button>
                </>
              ) : downloading ? (
                <button className={BTN_BORDERED} onClick={() => void cancelDownload()}>{t("Cancel")}</button>
              ) : phase === "verifying" ? (
                <span className="text-[12px] text-muted">{t("Verifying…")}</span>
              ) : (
                <button className={BTN_ACCENT} disabled={!status?.supported} onClick={() => void download()}>{t("Download model")}</button>
              )}
            </div>
            {downloading && (
              <div className="border-t border-line px-4 py-3">
                <div className="h-1.5 rounded-full bg-line overflow-hidden"><div className="h-full bg-accent transition-all" style={{ width: `${progressPercent}%` }} /></div>
                <div className="mt-1.5 text-[11.5px] text-muted flex"><span>{formatBytes(progress?.downloaded_bytes || 0)} of {formatBytes(progressTotal)}</span><span className="ml-auto">{progressPercent}%</span></div>
              </div>
            )}
          </div>

          <div className={CARD}>
            <div className="p-4 flex items-center gap-3">
              <Icon name="mic" size={18} className={ready ? "text-green-600" : "text-muted"} />
              <div className="min-w-0 flex-1">
                <div className="text-[13.5px] font-medium">Microphone test</div>
                <div className="text-[12px] text-muted mt-0.5">
                  {ready ? t("Your microphone and local transcription engine are working.") : t("Record a short phrase to enable the composer microphone.")}
                </div>
              </div>
              {ready && <span className="text-[11.5px] px-2 py-1 rounded-full bg-green-50 text-green-700">{t("● Ready")}</span>}
              <button className={BTN_BORDERED} disabled={!status?.supported || !status?.model_verified || phase === "transcribing"} onClick={() => void toggleTest()}>
                {status?.recording ? t("Stop and check") : phase === "transcribing" ? t("Transcribing…") : ready ? t("Test again") : t("Test microphone")}
              </button>
            </div>
            {status?.recording && <div className="border-t border-line px-4 py-3 text-[12px] text-accent" role="status">{t("● Listening… speak a short phrase, then stop.")}</div>}
            {testTranscript && <div className="border-t border-line bg-paper/50 px-4 py-3 text-[13px]">“{testTranscript}”</div>}
          </div>

          {error && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2.5 text-[12px] text-red-700">{error}</div>}
        </div>
      )}
    </section>
  );
}

// -- Personas: installed/enabled/delete management, the dir/Git importer, and the
// entry point to the Persona Gallery (a screen-sized modal — installs finish back
// here, disabled pending consent; a gallery install re-mounts the list in place).
function PersonasSection({ onOpenPersona }: { onOpenPersona?: (id: string) => void }) {
  const t = useT();
  const [galleryBump, setGalleryBump] = useState(0);
  const [galleryOpen, setGalleryOpen] = useState(false);

  return (
    <section>
      <PanelHead
        title={t("Personas")}
        sub={t("Which coworkers are enabled and shown in the picker, plus installing new persona bundles.")}
      />
      <PersonasTab key={galleryBump} onOpenPersona={onOpenPersona} />
      <button
        className="mt-6 w-full rounded-xl2 border border-line bg-panel px-4 py-3.5 flex items-center gap-3 text-left hover:border-lineStrong"
        data-testid="gallery-link"
        onClick={() => setGalleryOpen(true)}
      >
        <Icon name="sparkle" size={16} className="text-accent shrink-0" />
        <span className="min-w-0 flex-1">
          <span className="block text-[13.5px] font-medium">{t("Browse the Persona Gallery")}</span>
          <span className="block text-[12px] text-muted">
            {t("Curated coworkers from the QunWork team — see what each can do before installing.")}
          </span>
        </span>
        <span className="text-[12.5px] text-accent shrink-0">{t("Open →")}</span>
      </button>
      {galleryOpen && (
        <GalleryModal
          onClose={() => setGalleryOpen(false)}
          onInstalled={() => setGalleryBump((b) => b + 1)}
        />
      )}
    </section>
  );
}

// -- Appearance + app behaviour ------------------------------------------------
function AppearanceSection() {
  const t = useT();
  const { pref: langPref, setLanguage } = useLanguage();
  const [theme, setTheme] = useThemePref();
  const [autostart, setAuto] = useState(false);
  const [keepAwake, setKeep] = useState(false);
  const desktop = isTauri();

  useEffect(() => {
    if (isTauri()) {
      getAutostart().then((v) => setAuto(!!v));
      getKeepAwake().then((v) => setKeep(!!v));
    }
  }, []);

  const toggleAuto = async (v: boolean) => setAuto(!!(await setAutostart(v)));
  const toggleKeep = async (v: boolean) => setKeep(!!(await setKeepAwake(v)));
  const runSetupAgain = async () => {
    await setOnboarded(false);
    window.dispatchEvent(new CustomEvent("coworker:open-onboarding"));
  };

  return (
    <section>
      <PanelHead title={t("General")} sub={t("How QunWork looks and behaves on this machine.")} />

      <div className={CARD + " p-4 mb-4"}>
        <div className={FIELD_LABEL}>Theme</div>
        <div className="seg mt-2.5" role="radiogroup" aria-label={t("Appearance")}>
          {(["light", "dark", "auto"] as const).map((p) => (
            <button key={p} className={p === theme ? "active" : ""} onClick={() => setTheme(p)}>
              {p === "light" ? t("Light") : p === "dark" ? t("Dark") : t("Auto")}
            </button>
          ))}
        </div>
        <div className={FIELD_HELP}>{t("Auto follows your device's appearance.")}</div>
      </div>

      <div className={CARD + " p-4 mb-4"}>
        <div className={FIELD_LABEL}>{t("Language")}</div>
        <div className="seg mt-2.5" role="radiogroup" aria-label={t("Language")} data-testid="settings-language">
          {(["auto", "zh", "en"] as const).map((p) => (
            <button key={p} className={langPref === p ? "active" : ""} onClick={() => setLanguage(p)}>
              {p === "auto" ? t("Follow system") : p === "zh" ? "中文" : "English"}
            </button>
          ))}
        </div>
        <div className={FIELD_HELP}>{t("Language setting is saved on this device.")}</div>
      </div>

      <SidebarCard />

      <FilesCard />

      <TrustedWorkspacesCard />
      <TeamWorkspaceCard />
      <TeamMemoryCard />
      <OrgAssetsCard />
      <RhythmCard />

      {desktop && (
        <div className={CARD + " p-4"}>
          <div className={FIELD_LABEL + " mb-2.5"}>{t("Always-on")}</div>
          <label className="flex items-start gap-3 py-2">
            <input type="checkbox" className="mt-0.5" checked={autostart} onChange={(e) => toggleAuto(e.target.checked)} />
            <span>
              <span className="block text-[13px] text-ink">{t("Open at login")}</span>
              <span className="block text-[12px] text-muted">{t("Launch QunWork automatically when you sign in.")}</span>
            </span>
          </label>
          <label className="flex items-start gap-3 py-2">
            <input type="checkbox" className="mt-0.5" checked={keepAwake} onChange={(e) => toggleKeep(e.target.checked)} />
            <span>
              <span className="block text-[13px] text-ink">{t("Keep this system awake")}</span>
              <span className="block text-[12px] text-muted">{t("Prevent idle sleep so scheduled tasks fire on time.")}</span>
            </span>
          </label>
        </div>
      )}

      {/* One card for the app-lifecycle actions (UX-021): the onboarding replay (§24 —
          every build, the browser dev shell runs the same first-run flow) and, on
          desktop, the manual update check (launch also checks automatically). */}
      <div className={CARD + " p-4 mt-4"}>
        <div className={FIELD_LABEL + " mb-2"}>{t("Setup & updates")}</div>
        <div className="flex items-center gap-2">
          <button className={BTN_BORDERED} onClick={runSetupAgain}>
            {t("Run setup again")}
          </button>
          {desktop && <UpdateInline />}
        </div>
        <div className={FIELD_HELP}>Replays the first-run setup: model, first automation, tips.</div>
      </div>
    </section>
  );
}

function TeamWorkspaceCard() {
  const t = useT();
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [exported, setExported] = useState<string | null>(null);
  const [importPath, setImportPath] = useState("");
  const [importResult, setImportResult] = useState<string | null>(null);
  const [teamErr, setTeamErr] = useState<string | null>(null);

  const doExport = async () => {
    setExporting(true);
    setTeamErr(null);
    const res = await exportTeamPackage();
    setExporting(false);
    if (res.ok && res.path) setExported(res.path);
    else setTeamErr(res.error || "export failed");
  };

  const doImport = async () => {
    if (!importPath.trim()) return;
    setImporting(true);
    setTeamErr(null);
    const res = await importTeamPackage(importPath.trim());
    setImporting(false);
    if (res.ok) {
      const counts = res.imported ?? {};
      setImportResult(
        `${t("Imported")}: ${counts.swarm_templates ?? 0} ${t("templates")}, ` +
          `${counts.task_templates ?? 0} ${t("tasks")}, ` +
          `${counts.knowledge ?? 0} ${t("knowledge")}, ${counts.skills ?? 0} ${t("skills")}`,
      );
    } else setTeamErr(res.error || "import failed");
  };

  return (
    <div className={CARD + " p-4 mb-4"} data-testid="team-workspace-card">
      <div className={FIELD_LABEL}>{t("Team workspace")}</div>
      <div className={FIELD_HELP}>
        {t("Export your templates, knowledge and skills into a team package — import it on another machine.")}
      </div>
      <div className="flex items-center gap-2 mt-2.5">
        <button className={BTN_BORDERED} disabled={exporting} onClick={() => void doExport()}>
          {exporting ? t("Exporting…") : t("Export team package")}
        </button>
      </div>
      {exported && <div className="text-[12px] text-muted mt-2 break-all">{exported}</div>}
      <div className="flex items-center gap-2 mt-2.5">
        <input
          className="flex-1 min-w-0 px-3 py-2 rounded-lg border bg-panel text-[12.5px] outline-none focus:border-accent"
          placeholder={t("Path to a team package .zip")}
          value={importPath}
          onChange={(e) => setImportPath(e.target.value)}
        />
        <button className={BTN_BORDERED} disabled={importing || !importPath.trim()} onClick={() => void doImport()}>
          {importing ? t("Importing…") : t("Import")}
        </button>
      </div>
      {importResult && <div className="text-[12px] text-muted mt-2">{importResult}</div>}
      {teamErr && <div className="text-[12px] text-danger mt-2">{teamErr}</div>}
    </div>
  );
}

function TeamMemoryCard() {
  const t = useT();
  const [items, setItems] = useState<MemoryItem[] | null>(null);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [memErr, setMemErr] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [newText, setNewText] = useState("");
  const [newScope, setNewScope] = useState("workspace");
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editText, setEditText] = useState("");

  const loadAll = () => {
    listMemories()
      .then((r) => setItems(r.memory ?? []))
      .catch(() => setItems([]));
  };
  useEffect(() => {
    loadAll();
  }, []);

  const doSearch = async () => {
    if (!query.trim()) {
      loadAll();
      return;
    }
    setSearching(true);
    setMemErr(null);
    try {
      const r = await searchMemories(query.trim(), 20);
      setItems(r.results ?? []);
    } catch (e) {
      setMemErr(String(e));
    } finally {
      setSearching(false);
    }
  };

  const doAdd = async () => {
    if (!newText.trim()) return;
    setAdding(true);
    setMemErr(null);
    try {
      await addMemory(newText.trim(), newScope);
      setNewText("");
      loadAll();
    } catch (e) {
      setMemErr(String(e));
    } finally {
      setAdding(false);
    }
  };

  const saveEdit = async (id: number) => {
    if (!editText.trim()) return;
    const r = await updateMemory(id, editText.trim());
    if (r.ok) {
      setEditingId(null);
      loadAll();
    } else setMemErr(r.error || "update failed");
  };

  const clear = async (id: number) => {
    if (!window.confirm(t("Forget this memory?"))) return;
    await deleteMemory(id);
    loadAll();
  };

  return (
    <div className={CARD + " p-4 mb-4"} data-testid="team-memory-card">
      <div className={FIELD_LABEL}>{t("Team memory")}</div>
      <div className={FIELD_HELP}>
        {t("What the swarm remembers across sessions and workers — search, edit or forget it.")}
      </div>

      <div className="flex items-center gap-2 mt-2.5">
        <input
          className="flex-1 min-w-0 px-3 py-2 rounded-lg border bg-panel text-[12.5px] outline-none focus:border-accent"
          placeholder={t("Search memories…")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void doSearch()}
        />
        <button className={BTN_BORDERED} disabled={searching} onClick={() => void doSearch()}>
          {searching ? "…" : t("Search")}
        </button>
      </div>

      <div className="flex items-center gap-2 mt-2">
        <input
          className="flex-1 min-w-0 px-3 py-2 rounded-lg border bg-panel text-[12.5px] outline-none focus:border-accent"
          placeholder={t("Add a durable fact the team should keep…")}
          value={newText}
          onChange={(e) => setNewText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void doAdd()}
        />
        <select
          className="px-2 py-2 rounded-lg border bg-panel text-[12.5px] outline-none"
          value={newScope}
          onChange={(e) => setNewScope(e.target.value)}
        >
          <option value="workspace">{t("Workspace")}</option>
          <option value="global">{t("Global")}</option>
        </select>
        <button className={BTN_BORDERED} disabled={adding || !newText.trim()} onClick={() => void doAdd()}>
          {adding ? "…" : t("Add")}
        </button>
      </div>

      {memErr && <div className="text-[12px] text-danger mt-2">{memErr}</div>}
      {items === null ? null : items.length === 0 ? (
        <div className="text-[12px] text-faint mt-2.5">{t("No memories yet.")}</div>
      ) : (
        <div className="mt-2.5 space-y-1.5 max-h-72 overflow-y-auto pr-1">
          {items.map((m) => (
            <div key={m.id} className="rounded-lg border border-line bg-panel px-3 py-2">
              {editingId === m.id ? (
                <div className="flex items-start gap-2">
                  <textarea
                    className="flex-1 min-w-0 px-2 py-1.5 rounded border bg-panel text-[12.5px] outline-none focus:border-accent resize-none"
                    rows={2}
                    value={editText}
                    onChange={(e) => setEditText(e.target.value)}
                  />
                  <button className={BTN_BORDERED} onClick={() => void saveEdit(m.id)}>{t("Save")}</button>
                  <button className="text-[12px] text-faint" onClick={() => setEditingId(null)}>{t("Cancel")}</button>
                </div>
              ) : (
                <div>
                  <div className="text-[12.5px] text-ink break-words leading-relaxed">{m.content}</div>
                  <div className="flex items-center gap-2 mt-1">
                    <span className="text-[10.5px] uppercase tracking-wide text-faint border border-line rounded px-1 py-px">
                      {m.scope}
                    </span>
                    {m.key && <span className="text-[10.5px] text-faint font-mono">#{m.key}</span>}
                    {m.created_at && <span className="text-[10.5px] text-faint">{m.created_at}</span>}
                    <span className="flex-1" />
                    <button className="text-[11px] text-muted hover:text-ink" onClick={() => { setEditingId(m.id); setEditText(m.content); }}>
                      {t("Edit")}
                    </button>
                    <button className="text-[11px] text-muted hover:text-danger" onClick={() => void clear(m.id)}>
                      {t("Forget")}
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function OrgAssetsCard() {
  const t = useT();
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<AssetResults | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);

  const doSearch = async () => {
    if (!query.trim()) {
      setResults(null);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      setResults(await searchAssets(query.trim(), 8));
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const retire = async (id: number) => {
    if (!window.confirm(t("Retire this knowledge entry?"))) return;
    await setKnowledgeRetired(id, true);
    doSearch();
  };

  const count = (a: unknown[] | undefined) => a?.length ?? 0;

  return (
    <div className={CARD + " p-4 mb-4"} data-testid="org-assets-card">
      <div className={FIELD_LABEL}>{t("Organizational assets")}</div>
      <div className={FIELD_HELP}>
        {t("One search across knowledge, skills, templates, team memory and swarm runs — the asset loop.")}
      </div>

      <div className="flex items-center gap-2 mt-2.5">
        <input
          className="flex-1 min-w-0 px-3 py-2 rounded-lg border bg-panel text-[12.5px] outline-none focus:border-accent"
          placeholder={t("Search all organizational assets…")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void doSearch()}
        />
        <button className={BTN_BORDERED} disabled={busy} onClick={() => void doSearch()}>
          {busy ? "…" : t("Search")}
        </button>
      </div>

      {err && <div className="text-[12px] text-danger mt-2">{err}</div>}

      {results && (
        <div className="mt-2.5 space-y-2">
          {[
            { key: "knowledge" as const, label: t("Knowledge"), rows: results.knowledge.map((k) => ({
                id: String(k.id), title: k.title ?? "", sub: `${k.kind ?? ""}${k.source_run_id ? " · " + k.source_run_id : ""}`,
                meta: k, expanded: expanded === k.id, useCount: k.use_count ?? 0,
                onToggle: () => setExpanded(expanded === k.id ? null : (k.id ?? null)),
                onRetire: () => (k.id != null ? void retire(k.id) : undefined),
              })) },
            { key: "skills" as const, label: t("Skills"), rows: results.skills.map((s) => ({
                id: s.name ?? "", title: s.name ?? "", sub: s.description ?? "",
              })) },
            { key: "templates" as const, label: t("Templates"), rows: results.templates.map((m) => ({
                id: String(m.id), title: m.title ?? "", sub: `${m.runs_count ?? 0} ${t("runs")} · ${m.success_count ?? 0} ${t("ok")}`,
              })) },
            { key: "memories" as const, label: t("Team memory"), rows: results.memories.map((m) => ({
                id: String(m.id), title: m.content ?? "", sub: m.scope ?? "",
              })) },
            { key: "runs" as const, label: t("Swarm runs"), rows: results.runs.map((r) => ({
                id: r.run_id ?? "", title: r.intent ?? "", sub: r.status ?? "",
              })) },
          ].map((group) =>
            group.rows.length > 0 ? (
              <div key={group.key}>
                <div className="text-[10.5px] uppercase tracking-[0.07em] text-faint font-semibold mb-1">
                  {group.label} · {group.rows.length}
                </div>
                {group.rows.slice(0, 4).map((row) => (
                  <div key={group.key + row.id} className="text-[12px] leading-relaxed">
                    <button
                      className="w-full text-left text-ink truncate hover:text-accent"
                      onClick={(row as { onToggle?: () => void }).onToggle}
                    >
                      {row.title}
                      {(row as { useCount?: number }).useCount != null && (
                        <span className="text-faint ml-1.5">↻ {(row as { useCount?: number }).useCount}</span>
                      )}
                      {row.sub ? <span className="text-faint"> — {row.sub}</span> : null}
                    </button>
                    {(row as { expanded?: boolean }).expanded && (
                      <div className="text-[11.5px] text-muted bg-paper rounded px-2 py-1.5 mt-1 whitespace-pre-wrap break-words max-h-28 overflow-y-auto">
                        {((row as { meta?: { content?: string } }).meta?.content) || ""}
                      </div>
                    )}
                    {group.key === "knowledge" && (row as { onRetire?: () => void }).onRetire && (
                      <button
                        className="text-[10.5px] text-muted hover:text-danger ml-1"
                        onClick={(row as { onRetire?: () => void }).onRetire}
                      >
                        {t("Retire")}
                      </button>
                    )}
                  </div>
                ))}
              </div>
            ) : null,
          )}
          {count(results.knowledge) + count(results.skills) + count(results.templates) + count(results.memories) + count(results.runs) === 0 && (
            <div className="text-[12px] text-faint">{t("No assets matched.")}</div>
          )}
        </div>
      )}
    </div>
  );
}

function RhythmCard() {
  const t = useT();
  const [data, setData] = useState<RhythmForecast | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    rhythmForecast()
      .then(setData)
      .catch((e) => setErr(String(e)));
  }, []);

  const rhythmLabel = (r: string) =>
    r === "weekly" ? t("Weekly rhythm") : r === "daily" ? t("Daily rhythm") : r === "monthly" ? t("Monthly rhythm") : r === "irregular" ? t("Irregular — no dominant cadence yet") : t("Rhythm: {p}", { p: r });

  const fmt = (ts?: number) =>
    ts ? new Date(ts * 1000).toLocaleDateString() : "";

  return (
    <div className={CARD + " p-4 mb-4"} data-testid="rhythm-card">
      <div className={FIELD_LABEL}>{t("Organizational rhythm")}</div>
      <div className={FIELD_HELP}>
        {t("The org's dominant cadence, detected from run history — and what's due next week.")}
      </div>

      {err && <div className="text-[12px] text-danger mt-2">{err}</div>}
      {data && (
        <div className="mt-2.5">
          <div className="flex items-center gap-2">
            <span className="text-[12.5px] text-ink font-medium">
              {data.period_days > 0 ? t("Period detected: {n} days", { n: data.period_days }) : t("No period detected yet")}
            </span>
            <span className="text-[11px] text-faint border border-line rounded px-1.5 py-0.5">
              {rhythmLabel(data.rhythm)}
            </span>
          </div>
          <div className="text-[10.5px] uppercase tracking-[0.07em] text-faint font-semibold mt-3 mb-1">
            {t("Due in the next 7 days")}
          </div>
          {data.upcoming.length === 0 ? (
            <div className="text-[12px] text-faint">{t("Nothing scheduled.")}</div>
          ) : (
            <div className="space-y-1">
              {data.upcoming.map((u) => (
                <div key={u.id} className="flex items-center gap-2 text-[12px]">
                  <span className="text-faint font-mono w-20 shrink-0">{fmt(u.next_run)}</span>
                  <span className="text-ink truncate">{u.title}</span>
                  {u.cron ? <span className="text-faint font-mono text-[10.5px]">{u.cron}</span> : null}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function TrustedWorkspacesCard() {  const t = useT();
  const [workspaces, setWorkspaces] = useState<WorkspaceCommandTrust[] | null>(null);

  const refresh = () =>
    getTrustedWorkspaces()
      .then(setWorkspaces)
      .catch(() => setWorkspaces([]));

  useEffect(() => {
    refresh();
  }, []);

  const revoke = async (path: string) => {
    if (!window.confirm(`Revoke command trust for ${path}?`)) return;
    await setWorkspaceTrusted(path, false);
    refresh();
  };

  return (
    <div className={CARD + " p-4 mb-4"} data-testid="trusted-workspaces-card">
      <div className={FIELD_LABEL}>{t("Trusted workspaces")}</div>      <div className={FIELD_HELP}>
        {t("Trusted projects may manage their command allowances in .coworker/config.toml.")}
      </div>
      {workspaces === null ? (
        <div className="text-[12px] text-muted mt-3">{t("Loading…")}</div>
      ) : workspaces.length === 0 ? (
        <div className="text-[12px] text-muted mt-3">{t("No workspaces are trusted.")}</div>
      ) : (
        <div className="mt-3 divide-y divide-line">
          {workspaces.map((workspace) => (
            <div key={workspace.workspace} className="py-2.5 flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <div className="text-[12.5px] text-ink break-all">{workspace.workspace}</div>
                <div className="text-[11.5px] text-muted mt-0.5">
                  {workspace.requested_commands.length
                    ? t("{n} project command allowance(s)", { n: workspace.requested_commands.length })
                    : t("No project command allowances currently declared")}
                  {!workspace.exists ? " · " + t("Folder unavailable") : ""}
                </div>
              </div>
              <button
                className="text-[12px] text-red-600 px-2 py-1"
                onClick={() => void revoke(workspace.workspace)}
              >
                {t("Revoke")}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function UpdateInline() {
  const t = useT();
  const [state, setState] = useState<"idle" | "checking" | "none" | "found" | "installing" | "error">("idle");
  const [version, setVersion] = useState("");

  const check = async () => {
    setState("checking");
    try {
      const u = await checkForUpdate();
      if (u) {
        setVersion(u.version);
        setState("found");
      } else {
        setState("none");
      }
    } catch {
      setState("error");
    }
  };

  const install = async () => {
    setState("installing");
    try {
      await installUpdate(); // success restarts the app
    } catch {
      setState("error");
    }
  };

  return (
    <span className="inline-flex items-center gap-2.5">
      {state === "found" ? (
        <button className={BTN_BORDERED} onClick={install} data-testid="settings-update-install">
          {t("Update to v{version} and restart", { version })}
        </button>
      ) : (
        <button
          className={BTN_BORDERED}
          onClick={check}
          disabled={state === "checking" || state === "installing"}
          data-testid="settings-update-check"
        >
          {state === "checking" ? t("Checking…") : t("Check for updates")}
        </button>
      )}
      {(state === "none" || state === "error" || state === "installing") && (
        <span className="text-[12px] text-muted">
          {state === "none"
            ? t("You're on the latest version.")
            : state === "error"
              ? t("Couldn't check right now — try again later.")
              : t("Downloading — QunWork restarts by itself when it's ready.")}
        </span>
      )}
    </span>
  );
}

// Telemetry/Privacy card removed for this release (owner ask 2026-07-22); the
// setCloudTelemetry API stays for a future opt-out surface.

// -- Sidebar density -------------------------------------------------------------
// -- Token savings (PDF attachments; owner ask, 2026-07-17) ---------------------
// Attachments replay with EVERY turn, so a big PDF quietly multiplies token spend.
// Auto-compaction of long histories is a planned follow-up (punchlist §7) — until
// then this card is the user's dial: attach thresholds + the fallback for models
// without native PDF support.
function TokenSavingsCard() {
  const t = useT();
  const [pdf, setPdf] = useState<PdfSettings | null>(null);

  useEffect(() => {
    getSettings()
      .then((s) =>
        setPdf({
          pdf_fallback: s.pdf_fallback || "text",
          pdf_max_pages: s.pdf_max_pages || 20,
          pdf_max_mb: s.pdf_max_mb || 10,
        }),
      )
      .catch(() => setPdf({ pdf_fallback: "text", pdf_max_pages: 20, pdf_max_mb: 10 }));
  }, []);

  const save = async (patch: Partial<PdfSettings>) => {
    setPdf((p) => (p ? { ...p, ...patch } : p));
    await setPdfSettings(patch);
  };

  if (!pdf) return null;
  return (
    <div className={CARD + " p-4 mb-4"} data-testid="token-savings-card">
      <div className={FIELD_LABEL}>{t("Token savings")}</div>
      <div className={FIELD_HELP}>
        PDF attachments travel with every turn of a conversation, so large documents multiply
        what you spend on tokens.
      </div>

      <div className="mt-3 text-[13px] text-ink">{t("PDFs on models without native PDF support")}</div>
      <div className="seg mt-2" role="radiogroup" aria-label={t("PDF fallback")} data-testid="pdf-fallback">
        <button
          className={pdf.pdf_fallback === "text" ? "active" : ""}
          onClick={() => save({ pdf_fallback: "text" })}
        >
          {t("Extract text")}
        </button>
        <button
          className={pdf.pdf_fallback === "images" ? "active" : ""}
          onClick={() => save({ pdf_fallback: "images" })}
        >
          {t("Send page images")}
        </button>
      </div>
      <div className={FIELD_HELP}>
        Claude, GPT and Gemini read PDFs natively — this only applies to models that
        don&rsquo;t (GLM, Kimi, DeepSeek, local models…). Text extraction is cheapest; page
        images cost more tokens and need a vision-capable model.
      </div>

      <div className="mt-3 flex items-center gap-5">
        <label className="flex items-center gap-2.5">
          <span className="text-[13px] text-ink">{t("Max pages")}</span>
          <input
            type="number"
            min={1}
            max={100}
            value={pdf.pdf_max_pages}
            data-testid="pdf-max-pages"
            className="w-16 px-2 py-1.5 rounded-lg border border-line bg-paper text-[13px] text-ink outline-none focus:border-accent"
            onChange={(e) => save({ pdf_max_pages: Math.max(1, Math.min(Number(e.target.value) || 20, 100)) })}
          />
        </label>
        <label className="flex items-center gap-2.5">
          <span className="text-[13px] text-ink">{t("Max size")}</span>
          <input
            type="number"
            min={1}
            max={10}
            value={pdf.pdf_max_mb}
            data-testid="pdf-max-mb"
            className="w-16 px-2 py-1.5 rounded-lg border border-line bg-paper text-[13px] text-ink outline-none focus:border-accent"
            onChange={(e) => save({ pdf_max_mb: Math.max(1, Math.min(Number(e.target.value) || 10, 10)) })}
          />
          <span className="text-[12.5px] text-muted">MB</span>
        </label>
      </div>
      <div className={FIELD_HELP}>
        PDFs over these limits are not attached — you&rsquo;ll see a notice in the composer
        instead.
      </div>
    </div>
  );
}

function SidebarCard() {
  const t = useT();
  const [peek, setPeek] = useState<number | null>(null);

  useEffect(() => {
    getSettings()
      .then((s) => setPeek(s.sessions_peek || 5))
      .catch(() => setPeek(5));
  }, []);

  const save = async (n: number) => {
    const clamped = Math.max(1, Math.min(n || 5, 50));
    setPeek(clamped);
    await setSessionsPeek(clamped);
  };

  if (peek === null) return null;
  return (
    <div className={CARD + " p-4 mb-4"}>
      <div className={FIELD_LABEL}>{t("Sidebar")}</div>
      <label className="flex items-center gap-3 mt-2.5">
        <span className="text-[13px] text-ink">{t("Conversations shown per coworker")}</span>
        <input
          type="number"
          min={1}
          max={50}
          value={peek}
          className="w-16 px-2 py-1.5 rounded-lg border border-line bg-paper text-[13px] text-ink outline-none focus:border-accent"
          onChange={(e) => save(Number(e.target.value))}
        />
      </label>
      <div className={FIELD_HELP}>
        {t("Longer lists collapse behind “Show more”. Applies per coworker and per project.")}
      </div>
    </div>
  );
}

// -- Files (scratch location) — one card inside General (UX-021: a single option
// doesn't earn its own tab) -----------------------------------------------------
function FilesCard() {
  const t = useT();
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  const [scratchDraft, setScratchDraft] = useState("");
  const [scratchMsg, setScratchMsg] = useState<string | null>(null);
  const desktop = isTauri();

  const refresh = () =>
    getSettings()
      .then((s) => {
        setSettings(s);
        setScratchDraft((d) => d || s.scratch_base || "");
      })
      .catch(() => setSettings(null));
  useEffect(() => {
    refresh();
  }, []);

  const saveScratch = async () => {
    setScratchMsg(null);
    const res = await setScratchBase(scratchDraft.trim());
    if (res.ok) {
      setScratchMsg(t("Saved. New conversations will use this location."));
      refresh();
    } else {
      setScratchMsg(res.error || t("Could not use that location."));
    }
  };
  const browseScratch = async () => {
    const picked = await pickFolder();
    if (picked) setScratchDraft(picked);
  };

  if (!settings) return null;

  return (
    <div className={CARD + " p-4 mb-4"}>
      <div className={FIELD_LABEL}>{t("Files")}</div>
        <div className="flex items-center gap-2 mt-2.5">
          <input
            className={INPUT}
            type="text"
            placeholder="~/QunWork"
            value={scratchDraft}
            spellCheck={false}
            autoComplete="off"
            onChange={(e) => setScratchDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && saveScratch()}
          />
          {desktop && (
            <button className={BTN_BORDERED} onClick={browseScratch} title={t("Pick a folder")}>
              {t("Browse")}
            </button>
          )}
          <button className={BTN_ACCENT} onClick={saveScratch} disabled={!scratchDraft.trim()}>
            {t("Save")}
          </button>
        </div>
      <div className={FIELD_HELP}>
        Each conversation gets its own folder under this location. Existing conversations keep their current
        folder; you can grant access to more folders inside any conversation.
      </div>
      {scratchMsg && <div className="text-[12.5px] text-muted mt-2.5">{scratchMsg}</div>}
    </div>
  );
}
