"""Inbox footer-badge vs page consistency (bug: badge "N pending" over empty page).

list_sessions exposes TWO counts per session:
  attention        = all pending (inline + inbox)   -> per-session / persona dots
  inbox_attention  = pending with visibility=inbox  -> exactly what the cross-session
                     Inbox PAGE lists (GET /v1/inbox filters to VIS_INBOX with no
                     session_id)                     -> footer/nav Inbox badge
Before the fix the footer badge summed `attention`, so attended `inline` prompts parked
on a conversation you navigated away from left a permanent count over a "Nothing pending"
page (the page never listed them, and its orphan-closer only touches listed items).
"""
from coworker.inbox import VIS_INBOX, VIS_INLINE
from coworker.server.manager import SessionManager
from coworker.sessions import SessionRecord


def _mgr(tmp_path):
    m = SessionManager(data_dir=tmp_path / "data", workspace=str(tmp_path))
    m.session_store.save(
        SessionRecord(
            session_id="s1",
            workspace=str(tmp_path),
            model="gpt-5.5",
            mode="interactive",
            agent="cowork",
        )
    )
    return m


def _row(m):
    return next(s for s in m.list_sessions() if s["session_id"] == "s1")


def test_inbox_attention_counts_only_inbox_visibility(tmp_path):
    m = _mgr(tmp_path)
    m.inbox.add_approval("s1", "In-line run_shell?", visibility=VIS_INLINE)
    m.inbox.add_approval("s1", "Unattended deploy?", visibility=VIS_INBOX)
    row = _row(m)
    assert row["attention"] == 2          # both pending (drives the row/persona dot)
    assert row["inbox_attention"] == 1    # only the unattended one is on the Inbox page
    m.inbox._items.clear()


def test_resolved_items_leave_both_counts(tmp_path):
    m = _mgr(tmp_path)
    it = m.inbox.add_approval("s1", "Pending?", visibility=VIS_INBOX)
    assert _row(m)["inbox_attention"] == 1
    m.inbox.resolve(it.id, "allow")
    row = _row(m)
    assert row["attention"] == 0 and row["inbox_attention"] == 0


def test_attended_only_session_leaves_footer_badge_zero(tmp_path):
    # The exact regression: a session parked ONLY on an attended inline prompt.
    m = _mgr(tmp_path)
    m.inbox.add_question("s1", "Which region?", visibility=VIS_INLINE)
    row = _row(m)
    assert row["attention"] == 1        # row dot still flags it (answer in-context)
    assert row["inbox_attention"] == 0  # footer Inbox badge (what the page shows) is 0
