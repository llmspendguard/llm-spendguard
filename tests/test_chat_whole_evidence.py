"""Offline executable guards: whole chat evidence, bounded synthesis, and estimate-first."""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from unittest.mock import patch

if not os.environ.get("SPENDGUARD_CHAT_EVIDENCE_GUARD_CHILD"):
    with tempfile.TemporaryDirectory(prefix="spendguard-chat-evidence-") as evidence_guard_home:
        evidence_guard_env = dict(os.environ, HOME=evidence_guard_home, SPENDGUARD_HOME=evidence_guard_home,
                                  SPENDGUARD_TEST_ISOLATED="1", SPENDGUARD_CHAT_EVIDENCE_GUARD_CHILD="1")
        sys.exit(subprocess.call([sys.executable, __file__], env=evidence_guard_env))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from spendguard import adapters, chat, config, pricing, ui

EVIDENCE_GUARD_FIELD_LENGTH = 401
EVIDENCE_GUARD_CORPUS_SIZE = 57
EVIDENCE_GUARD_INPUT_BUDGET = 7000
EVIDENCE_GUARD_OUTPUT_BUDGET = 300
EVIDENCE_GUARD_PROVIDER_INPUT_BUDGET = 1500
EVIDENCE_GUARD_MODEL = "offline-chat-evidence-model"
EVIDENCE_GUARD_TAXONOMY = {"orgs": [], "teams": [], "projects": []}
EVIDENCE_GUARD_REPLY = json.dumps(dict(story="complete", insights=[], orgs=[], teams=[], projects=[]))


def verify_chat_digest_stores_whole_first_user():
    long_first_user = "FIRST-USER-" + "u" * EVIDENCE_GUARD_FIELD_LENGTH
    conv = {"uuid": "digest-whole", "name": "whole title", "summary": "whole summary", "updated_at": "2026-01-01"}
    detail = {"model": EVIDENCE_GUARD_MODEL, "chat_messages": [
        {"sender": "human", "created_at": "2026-01-01T00:00:00Z", "text": long_first_user}]}
    digest = chat._digest_conv(conv, detail, {})
    assert digest["first_user"] == long_first_user, (len(digest["first_user"]), len(long_first_user))


