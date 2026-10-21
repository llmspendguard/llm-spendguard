"""A context refresh reads the current breadth layer, including failed/removed layers."""
import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import config, pricing  # noqa: E402

CONTEXT_RELOAD_MODEL = Path(__file__).stem
CONTEXT_RELOAD_INITIAL_INPUT = len(CONTEXT_RELOAD_MODEL)
CONTEXT_RELOAD_UPDATED_INPUT = CONTEXT_RELOAD_INITIAL_INPUT * CONTEXT_RELOAD_INITIAL_INPUT
CONTEXT_RELOAD_INITIAL = {
    "context": {CONTEXT_RELOAD_MODEL: {"max_input_tokens": CONTEXT_RELOAD_INITIAL_INPUT}}
}
CONTEXT_RELOAD_UPDATED = {
    "context": {CONTEXT_RELOAD_MODEL: {"max_input_tokens": CONTEXT_RELOAD_UPDATED_INPUT}}
}

with tempfile.TemporaryDirectory(prefix=CONTEXT_RELOAD_MODEL) as context_reload_home:
    with patch.object(config, "HOME", Path(context_reload_home)):
        context_reload_path = config.HOME / pricing.LITELLM_BREADTH_FILENAME
        assert pricing._load_context() == {}, "missing breadth must remain unknown"
        context_reload_path.write_text(json.dumps(CONTEXT_RELOAD_INITIAL))
        assert pricing._load_context() == CONTEXT_RELOAD_INITIAL["context"], "new cache was ignored"
        context_reload_path.write_text(json.dumps(CONTEXT_RELOAD_UPDATED))
        assert pricing._load_context() == CONTEXT_RELOAD_UPDATED["context"], "replacement cache was ignored"
        context_reload_path.write_text(repr(CONTEXT_RELOAD_UPDATED))
        context_reload_errors = io.StringIO()
        with contextlib.redirect_stderr(context_reload_errors):
            assert pricing._load_context() == {}, "corrupt cache must not retain stale bounds"
        assert "GAP unreadable breadth cache" in context_reload_errors.getvalue()
        context_reload_path.unlink()
        assert pricing._load_context() == {}, "removed cache must not retain stale bounds"

print("OK context breadth reload: create/replace/corrupt/remove")
