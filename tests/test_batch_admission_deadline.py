"""Offline per-job admission contract, including expiry during control-plane calls."""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from spendguard import submit as admission, gate

ADMISSION_CLOCK_START = 100.0
ADMISSION_TIGHT_BUDGET = 3.0
ADMISSION_GENEROUS_BUDGET = 100.0
ADMISSION_STEP_ELAPSED = 1.0
ADMISSION_EXPIRED_OFFSET = -1.0
ADMISSION_MODEL = 'offline-admission-model'
ADMISSION_ENDPOINT = '/v1/chat/completions'
ADMISSION_ESTIMATE = dict(requests=1, mode='batch', in_tok=1, out_tok=1, out_basis='measured',
                          token_basis='mock', cost=0, model=ADMISSION_MODEL)


def exercise_batch_admission_contract():
    clock = [ADMISSION_CLOCK_START]
    events = []
    fault_stage = [None]
    current_deadline = [None]

    def admission_mock_dispatch(stage, **kwargs):
        if current_deadline[0] is None:
            assert 'timeout' not in kwargs
        else:
            assert kwargs['timeout'] == current_deadline[0] - clock[0]
            assert kwargs['timeout'] > 0
        events.append((stage, current_deadline[0], kwargs.get('timeout')))
        clock[0] += (ADMISSION_GENEROUS_BUDGET if fault_stage[0] == stage else ADMISSION_STEP_ELAPSED)
        return SimpleNamespace(id=stage)

    def admission_mock_options(**kwargs):
        assert kwargs == {'max_retries': admission._BATCH_ADMISSION_RETRIES}
        return client

    client = SimpleNamespace(
        with_options=admission_mock_options,
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: admission_mock_dispatch('preflight', **kw))),
        embeddings=SimpleNamespace(create=lambda **kw: admission_mock_dispatch('preflight', **kw)),
        files=SimpleNamespace(create=lambda **kw: admission_mock_dispatch('upload', **kw)),
        batches=SimpleNamespace(create=lambda **kw: admission_mock_dispatch('create', **kw)))
    with tempfile.TemporaryDirectory() as directory:
        request = Path(directory) / 'requests.jsonl'
        request.write_text(json.dumps({'body': {'model': ADMISSION_MODEL, 'messages': []}}) + '\n')
        with patch.dict(sys.modules, {'openai': SimpleNamespace(OpenAI=lambda **kw: client)}), \
             patch.object(admission.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(admission, '_api_key', return_value=None), \
             patch.object(admission, 'AUDIT_DIR', directory), \
             patch.object(admission, 'estimate_submission_jsonl_cost', return_value=ADMISSION_ESTIMATE), \
             patch.object(admission.bulk_resilience, 'require_resilient'), \
             patch.object(gate, 'record_accepted_batch_estimate'):
            def admission_run_job(deadline):
                current_deadline[0] = deadline
                return admission.guarded_submit(str(request), ADMISSION_MODEL, None, deadline_at=deadline)

            tight = clock[0] + ADMISSION_TIGHT_BUDGET
            assert admission_run_job(tight) == 'create'
            assert [e[1] for e in events] == [tight] * len(events)
            generous = clock[0] + ADMISSION_GENEROUS_BUDGET
            before = len(events)
            try:
                admission_run_job(clock[0] + ADMISSION_EXPIRED_OFFSET)
                raise AssertionError('expired job admitted')
            except admission.BatchAdmissionDeadlineRefused:
                assert len(events) == before
            assert admission_run_job(generous) == 'create'
            assert all(e[1] == generous for e in events[before:])
            for stage in ('preflight', 'upload'):
                fault_stage[0] = stage
                before = len(events)
                try:
                    admission_run_job(clock[0] + ADMISSION_TIGHT_BUDGET)
                    raise AssertionError('fault-expired job admitted')
                except admission.BatchAdmissionDeadlineRefused as exc:
                    assert ('upload' if stage == 'preflight' else 'create') in str(exc)
                    assert 'create' not in [e[0] for e in events[before:]]
                fault_stage[0] = None
                assert admission_run_job(clock[0] + ADMISSION_GENEROUS_BUDGET) == 'create'
            for invalid in (float('nan'), float('inf'), float('-inf'), 'timestamp', True):
                before = len(events)
                try:
                    admission_run_job(invalid)
                    raise AssertionError('invalid deadline admitted')
                except admission.BatchAdmissionDeadlineRefused:
                    assert len(events) == before
            assert admission_run_job(None) == 'create'
    print('ok — per-job admission deadlines, dispatch budgets, clock chaos and legacy None')


exercise_batch_admission_contract()