def verify_chat_whole_judgement_fields_and_chunks():
    corpus = [dict(uuid=f"evidence-{i}", title=f"TITLE-{i}-" + "t" * EVIDENCE_GUARD_FIELD_LENGTH,
                   summary=f"SUMMARY-{i}-" + "s" * EVIDENCE_GUARD_FIELD_LENGTH,
                   first_user=f"FIRST-{i}-" + "f" * EVIDENCE_GUARD_FIELD_LENGTH, value=i)
              for i in range(EVIDENCE_GUARD_CORPUS_SIZE)]
    corpus[-1]["summary"] = ""  # Discovery must also preserve the whole first-user fallback.
    allocation_prompt = chat._chat_allocation_prompt(EVIDENCE_GUARD_TAXONOMY, corpus)
    for conv in corpus:
        for field in ("title", "summary", "first_user"):
            assert conv[field] in allocation_prompt, field

    def evidence_guard_cfg(section, key, default=None):
        return {"synth_input_tokens": EVIDENCE_GUARD_INPUT_BUDGET,
                "synth_output_tokens": EVIDENCE_GUARD_OUTPUT_BUDGET}.get(key, default)

    with contextlib.ExitStack() as guards:
        guards.enter_context(patch.object(chat, "_load_state", return_value={"convs": {c["uuid"]: c for c in corpus}}))
        guards.enter_context(patch.object(chat, "_save_state"))
        guards.enter_context(patch.object(chat, "_convs_in_range", return_value=corpus))
        guards.enter_context(patch.object(chat, "_taxonomy", return_value=(EVIDENCE_GUARD_TAXONOMY, True)))
        guards.enter_context(patch.object(chat, "_toklen", side_effect=len))
        guards.enter_context(patch.object(config, "_cfg_get", side_effect=evidence_guard_cfg))
        guards.enter_context(patch.object(config, "advisor_model", return_value=EVIDENCE_GUARD_MODEL))
        guards.enter_context(patch.object(config, "meta_cap", return_value=0))
        guards.enter_context(patch.object(pricing, "realtime_cost", side_effect=lambda model, inp, out: inp + out))
        estimate = guards.enter_context(patch.object(ui, "estimate_only"))
        model_call = guards.enter_context(patch.object(adapters, "call", return_value={"text": EVIDENCE_GUARD_REPLY, "cost": 0}))
        guards.enter_context(contextlib.redirect_stdout(io.StringIO()))
        for action in (chat.story, chat.discover_taxonomy):
            model_call.reset_mock()
            estimate.reset_mock()
            assert action(run=False) == 0
            assert not model_call.called and estimate.call_count == 1
            reserved = estimate.call_args.kwargs["cost"]
            kwargs = {"apply": False} if action is chat.discover_taxonomy else {}
            assert action(run=True, **kwargs) == 0
            assert model_call.call_count > 1
            prompts = [call.args[1] for call in model_call.call_args_list]
            for conv in corpus:
                selected = conv["summary"] or (conv["first_user"] if action is chat.discover_taxonomy else "")
                assert any(conv["title"] in prompt and selected in prompt for prompt in prompts), conv["uuid"]
            actual_maximum = sum(len(call.kwargs["system"] + call.args[1]) + call.kwargs["max_tokens"]
                                 for call in model_call.call_args_list)
            assert reserved >= actual_maximum, (reserved, actual_maximum)
        with contextlib.redirect_stdout(io.StringIO()) as sample_output:
            assert chat.discover_taxonomy(sample=1, run=False) == 0
        assert f"sampling 1 of {len(corpus)} by value; {len(corpus) - 1} omitted" in sample_output.getvalue()
        assert "chat_sampling_gap" in sample_output.getvalue()
        with patch.object(config, "_cfg_get", side_effect=lambda section, key, default=None:
                          {"synth_input_tokens": EVIDENCE_GUARD_INPUT_BUDGET,
                           "synth_output_tokens": EVIDENCE_GUARD_OUTPUT_BUDGET}.get(key, default)), \
             patch.object(chat, "_convs_in_range", return_value=[dict(corpus[0], summary="x" * EVIDENCE_GUARD_INPUT_BUDGET)]), \
             contextlib.redirect_stdout(io.StringIO()) as oversize_output:
            model_call.reset_mock()
            assert chat.story(run=True) == 1
            assert not model_call.called
        assert "chat_oversized_record_gap" in oversize_output.getvalue()


def verify_chat_incomplete_structured_reply_is_named_gap():
    incomplete_metadata = ({"finish_reason": "length"}, {"truncated": True},
                           {"out_tok": EVIDENCE_GUARD_OUTPUT_BUDGET})
    for metadata in incomplete_metadata:
        with patch.object(chat, "_toklen", side_effect=len), \
             patch.object(adapters, "call", return_value={"text": EVIDENCE_GUARD_REPLY, "cost": 0, **metadata}):
            result = chat._chat_execute_evidence_plan(EVIDENCE_GUARD_MODEL,
                        (["whole input"], EVIDENCE_GUARD_OUTPUT_BUDGET, EVIDENCE_GUARD_INPUT_BUDGET, 0),
                        "system", "prefix", "spendguard:worklog")
        assert "chat_reply_incomplete_gap" in result["error"] and "text" not in result, metadata


