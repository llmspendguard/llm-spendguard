import tempfile
import sys
from pathlib import Path
from unittest import mock

from spendguard.platform import setup
from spendguard.platform.manifest import MANIFEST_WHEEL_KIND


def guard_noneditable_install_hook_skips_source_refresh():
    with tempfile.TemporaryDirectory() as temporary_directory:
        venv = Path(temporary_directory) / "consumer"
        target = venv / "bin" / "python"
        target.parent.mkdir(parents=True)
        target.symlink_to(sys.executable)
        purelib = venv / "purelib"
        purelib.mkdir()
        with (mock.patch.object(setup, "_interpreter_purelib", return_value=str(purelib)),
              mock.patch.object(setup, "_startup_gate_active", return_value=True),
              mock.patch.object(setup, "_sdk_gate_states",
                                return_value={name: "absent" for name in setup._GATED_SDKS}),
              mock.patch("spendguard.platform.manifest.inspect_current_interpreter_install_kind",
                         return_value={"install_kind": MANIFEST_WHEEL_KIND}),
              mock.patch.object(setup, "refresh_consumer_editable_package") as refresh):
            assert setup.install_hook(venv=str(venv)) == 0
        assert (purelib / "sitecustomize.py").exists()
        refresh.assert_not_called()


if __name__ == "__main__":
    guard_noneditable_install_hook_skips_source_refresh()
    print("GREEN: non-editable install writes the hook without reading source build metadata")
