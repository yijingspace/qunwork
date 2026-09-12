import { useEffect, useState } from "react";
import { getConnectors, getSessionConnections, listTaskTemplates, addTaskTemplate, deleteTaskTemplate, type TaskTemplate } from "../api";
import type { Attachment } from "../types";
import brandLogo from "../assets/brand-logo.webp";
import brandLogoDark from "../assets/brand-logo-dark.webp";
import { ConnectorIcon } from "../connectors/ConnectorIcon";
import { indexConnectors, visualFor, type ConnectorMap } from "../connectors/visuals";
import { useRoots } from "../useRoots";
import { AddFolderForm } from "./AddFolderForm";
import { useT } from "../i18n";

// Empty-state for a fresh Cowork session (§27, redesigned as a card grid): a greeting, exactly
// three concrete template tasks plus the "custom template" ghost cell, and the composer —
// nothing else. Each task carries its own setup: connector dots on the sub-line (brand color =
// connected and enabled for this session, grayscale = not — §23's vocabulary), and sub-line copy
// that is always the task's OUTCOME, never connection state. Sources ready → "Start →" on hover,
// click prefills the composer. Not ready → "Configure ›" always visible (for a gated card the
// setup action IS the card's meaning), opening the §23 Session settings drawer. Custom templates
// live in their own labeled group; each card is a real <button> with a SIBLING delete button —
// never an interactive element nested inside another (the old × span inside <button> was invalid
// HTML). Inline forms (add-folder / new template) open in one full-width panel below the grids.

const FOLDER_PROMPT = "Analyze the files in this folder and summarize what matters.";
const HUBSPOT_PROMPT =
  "Create a report on my recent HubSpot leads: sources, stages, and who needs follow-up.";
const GH_SLACK_PROMPT =
  "Set up a weekly progress report: summarize activity in my GitHub repos and post it to Slack every Friday morning.";

