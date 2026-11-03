"""The ONE sanctioned way to put a file into an LLM prompt: whole, stamped, self-verified.

This is the INPUT twin of the OUTPUT-completeness guarantee in adapters.py (see the `# ── a COMPLETE answer, or
an explicit UNKNOWN — never a truncated body ──` section and `bulkgate.is_truncated`). spendguard already stops a
REPLY from being silently truncated by max_tokens; this stops a PROMPT from silently carrying a truncated FILE —
the mirror failure, which had no guard.

WHY THIS EXISTS. Sending a model a slice of a file instead of the whole thing is a shortcut that does not
announce itself. Measured in a sister project: a code-arbitration run fed the model ±28-line excerpts and told it
to default to a verdict when unsure; it repeatedly answered "the excerpt is insufficient" and defaulted anyway —
verdicts that were artefacts of starved context, not judgements of the code. Re-running with WHOLE files flipped
5 of 35 verdicts; the money was spent on judgements of starved context. A rule ("read whole files") existed and
did not prevent it, because a rule you must remember is absent when it matters.

So this makes truncation HARD instead of forbidden-by-memory:
  * attach_whole(path) takes a PATH, not text — the reading happens HERE, so a caller cannot pre-truncate;
  * it reads the ENTIRE file and stamps a header the MODEL also sees (sha256, line count, byte count, COMPLETE),
    so a downstream clip (context-window, copy-paste) is visible to the reader on both ends;
  * before returning it RECONSTRUCTS the source from the rendered block and requires byte-for-byte equality — a
    CHECKED FACT — raising PartialFileError (fail closed, send nothing) if it does not; FileNotFoundError if the
    file is absent (a missing file is not an empty one).

adapters.call(..., files=[paths]) assembles the prompt through here, so a caller routing files that way has no
opportunity to truncate at all.
"""
import hashlib
import os


class PartialFileError(RuntimeError):
    """The assembled block does not reconstruct the whole file. Fail closed — never send a starved prompt."""


def _render_lines(lines, line_numbered):
    """Each source line in its emitted form — numbered ('    3 | <line>') or verbatim. Factored out as the one
    seam the reconstruction check in attach_whole verifies, so a fault-injection test can perturb it and PROVE
    the guard fails closed (a rendering that drops or mangles a line must raise, never ship a starved prompt)."""
    if line_numbered:
        return [f"{i + 1:5d} | {ln}" for i, ln in enumerate(lines)]
    return list(lines)


def attach_whole(path, line_numbered=True):
    """Return (block, manifest) for the COMPLETE file at `path`, with a header the model also sees.

    Takes a PATH, not text — the read happens here so the caller cannot pre-truncate. Raises FileNotFoundError if
    absent (a missing file is not an empty one) and PartialFileError if the rendered block does not reconstruct
    the source byte-for-byte (defence against an encoding / line-join dropping content). manifest = {path, sha256,
    lines, bytes} for the caller's own record."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"attach_whole: {path} not on disk (a missing file is not an empty one)")
    raw = open(path, "rb").read()
    sha = hashlib.sha256(raw).hexdigest()[:12]
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines(keepends=True)         # keepends → each line carries its exact newline (or none, last line)
    nl = chr(10)                                    # not '\n' — a backslash inside an f-string expr is a py39 SyntaxError

    # Render every source line verbatim (numbered or not); `body` is built from ALL of `lines`, so completeness is
    # by construction and the check below is the paranoia guard against a rendering bug.
    rendered = _render_lines(lines, line_numbered)
    body = "".join(rendered)

    # SELF-VERIFY by RECONSTRUCTION — a byte-EQUALITY fact, not a substring heuristic. Strip the "<n> | " prefix
    # off each rendered line (partition keeps the content whatever the line-count width, and even when the content
    # itself contains ' | ') and require the rejoined result to equal the source exactly. Any dropped, added, or
    # altered line makes recon != text and fails closed — a starved prompt is never sent.
    recon = "".join(r.partition(" | ")[2] for r in rendered) if line_numbered else body
    if recon != text:
        raise PartialFileError(f"attach_whole: the assembled block for {path} does not reconstruct the source")

    name = os.path.basename(path)
    header = f"--- FILE: {name}  ({len(lines)} lines, {len(raw)} bytes, sha256:{sha}, COMPLETE) ---"
    footer = f"--- END {name} ({len(lines)} lines) ---"
    block = f"{header}{nl}{body}"
    if not block.endswith(nl):                     # a file with no trailing newline must not run the footer onto its last line
        block += nl
    block += f"{footer}{nl}"
    return block, {"path": path, "sha256": sha, "lines": len(lines), "bytes": len(raw)}


def attach_many(paths, line_numbered=True):
    """Several whole files, each stamped + self-verified. Returns (concatenated_block, [manifest, ...])."""
    blocks, manifests = [], []
    for p in paths:
        b, m = attach_whole(p, line_numbered)
        blocks.append(b)
        manifests.append(m)
    return (chr(10)).join(blocks), manifests
