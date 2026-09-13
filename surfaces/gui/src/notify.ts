/**
 * Task-completion notifications (owner ask 2026-09-13).
 *
 * A swarm run or a normal turn usually finishes while the window is behind something
 * else, and until now the only signal was the UI quietly going idle. Two channels, each
 * independently switchable, stored per device (localStorage — same convention as the
 * theme preference):
 *
 *   sound   — a short rising two-note chime synthesized with WebAudio. No asset to ship,
 *             no network, and it degrades to silence when the platform blocks audio.
 *   desktop — the Web Notification API, the same channel the 7×24 stall alerts already
 *             use (silently skipped when unsupported or not granted).
 *
 * Everything here is failure-safe by design: a notification must never break a turn, so
 * every entry point swallows platform errors instead of propagating them.
 */

import { useEffect, useState } from "react";

export interface NotifyPrefs {
  /** Play a chime when a task finishes. */
  sound: boolean;
  /** Show a desktop notification when a task finishes. */
  desktop: boolean;
}

/** Sound on, desktop popup off: a chime is unmissable but never intrusive, while an OS
 *  notification is a bigger interruption — the owner opts into it deliberately. */
export const DEFAULT_NOTIFY_PREFS: NotifyPrefs = { sound: true, desktop: false };

const KEY = "qunwork-notify";
const PREF_EVENT = "qunwork:notify-prefs";

export function getNotifyPrefs(): NotifyPrefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULT_NOTIFY_PREFS };
    const parsed = JSON.parse(raw) as Partial<NotifyPrefs>;
    return {
      sound: typeof parsed.sound === "boolean" ? parsed.sound : DEFAULT_NOTIFY_PREFS.sound,
      desktop: typeof parsed.desktop === "boolean" ? parsed.desktop : DEFAULT_NOTIFY_PREFS.desktop,
    };
  } catch {
    return { ...DEFAULT_NOTIFY_PREFS };
  }
}

export function setNotifyPrefs(patch: Partial<NotifyPrefs>): NotifyPrefs {
  const next = { ...getNotifyPrefs(), ...patch };
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    /* private mode — in-memory value still applies for this session */
  }
  try {
    window.dispatchEvent(new CustomEvent(PREF_EVENT));
  } catch {
    /* non-DOM environment */
  }
  return next;
}

/** Settings-page hook — stays in sync if the prefs change elsewhere. */
export function useNotifyPrefs(): [NotifyPrefs, (patch: Partial<NotifyPrefs>) => void] {
  const [prefs, setPrefs] = useState<NotifyPrefs>(getNotifyPrefs);
  useEffect(() => {
    const sync = () => setPrefs(getNotifyPrefs());
    window.addEventListener(PREF_EVENT, sync);
    return () => window.removeEventListener(PREF_EVENT, sync);
  }, []);
  return [prefs, setNotifyPrefs];
}

// -- sound --------------------------------------------------------------------------------

let audioCtx: AudioContext | null = null;

function audioContext(): AudioContext | null {
  try {
    const Ctor =
      window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return null;
    audioCtx = audioCtx ?? new Ctor();
    return audioCtx;
  } catch {
    return null;
  }
}

/**
 * The completion chime: E6 → A6, ~300 ms, soft. Two notes read as "finished" rather
 * than "error", and the short decay keeps it from being annoying on the tenth run.
 */
export function playCompletionChime(): boolean {
  if (!getNotifyPrefs().sound) return false;
  const ac = audioContext();
  if (!ac) return false;
  try {
    if (ac.state === "suspended") void ac.resume();
    const t0 = ac.currentTime;
    const notes: Array<[number, number, number]> = [
      [1318.5, 0, 0.13], // E6
      [1760.0, 0.15, 0.11], // A6
    ];
    for (const [freq, delay, peak] of notes) {
      const at = t0 + delay;
      const osc = ac.createOscillator();
      const gain = ac.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      // Exponential ramps need a non-zero floor.
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(peak, at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.26);
      osc.connect(gain);
      gain.connect(ac.destination);
      osc.start(at);
      osc.stop(at + 0.3);
    }
    return true;
  } catch {
    return false;
  }
}

// -- desktop notification -----------------------------------------------------------------

export type PermissionState = NotificationPermission | "unsupported";

export function notificationPermission(): PermissionState {
  try {
    if (typeof Notification === "undefined") return "unsupported";
    return Notification.permission;
  } catch {
    return "unsupported";
  }
}

/** Ask for permission — must be called from a user gesture (the Settings toggle). */
export async function requestNotificationPermission(): Promise<PermissionState> {
  try {
    if (typeof Notification === "undefined") return "unsupported";
    if (Notification.permission !== "default") return Notification.permission;
    return await Notification.requestPermission();
  } catch {
    return "unsupported";
  }
}

export function showDesktopNotification(opts: {
  title: string;
  body: string;
  tag?: string;
  onClick?: () => void;
}): boolean {
  if (!getNotifyPrefs().desktop) return false;
  if (notificationPermission() !== "granted") return false;
  try {
    const n = new Notification(opts.title, {
      body: opts.body,
      tag: opts.tag ?? "qunwork-task-done",
      // Keep the popup around long enough to be noticed, then let the OS decide.
      requireInteraction: false,
    });
    n.onclick = () => {
      try {
        window.focus();
      } catch {
        /* some webviews refuse focus() */
      }
      opts.onClick?.();
      n.close();
    };
    return true;
  } catch {
    return false;
  }
}

// -- one entry point for both task kinds --------------------------------------------------

export interface TaskDoneNotice {
  title: string;
  body: string;
  tag?: string;
  onClick?: () => void;
}

/** Fire both configured channels. Returns which ones actually went out (for tests). */
export function notifyTaskDone(notice: TaskDoneNotice): { sound: boolean; desktop: boolean } {
  return {
    sound: playCompletionChime(),
    desktop: showDesktopNotification(notice),
  };
}

/**
 * Which alert (if any) a finished turn deserves. The owner stopping a turn is not a
 * completion, and an errored turn must not claim success — both cases the UI cannot
 * express through the notification itself, so they are decided here.
 */
export function completionAlertKind(o: { interrupted?: boolean; errored?: boolean }): "none" | "ok" | "error" {
  if (o.interrupted) return "none";
  return o.errored ? "error" : "ok";
}

/** Trim a model answer into a one-line notification body. */
export function summarize(text: string, max = 120): string {
  const flat = (text || "").replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  return flat.slice(0, max - 1) + "…";
}
