/**
 * Task-completion alerts: preferences, the two channels, and the "is this even a
 * completion?" decision. The interesting cases are all failure paths — a notification
 * must never throw, and a suppressed/blocked channel must report that it did nothing.
 */

import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_NOTIFY_PREFS,
  completionAlertKind,
  getNotifyPrefs,
  notificationPermission,
  notifyTaskDone,
  playCompletionChime,
  setNotifyPrefs,
  showDesktopNotification,
  summarize,
} from "./notify";

const KEY = "qunwork-notify";

function stubNotification(permission: NotificationPermission) {
  const made: Array<{ title: string; body?: string }> = [];
  class Fake {
    static permission = permission;
    static requestPermission = async () => permission;
    onclick: (() => void) | null = null;
    constructor(title: string, opts?: { body?: string }) {
      made.push({ title, body: opts?.body });
    }
    close() {}
  }
  vi.stubGlobal("Notification", Fake);
  return made;
}

describe("notify preferences", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("defaults to sound on, desktop off", () => {
    expect(getNotifyPrefs()).toEqual(DEFAULT_NOTIFY_PREFS);
    expect(DEFAULT_NOTIFY_PREFS).toEqual({ sound: true, desktop: false });
  });

  it("persists a partial update without dropping the other channel", () => {
    setNotifyPrefs({ desktop: true });
    expect(getNotifyPrefs()).toEqual({ sound: true, desktop: true });
    setNotifyPrefs({ sound: false });
    expect(getNotifyPrefs()).toEqual({ sound: false, desktop: true });
  });

  it("survives corrupted stored JSON", () => {
    localStorage.setItem(KEY, "{not json");
    expect(getNotifyPrefs()).toEqual(DEFAULT_NOTIFY_PREFS);
  });
});

describe("sound channel", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("does nothing when the pref is off", () => {
    setNotifyPrefs({ sound: false });
    vi.stubGlobal(
      "AudioContext",
      class {
        constructor() {
          throw new Error("must not be constructed when sound is off");
        }
      },
    );
    expect(playCompletionChime()).toBe(false);
  });

  it("stays silent (not thrown) when the platform has no AudioContext", () => {
    setNotifyPrefs({ sound: true });
    vi.stubGlobal("AudioContext", undefined);
    vi.stubGlobal("webkitAudioContext", undefined);
    expect(playCompletionChime()).toBe(false);
  });
});

describe("desktop channel", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("is skipped while the pref is off, even with permission granted", () => {
    const made = stubNotification("granted");
    setNotifyPrefs({ desktop: false });
    expect(showDesktopNotification({ title: "x", body: "y" })).toBe(false);
    expect(made).toHaveLength(0);
  });

  it("is skipped when permission was never granted", () => {
    const made = stubNotification("default");
    setNotifyPrefs({ desktop: true });
    expect(showDesktopNotification({ title: "x", body: "y" })).toBe(false);
    expect(made).toHaveLength(0);
  });

  it("shows the popup with the body when enabled and granted", () => {
    const made = stubNotification("granted");
    setNotifyPrefs({ desktop: true });
    expect(showDesktopNotification({ title: "任务已完成", body: "摘要", tag: "t1" })).toBe(true);
    expect(made).toEqual([{ title: "任务已完成", body: "摘要" }]);
  });

  it("reports 'unsupported' instead of throwing on a bare environment", () => {
    vi.stubGlobal("Notification", undefined);
    expect(notificationPermission()).toBe("unsupported");
    setNotifyPrefs({ desktop: true });
    expect(showDesktopNotification({ title: "x", body: "y" })).toBe(false);
  });
});

describe("notifyTaskDone", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("reports exactly which channels went out", () => {
    const made = stubNotification("granted");
    vi.stubGlobal("AudioContext", undefined);
    setNotifyPrefs({ sound: true, desktop: true });
    // No AudioContext in jsdom → the sound channel reports false, the desktop one fires.
    expect(notifyTaskDone({ title: "t", body: "b" })).toEqual({ sound: false, desktop: true });
    expect(made).toHaveLength(1);
  });

  it("fires nothing when both channels are off", () => {
    stubNotification("granted");
    setNotifyPrefs({ sound: false, desktop: false });
    expect(notifyTaskDone({ title: "t", body: "b" })).toEqual({ sound: false, desktop: false });
  });
});

describe("completionAlertKind", () => {
  it("treats an owner interrupt as no completion at all", () => {
    expect(completionAlertKind({ interrupted: true })).toBe("none");
    expect(completionAlertKind({ interrupted: true, errored: true })).toBe("none");
  });

  it("distinguishes a clean finish from an errored one", () => {
    expect(completionAlertKind({})).toBe("ok");
    expect(completionAlertKind({ errored: true })).toBe("error");
  });
});

describe("summarize", () => {
  it("flattens whitespace and truncates with an ellipsis", () => {
    expect(summarize("  hello\n\nworld  ")).toBe("hello world");
    expect(summarize("x".repeat(200), 10)).toBe("xxxxxxxxx…");
  });
});