export function SessionIntro({
  sessionId,
  onOpenSessionSettings,
  onPrefill,
}: {
  sessionId: string;
  // Opens the §23 Session settings drawer (sources section) — the gated cards' Configure target.
  onOpenSessionSettings: () => void;
  onPrefill: (text: string, attachments?: Attachment[]) => void;
}) {
  const t = useT();
  const { roots, busy, error, addRoot } = useRoots(sessionId);
  const [live, setLive] = useState<Set<string>>(new Set());
  const [byName, setByName] = useState<ConnectorMap>({});
  const [addingFolder, setAddingFolder] = useState(false);
  const [templates, setTemplates] = useState<TaskTemplate[]>([]);
  const [addingTemplate, setAddingTemplate] = useState(false);
  const [tmplTitle, setTmplTitle] = useState("");
  const [tmplPrompt, setTmplPrompt] = useState("");
  const [tmplError, setTmplError] = useState<string | null>(null);
  const [tmplBusy, setTmplBusy] = useState(false);

  useEffect(() => {
    // Live = what this session can touch right now (connected AND not muted here) — the same
    // truth the §23 glance renders, so the dots here can never disagree with the row above.
    getSessionConnections(sessionId)
      .then((c) => setLive(new Set(c.connected.filter((x) => x.enabled).map((x) => x.connector))))
      .catch(() => {});
    getConnectors()
      .then((list) => setByName(indexConnectors(list)))
      .catch(() => {});
    listTaskTemplates()
      .then((r) => setTemplates(r.templates ?? []))
      .catch(() => {});
  }, [sessionId]);

  const shared = roots.filter((r) => !r.primary);
  const hubspotReady = live.has("hubspot");
  const ghSlackReady = live.has("github") && live.has("slack");

  const dot = (name: string, on: boolean) => (
    <span className={"task-dot" + (on ? "" : " off")} key={name}>
      <ConnectorIcon connector={visualFor(name, "connector", byName)} size={12} />
    </span>
  );

  const pickFolder = () => {
    // A shared folder already exists → straight to the prompt; otherwise share one first.
    if (shared.length > 0) onPrefill(t(FOLDER_PROMPT));
    else setAddingFolder((v) => !v);
  };

  const saveTemplate = async () => {
    setTmplBusy(true);
    setTmplError(null);
    const res = await addTaskTemplate(tmplTitle, tmplPrompt);
    setTmplBusy(false);
    if (!res.ok) {
      setTmplError(res.error || t("Failed to save template"));
      return;
    }
    setTemplates((prev) => [...prev, res.template!]);
    setAddingTemplate(false);
    setTmplTitle("");
    setTmplPrompt("");
  };

  const removeTemplate = async (id: number) => {
    const ok = await deleteTaskTemplate(id);
    if (ok.ok) setTemplates((prev) => prev.filter((x) => x.id !== id));
  };

  return (
    <div className="intro">
      {/* Complete brand lockup (Q+swarm mark + 群沃客 wordmark), centered above the greeting.
          Two transparent-ink variants — data-theme picks the one with contrast on the paper —
          so the lockup never renders as a white slab in dark mode. */}
      <div className="intro-brand" data-testid="intro-brand">
        <img src={brandLogo} alt="群沃客" className="intro-brand-img intro-brand-light" />
        <img src={brandLogoDark} alt="" aria-hidden="true" className="intro-brand-img intro-brand-dark" />
      </div>
      <h1 className="greeting">
        <span className="mark">✦</span> {t("What task can I help you solve?")}
      </h1>
      <p className="intro-lede">
        {t("Tell me what you need done — I'll plan it, do the work, and save the result. Or pick a task below.")}
      </p>

      <div className="intro-section-label">{t("Quick start")}</div>
      <div className="task-grid">
        <button className="task-card" data-testid="intro-task-folder" onClick={pickFolder}>
          <span className="task-card-body">
            <span className="task-card-title">{t("Analyze the files in a directory")}</span>
            <span className="task-card-sub">{t("I'll read them and summarize what matters")}</span>
          </span>
          <span className="task-card-act">{t("Pick a folder →")}</span>
        </button>

        <button
          className={"task-card" + (hubspotReady ? "" : " gated")}
          data-testid="intro-task-hubspot"
          onClick={() => (hubspotReady ? onPrefill(t(HUBSPOT_PROMPT)) : onOpenSessionSettings())}
        >
          <span className="task-card-body">
            <span className="task-card-title">{t("Create a report from my HubSpot leads")}</span>
            <span className="task-card-sub">
              {dot("hubspot", hubspotReady)}
              {t("Sources, stages, and who needs follow-up")}
            </span>
          </span>
          <span className="task-card-act">{hubspotReady ? t("Start →") : t("Configure ›")}</span>
        </button>

        <button
          className={"task-card" + (ghSlackReady ? "" : " gated")}
          data-testid="intro-task-github-slack"
          onClick={() => (ghSlackReady ? onPrefill(t(GH_SLACK_PROMPT)) : onOpenSessionSettings())}
        >
          <span className="task-card-body">
            <span className="task-card-title">{t("Automate a weekly GitHub progress report to Slack")}</span>
            <span className="task-card-sub">
              {dot("github", live.has("github"))}
              {dot("slack", live.has("slack"))}
              {t("Repo activity, summarized and posted every Friday")}
            </span>
          </span>
          <span className="task-card-act">{ghSlackReady ? t("Start →") : t("Configure ›")}</span>
        </button>

        <button className="task-card task-card-add" data-testid="intro-task-add" onClick={() => setAddingTemplate((v) => !v)}>
          <span className="task-card-body">
            <span className="task-card-title">{addingTemplate ? t("Cancel") : "＋ " + t("Custom template")}</span>
            <span className="task-card-sub">{t("Save a task you run often — click to fill the composer")}</span>
          </span>
        </button>
      </div>

      {addingFolder && (
        <div className="intro-form-panel">
          <AddFolderForm
            startOpen
            busy={busy}
            onAdd={async (path, writable) => {
              const ok = await addRoot(path, writable);
              if (ok !== false) onPrefill(t(FOLDER_PROMPT));
              return ok;
            }}
            onDismiss={() => setAddingFolder(false)}
          />
          {error && <div className="roots-err">{error}</div>}
        </div>
      )}

      {addingTemplate && (
        <div className="intro-form-panel intro-template-form">
          <input
            className="intro-tmpl-input"
            placeholder={t("Template title")}
            value={tmplTitle}
            onChange={(e) => setTmplTitle(e.target.value)}
            autoFocus
          />
          <textarea
            className="intro-tmpl-input intro-tmpl-prompt"
            placeholder={t("The task prompt")}
            value={tmplPrompt}
            onChange={(e) => setTmplPrompt(e.target.value)}
            rows={3}
          />
          {tmplError && <div className="roots-err">{tmplError}</div>}
          <div className="flex gap-2">
            <button className="btn" disabled={tmplBusy || !tmplTitle.trim() || !tmplPrompt.trim()} onClick={() => void saveTemplate()}>
              {tmplBusy ? t("Saving…") : t("Save template")}
            </button>
            <button className="btn quiet" onClick={() => setAddingTemplate(false)}>
              {t("Cancel")}
            </button>
          </div>
        </div>
      )}

      {templates.length > 0 && (
        <>
          <div className="intro-section-label">{t("My templates")}</div>
          <div className="task-grid">
            {templates.map((tmpl) => (
              <div className="task-card" data-testid={`intro-task-custom-${tmpl.id}`} key={tmpl.id}>
                <button className="task-card-main" onClick={() => onPrefill(tmpl.prompt)}>
                  <span className="task-card-body">
                    <span className="task-card-title">{tmpl.title}</span>
                    <span className="task-card-sub">{tmpl.prompt}</span>
                  </span>
                  <span className="task-card-act">{t("Start →")}</span>
                </button>
                <button
                  className="task-card-del"
                  aria-label={t("Delete template")}
                  title={t("Delete template")}
                  onClick={() => void removeTemplate(tmpl.id)}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
