"""CI entry for the separate governance chaos and source-mutation contract."""
import pathlib
import runpy


if __name__ == "__main__":
    runpy.run_path(str(pathlib.Path(__file__).with_name("chaos") / "governance_fail_closed.py"),
                   run_name="__main__")
