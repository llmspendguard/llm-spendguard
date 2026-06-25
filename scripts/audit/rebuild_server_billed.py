"""Re-push the CLEAN billed ledger to the server, per ORG connection (run AFTER deleting server billed=true).

Each org is pushed ONCE: both ensight repo connections roll up the SAME ensight rows, so syncing both would double
ensight — sync only one. `saas.sync()` pushes the billed LLM rollup (gate + provider-truth reconcile); `resources.sync()`
pushes the GPU billed rows. Prints what each pushed so Σ can be checked against provider_truth.py.
"""
import os

from spendguard import saas, resources

CONNS = [
    (os.path.expanduser("~"), "Healiom (global)"),
    ("/Users/ashdamle/Documents/claude/llm-spendguard", "ensight (llm-spendguard — ONE ensight conn)"),
]


def main():
    for cwd, label in CONNS:
        os.chdir(cwd)
        org = saas.conn().get("org")
        try:
            llm = saas.sync()
        except Exception as e:
            llm = {"error": str(e)[:160]}
        try:
            gpu = resources.sync()
        except Exception as e:
            gpu = {"error": str(e)[:160]}
        print(f"[{label}] conn org={org}")
        print(f"   LLM (saas.sync):     {str(llm)[:220]}")
        print(f"   GPU (resources.sync):{str(gpu)[:220]}")


if __name__ == "__main__":
    main()
