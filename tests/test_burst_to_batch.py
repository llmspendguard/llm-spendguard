import os
import sys
import tempfile
from unittest.mock import patch
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-e-")
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
for name in list(sys.modules):
    if name == "spendguard" or name.startswith("spendguard."):
        del sys.modules[name]
from spendguard import bulkgate, calls  # noqa: E402
fails = []
def ck(label, condition):
    print(f"{'OK' if condition else 'FAIL'} {label}")
    if not condition:
        fails.append(label)

import re  # noqa: E402
with patch.object(bulkgate, "preview_max", return_value=1), patch.object(bulkgate, "mode", return_value="warn"), patch.object(bulkgate, "check_bulk", return_value="allow"), patch.object(bulkgate.config, "warn_once") as notice:
    bulkgate.check_realtime("eligible", "gpt-4.1-nano", 0.1, batch_eligible=True)
    bulkgate.check_realtime("eligible", "gpt-4.1-nano", 0.1, batch_eligible=True)
    ck("handoff and estimate", notice.called and "$0.200000" in notice.call_args[0][0] and "submit=False" in notice.call_args[0][0])
    notice.reset_mock()
    bulkgate.check_realtime("interactive", "gpt-4.1-nano", 0.1)
    bulkgate.check_realtime("interactive", "gpt-4.1-nano", 0.1)
    ck("interactive no conversion", not notice.called)
with calls.context(intent="offline", batch_eligible=True):
    ck("explicit eligibility context", calls.current()["batch_eligible"])
ck("context restored", not calls.current().get("batch_eligible"))
from spendguard import submit  # noqa: E402
requests, count = submit.build_message_batch_requests(["a", "b"], "claude-opus-4-8")
ck("transport IDs Anthropic safe", count == 2 and all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["custom_id"]) for item in requests))
sys.exit(1 if fails else 0)