def verify_chat_truncated_allocation_never_assigns():
    allocation_budget = chat.CHAT_ALLOCATION_OUTPUT_PER_RECORD + chat.CHAT_ALLOCATION_OUTPUT_OVERHEAD
    allocation_reply = json.dumps({"items": [{"i": 0, "org": "guard-assignment", "team": "guard-team",
                                  "allocation": [{"project": "guard-project", "pct": 100}], "confidence": 100}]})
    for metadata in ({"finish_reason": "length"}, {"truncated": True}, {"out_tok": allocation_budget}):
        conv = {"uuid": "guard-unassigned", "title": "whole title", "summary": "whole summary"}
        state = {"convs": {conv["uuid"]: conv}}
        with patch.object(chat, "_load_state", return_value=state), \
             patch.object(chat, "_save_state") as allocation_save, \
             patch.object(chat, "_taxonomy", return_value=(EVIDENCE_GUARD_TAXONOMY, True)), \
             patch.object(config, "_cfg_get", side_effect=lambda section, key, default=None: default), \
             patch.object(config, "advisor_model", return_value=EVIDENCE_GUARD_MODEL), \
             patch.object(config, "meta_cap", return_value=0), \
             patch.object(pricing, "realtime_cost", return_value=0), \
             patch.object(adapters, "call", return_value={"text": allocation_reply, "cost": 0, **metadata}), \
             contextlib.redirect_stdout(io.StringIO()) as allocation_output:
            assert chat.classify_chat_allocations(run=True) == 0
        assert not conv.get("org") and not conv.get("allocation"), metadata
        assert "chat_allocation_reply_gap" in allocation_output.getvalue(), metadata
        assert allocation_save.called


def verify_chat_provider_limits_bound_plan_before_spend():
    whole_records = [f"provider-bound-record-{i}:" + "x" * EVIDENCE_GUARD_FIELD_LENGTH
                     for i in range(EVIDENCE_GUARD_CORPUS_SIZE)]
    with patch.object(chat, "_toklen", side_effect=len), \
         patch.object(config, "_cfg_get", side_effect=lambda section, key, default=None:
                      {"synth_input_tokens": EVIDENCE_GUARD_INPUT_BUDGET,
                       "synth_output_tokens": EVIDENCE_GUARD_OUTPUT_BUDGET}.get(key, default)), \
         patch.object(pricing, "max_input_tokens", return_value=EVIDENCE_GUARD_PROVIDER_INPUT_BUDGET), \
         patch.object(pricing, "max_output_tokens", return_value=EVIDENCE_GUARD_OUTPUT_BUDGET), \
         patch.object(adapters, "call") as provider_guard_call:
        plan = chat._chat_whole_evidence_plan(whole_records, "prefix", "system", EVIDENCE_GUARD_MODEL)
        assert plan[2] == EVIDENCE_GUARD_PROVIDER_INPUT_BUDGET
        assert len(plan[0]) > 1
        assert all(len("system" + prompt) <= EVIDENCE_GUARD_PROVIDER_INPUT_BUDGET for prompt in plan[0])
        assert all(any(record in prompt for prompt in plan[0]) for record in whole_records)
        with patch.object(pricing, "max_output_tokens", return_value=EVIDENCE_GUARD_OUTPUT_BUDGET - 1), \
             patch.object(chat, "_load_state", return_value={}), \
             patch.object(chat, "_convs_in_range", return_value=[{"title": whole_records[0], "summary": "whole"}]), \
             patch.object(config, "advisor_model", return_value=EVIDENCE_GUARD_MODEL), \
             contextlib.redirect_stdout(io.StringIO()) as provider_gap_output:
            assert chat.story(run=True) == 1
        assert "chat_synthesis_output_gap" in provider_gap_output.getvalue()
        assert not provider_guard_call.called


if __name__ == "__main__":
    verify_chat_digest_stores_whole_first_user()
    verify_chat_whole_judgement_fields_and_chunks()
    verify_chat_incomplete_structured_reply_is_named_gap()
    verify_chat_truncated_allocation_never_assigns()
    verify_chat_provider_limits_bound_plan_before_spend()
    print("OK chat whole evidence, digest guard, multi-call coverage, caged estimates, named gaps, incomplete JSON guard")
