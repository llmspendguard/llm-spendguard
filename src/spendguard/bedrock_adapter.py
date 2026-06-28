"""AWS Bedrock coverage — direct boto3 (for teams NOT routing through LiteLLM). Patches botocore's single dispatch
method `BaseClient._make_api_call`, and for bedrock-runtime model invocations records token usage into the SAME
realtime ledger as the SDK gate. CAPTURE-focused + strictly FAIL-OPEN: it never alters or blocks the AWS call (the
real call runs untouched; only our recording is guarded).

Opt-in: `spendguard.install_bedrock()` after importing boto3 (botocore is heavy/common, so the startup gate only
auto-wires it if botocore is already imported — never force-imports it for non-AWS users). Patching the ONE class
method covers every boto3 client, created before or after."""
import functools, sys

_OPS = {"Converse", "InvokeModel"}     # non-stream model invocations whose usage we can read without consuming the body


def _toks(operation_name, response):
    """(in_tok, out_tok) from a Bedrock response. Converse carries `response['usage']`; InvokeModel returns the counts
    in response HEADERS (so we never read/consume the body StreamingBody the caller needs). Best-effort → (0,0)."""
    if operation_name == "Converse":
        u = (response or {}).get("usage") or {}
        return int(u.get("inputTokens") or 0), int(u.get("outputTokens") or 0)
    hdr = (((response or {}).get("ResponseMetadata") or {}).get("HTTPHeaders") or {})
    return (int(hdr.get("x-amzn-bedrock-input-token-count") or 0),
            int(hdr.get("x-amzn-bedrock-output-token-count") or 0))


def _wrap(orig):
    @functools.wraps(orig)
    def w(self, operation_name, api_params):
        try:
            svc = self.meta.service_model.service_name
        except Exception:
            svc = ""
        if svc != "bedrock-runtime" or operation_name not in _OPS:
            return orig(self, operation_name, api_params)          # fast passthrough for all other boto3 traffic
        resp = orig(self, operation_name, api_params)              # the real AWS call — untouched, errors propagate
        try:
            from . import gate
            model = (api_params or {}).get("modelId") or ""
            in_tok, out_tok = _toks(operation_name, resp)
            if in_tok or out_tok:
                gate._record_rt(model, {"model": model}, in_tok, out_tok, provider="bedrock")
        except Exception as e:
            print(f"[spend_gate] WARN bedrock capture failed ({e}); call unaffected", file=sys.stderr)
        return resp
    w._spend_gated = True
    return w


def install(force: bool = False) -> bool:
    """Patch botocore's dispatch so bedrock-runtime usage is captured (idempotent). Only wires if botocore is already
    imported, unless `force` (the explicit `spendguard.install_bedrock()` path). Returns True iff now patched."""
    if not force and sys.modules.get("botocore") is None:
        return False
    try:
        import botocore.client as bc
    except Exception:
        return False
    if getattr(bc.BaseClient._make_api_call, "_spend_gated", False):
        return True
    bc.BaseClient._make_api_call = _wrap(bc.BaseClient._make_api_call)
    return True
