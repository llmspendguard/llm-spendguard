"""Offline guards for reviewed chat pagination gaps, durable resume, and safe sync."""
import contextlib
import copy
import io
import os
import pathlib
import sys
import tempfile
from unittest.mock import patch

if not os.environ.get("SPENDGUARD_CHAT_REFRESH_GUARD_CHILD"):
    with tempfile.TemporaryDirectory(prefix="spendguard-chat-refresh-") as refresh_guard_home:
        import subprocess
        refresh_guard_env = dict(os.environ, HOME=refresh_guard_home, SPENDGUARD_HOME=refresh_guard_home,
                                 SPENDGUARD_TEST_ISOLATED="1", SPENDGUARD_CHAT_REFRESH_GUARD_CHILD="1")
        sys.exit(subprocess.call([sys.executable, __file__], env=refresh_guard_env))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from spendguard import chat, config, saas

REFRESH_GUARD_ORG = "refresh-guard-org"
REFRESH_GUARD_UPDATED = "refresh-guard-updated"
REFRESH_GUARD_COOKIE = {"sessionKey": "offline-refresh-guard"}
REFRESH_GUARD_NO_UUID = {"updated_at": REFRESH_GUARD_UPDATED}
REFRESH_GUARD_GOOD = {"uuid": "refresh-guard-good", "updated_at": REFRESH_GUARD_UPDATED}
REFRESH_GUARD_BAD = {"uuid": "refresh-guard-bad", "updated_at": REFRESH_GUARD_UPDATED}


def verify_chat_refresh_durable_named_gaps():
    snapshots = []
    page_saw_checkpoint = []
    initial = {"orgs": {}, "convs": {}}

    def refresh_guard_api(endpoint, cookies):
        if endpoint.endswith("/projects"):
            return []
        if "offset=0" in endpoint:
            return [REFRESH_GUARD_NO_UUID, REFRESH_GUARD_GOOD, REFRESH_GUARD_BAD]
        if "offset=" in endpoint:
            page_saw_checkpoint.append(bool(snapshots))
            raise RuntimeError("offline named page failure")
        if REFRESH_GUARD_BAD["uuid"] in endpoint:
            raise RuntimeError("offline named detail failure")
        return {}

    def refresh_guard_digest(conv, detail, projects, prev=None):
        return dict(conv, v=chat._DIGEST_V)

    with patch.object(chat, "_cookies", return_value=REFRESH_GUARD_COOKIE), \
         patch.object(chat, "_resolve_org", return_value=REFRESH_GUARD_ORG), \
         patch.object(config, "load_state", return_value=initial), \
         patch.object(config, "save_state", side_effect=lambda name, st: snapshots.append(copy.deepcopy(st))), \
         patch.object(chat, "CHAT_FETCH_PAGE_SIZE", len([REFRESH_GUARD_NO_UUID, REFRESH_GUARD_GOOD, REFRESH_GUARD_BAD])), \
         patch.object(chat, "_digest_conv", side_effect=refresh_guard_digest), \
         patch.object(chat, "_api", side_effect=refresh_guard_api):
        state, info = chat.refresh_chat_conversations()
    assert info["rejections"] == dict(no_uuid=1, detail_fetch_error=1, page_fetch_error=1, capped=0)
    assert info["incomplete"] and info["more"] and not info["capped"]
    assert info["skipped"] == info["rejections"]["detail_fetch_error"]
    assert page_saw_checkpoint == [True] and len(snapshots) > len(page_saw_checkpoint)
    assert REFRESH_GUARD_GOOD["uuid"] in snapshots[0]["convs"]
    assert snapshots[0]["orgs"][REFRESH_GUARD_ORG]["since"] == ""
    assert state["orgs"][REFRESH_GUARD_ORG]["since"] == ""
    assert "page_fetch_error=1" in chat._chat_refresh_gap_notice(info)


def verify_chat_refresh_cap_and_safe_sync():
    cap_page = [REFRESH_GUARD_GOOD, REFRESH_GUARD_BAD]
    with patch.object(config, "load_state", return_value={"orgs": {}, "convs": {}}), \
         patch.object(chat, "_resolve_org", return_value=REFRESH_GUARD_ORG), \
         patch.object(config, "save_state"), \
         patch.object(chat, "_api", side_effect=[[], cap_page]):
        _, info = chat.refresh_chat_conversations(ck=REFRESH_GUARD_COOKIE, max_new=0)
    assert info["capped"] and info["max_new"] == 0 and info["more"]
    assert info["rejections"]["capped"] == len(cap_page)
    assert "max_new=0" in chat._chat_refresh_gap_notice(info)
    # A short page ending exactly at max_new still reached the user-visible cap;
    # it cannot quietly become an authoritative replacement snapshot.
    with patch.object(config, "load_state", return_value={"orgs": {}, "convs": {}}), \
         patch.object(chat, "_resolve_org", return_value=REFRESH_GUARD_ORG), \
         patch.object(config, "save_state"), \
         patch.object(chat, "_digest_conv", return_value=dict(REFRESH_GUARD_GOOD, v=chat._DIGEST_V)), \
         patch.object(chat, "_api", side_effect=[[], [REFRESH_GUARD_GOOD], {}]):
        _, exact_info = chat.refresh_chat_conversations(ck=REFRESH_GUARD_COOKIE, max_new=len([REFRESH_GUARD_GOOD]))
    assert exact_info["fetched"] == exact_info["max_new"]
    assert exact_info["capped"] and exact_info["incomplete"] and exact_info["more"]
    assert exact_info["rejections"]["capped"] == 0  # no known omitted records; unseen count remains unknown
    assert "max_new=" in chat._chat_refresh_gap_notice(exact_info)
    with patch.object(chat, "_chat_enabled", return_value=True), \
         patch.object(saas, "saas_connection", return_value={"enabled": True, "visibility": "team"}), \
         patch.object(saas, "contributor_ok", return_value=(True, "")), \
         patch.object(chat, "refresh_chat_conversations", return_value=({}, info)), \
         patch.object(saas, "_request") as refresh_guard_request, \
         contextlib.redirect_stdout(io.StringIO()) as refresh_guard_print:
        result = chat.sync()
    assert "incomplete" in result["skipped"] and not refresh_guard_request.called
    assert "capped=" in refresh_guard_print.getvalue()


if __name__ == "__main__":
    verify_chat_refresh_durable_named_gaps()
    verify_chat_refresh_cap_and_safe_sync()
    print("OK chat refresh durable checkpoints, rejection accounting, cap notice, and safe sync")
