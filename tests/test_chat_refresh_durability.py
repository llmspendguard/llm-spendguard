"""Script guard for durable refresh checkpoints and incomplete-gap reporting."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-chat-refresh-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from _script_checks import record_script_check  # noqa: E402
from spendguard.generate.spend_capture import chat  # noqa: E402

fails = []
old_watermark = "2026-01-01T00:00:00Z"
old_state = {"orgs": {"org": {"since": old_watermark}}, "convs": {}}
saved = []
chat.config.load_state = lambda name, default=None: old_state
chat.config.save_state = lambda name, state: saved.append({"since": state["orgs"]["org"]["since"],
                                                           "incomplete": state["orgs"]["org"].get("incomplete")})
chat._resolve_org = lambda cookies: "org"
chat._digest_conv = lambda conv, detail, projmap, prev=None: {"uuid": conv["uuid"], "updated_at": conv["updated_at"], "v": chat._DIGEST_V}
page = [{"uuid": "good", "updated_at": "2026-01-04T00:00:00Z"},
        {"updated_at": "2026-01-03T00:00:00Z"},
        {"uuid": "bad", "updated_at": "2026-01-02T00:00:00Z"}]
def fake_api(path, cookies):
    if path.endswith("/projects"):
        return []
    if "chat_conversations?" in path:
        return page
    if "/bad" in path:
        raise RuntimeError("detail unavailable")
    return {"chat_messages": []}
chat._api = fake_api
state, info = chat.refresh_chat_conversations(ck={"cookie": "value"})
record_script_check(fails, "refresh: rejected rows and detail failures are named",
                    info["rejections"]["no_uuid"] == 1 and info["rejections"]["detail_fetch_error"] == 1)
record_script_check(fails, "refresh: incomplete traversal retains the old watermark",
                    info["incomplete"] and state["orgs"]["org"]["since"] == old_watermark)
record_script_check(fails, "refresh: each processed page gets a durable incomplete checkpoint",
                    saved and saved[0] == {"since": old_watermark, "incomplete": True})
notice = chat._chat_refresh_gap_notice(info)
record_script_check(fails, "refresh: gap notice names offending sites", "no_uuid=1" in notice and "detail_fetch_error=1" in notice)
print(("\n[FAIL] " if fails else "\n[OK] ") + f"chat_refresh_durability: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
