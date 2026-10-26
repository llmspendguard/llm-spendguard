import os
from pathlib import Path
import stat
import sys
import tempfile

import pytest

from spendguard import setup


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS filesystem flags only")


def _resolved_temp_purelib(monkeypatch, tmp_path):
    monkeypatch.setattr(setup, "_interpreter_purelib", lambda target, timeout: str(tmp_path))
    return tmp_path / setup._PTH_NAME


def test_gate_pth_hidden_diagnosis_detects_uf_hidden(monkeypatch, tmp_path):
    gate_pth_path = _resolved_temp_purelib(monkeypatch, tmp_path)
    gate_pth_path.write_text(setup._PTH_SENTINEL)
    os.chflags(gate_pth_path, stat.UF_HIDDEN)

    diagnosis = setup.gate_pth_hidden_diagnosis(sys.executable)

    assert diagnosis is not None
    assert diagnosis["hidden"] is True
    assert diagnosis["path"] == gate_pth_path


def test_gate_pth_hidden_diagnosis_ignores_clean_hook(monkeypatch, tmp_path):
    gate_pth_path = _resolved_temp_purelib(monkeypatch, tmp_path)
    gate_pth_path.write_text(setup._PTH_SENTINEL)

    assert setup.gate_pth_hidden_diagnosis(sys.executable) is None


def test_gate_pth_hidden_diagnosis_reuses_hook_filename_constant(monkeypatch, tmp_path):
    derived_hook_name = f"{tmp_path.name}{Path(setup._PTH_NAME).suffix}"
    monkeypatch.setattr(setup, "_PTH_NAME", derived_hook_name)
    gate_pth_path = _resolved_temp_purelib(monkeypatch, tmp_path)
    gate_pth_path.write_text(setup._PTH_SENTINEL)
    os.chflags(gate_pth_path, stat.UF_HIDDEN)

    diagnosis = setup.gate_pth_hidden_diagnosis(sys.executable)

    assert diagnosis is not None
    assert Path(diagnosis["path"]).name == setup._PTH_NAME


if __name__ == "__main__" and sys.platform == "darwin":
    for test_case in (
        test_gate_pth_hidden_diagnosis_detects_uf_hidden,
        test_gate_pth_hidden_diagnosis_ignores_clean_hook,
        test_gate_pth_hidden_diagnosis_reuses_hook_filename_constant,
    ):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with pytest.MonkeyPatch.context() as isolated_monkeypatch:
                test_case(isolated_monkeypatch, Path(temporary_directory))
