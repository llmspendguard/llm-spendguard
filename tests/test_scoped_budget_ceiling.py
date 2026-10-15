"""Offline behavioral coverage of authorized running budgets at the existing call door."""
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

os.environ['SPENDGUARD_HOME'] = tempfile.mkdtemp(prefix='sg-scoped-budget-')
os.environ['OPENAI_API_KEY'] = 'sk-offline-scoped-budget'
os.environ['ANTHROPIC_API_KEY'] = 'sk-ant-offline-scoped-budget'
os.environ.pop('GATE_ALLOW', None)
from spendguard import config, calls, gate, bakeoff, adapters, whole_job, crossllm, vendor_call

with patch.object(config, '_cfg_get', return_value={}):
    assert config.intent_cap('scope-unit') is None
    with patch.dict(os.environ, {'GATE_INTENT_CAP_SCOPE_UNIT': '8'}):
        assert config.intent_cap('scope-unit') == 8
        with config.scoped_intent_cap('scope-unit', 5):
            assert config.intent_cap('scope-unit') == 5
            with config.scoped_intent_cap('scope-unit', 3):
                assert config.intent_cap('scope-unit') == 3
            assert config.intent_cap('scope-unit') == 5
            try:
                with config.scoped_intent_cap('scope-unit', 2):
                    raise RuntimeError('restore on failure')
            except RuntimeError:
                pass
            assert config.intent_cap('scope-unit') == 5
            with config.scoped_intent_cap('other-intent', 1):
                assert config.intent_cap('scope-unit') == 5
        assert config.intent_cap('scope-unit') == 8
        with config.scoped_intent_cap('scope-unit', 12):
            assert config.intent_cap('scope-unit') == 8
        with config.scoped_intent_cap('scope-unit', None):
            assert config.intent_cap('scope-unit') == 8
    assert config.intent_cap('scope-unit') is None
with patch.object(config, '_cfg_get', return_value={'scope-unit': 4}):
    with config.scoped_intent_cap('scope-unit', 9):
        assert config.intent_cap('scope-unit') == 4
    with config.scoped_intent_cap('scope-unit', 2):
        assert config.intent_cap('scope-unit') == 2
    assert config.intent_cap('scope-unit') == 4

# Overruns stop NEW dispatches; an already admitted worker completes and isn't cancelled.
started, finish = threading.Event(), threading.Event()
def scoped_budget_inflight_worker():
    started.set()
    assert finish.wait(10)
    return 'completed'
with patch.object(gate, '_allow', return_value=False), patch.object(calls, 'intent_spend', return_value=0) as spend:
    with config.scoped_intent_cap('budget-workload', 5), ThreadPoolExecutor(max_workers=1) as pool:
        gate._intent_door_gate('budget-workload', 1, 'offline', 'model')
        pending = pool.submit(scoped_budget_inflight_worker)
        assert started.wait(10)
        try:
            for amount in (1, 2, 3):
                spend.return_value = amount
                gate._intent_door_gate('budget-workload', 1, 'offline', 'model')
            spend.return_value = 6
            try:
                gate._intent_door_gate('budget-workload', 1, 'offline', 'model')
            except gate.SpendGateRefused as refusal:
                assert 'budget_usd=$5' in str(refusal)
                assert 'cumulative spend $6' in str(refusal)
                assert 're-approve' in str(refusal)
            else:
                raise AssertionError('overrun admitted')
            assert not pending.cancelled()
        finally:
            finish.set()
        assert pending.result() == 'completed'
    assert config.intent_cap('budget-workload') is None
    with config.scoped_intent_cap('budget-workload', None):
        gate._intent_door_gate('budget-workload', 100, 'offline', 'model')

# Multi-intent phases share ONE authorization, including the meta judge's real door.
with patch.object(gate, '_allow', return_value=False), patch.object(calls, 'intent_spend', side_effect=lambda label, window: {'phase-a': 3, 'spendguard:bakeoff-judge': 3}.get(label, 0)):
    with config.scoped_intent_cap(('phase-a', 'spendguard:bakeoff-judge'), 5):
        with calls.context(intent='spendguard:bakeoff-judge'):
            try:
                gate._rt_precheck_usd('offline', 'model', 0)
            except gate.SpendGateRefused as refusal:
                assert 'cumulative spend $6' in str(refusal)
            else:
                raise AssertionError('meta phase escaped job ceiling')

# Real bakeoff orchestration: admission passes; the judge's deliberate stop must propagate.
def scoped_budget_fake_candidate(*args, **kwargs):
    assert config.intent_cap('bakeoff-budget') == 5
    return {'text': 'candidate output', 'cost': 1}
def scoped_budget_refusing_judge(*args, **kwargs):
    gate._intent_door_gate('spendguard:bakeoff-judge', 1, 'offline', 'model')
with patch.object(bakeoff, '_plan', return_value=(1, {'offline:model': 1}, 1, 1)), patch.object(adapters, 'call', side_effect=scoped_budget_fake_candidate), patch.object(bakeoff, '_judge_one', side_effect=scoped_budget_refusing_judge), patch.object(calls, 'intent_spend', return_value=6), patch.object(gate, '_allow', return_value=False):
    try:
        bakeoff.bakeoff('bakeoff-budget', candidates=['offline:model'], prompts=['task'], judge_model='offline:judge', run=True, budget_usd=5)
    except gate.SpendGateRefused:
        pass
    else:
        raise AssertionError('bakeoff swallowed refusal')
assert config.intent_cap('bakeoff-budget') is None

# Multiple whole-job groups keep the same shared scope until all groups are finished.
planned = {'bad_jobs': [], 'unpriced': [], 'priced_est_usd': 2, 'groups': {label: [{'id': label}] for label in ('group-a', 'group-b')}, 'plan': [{'intent': label, 'method': 'realtime', 'est_usd': 1} for label in ('group-a', 'group-b')]}
def scoped_budget_fake_group(intent, *args):
    assert config.intent_cap(intent) == 5
    assert config.scoped_intent_authorizations(intent)[0][2] == ('group-a', 'group-b')
    return {intent: {'text': 'ok'}}
with patch.object(whole_job, 'plan_jobs', return_value=planned), patch.object(whole_job, '_run_realtime_group', side_effect=scoped_budget_fake_group):
    result = whole_job.run_jobs([], {'budget_usd': 5}, checkpoint='/private/tmp/offline-budget-checkpoint')
    assert len(result['results']) == 2
assert not config.scoped_intent_authorizations('group-a')
print('ok — scoped budgets: precedence, tightening, nesting, exceptions, no-budget, under-budget, overruns, shared phases, no cancellation')

# Cross-LLM surfaces the existing typed stop even though vendor_call stores it as a result.
def scoped_budget_fake_fan(*args, **kwargs):
    assert config.intent_cap('panel-budget') == 5
    return {'results': [vendor_call.Result(vendor_call.GATE_REFUSED, 'offline', 'model', error='budget_usd re-approve')], 'ok': [], 'failed': [], 'n': 1, 'n_ok': 0, 'complete': False}
with patch.object(crossllm, '_estimate_metered', return_value=(1, {}, [])), patch.object(vendor_call, 'fan_out', side_effect=scoped_budget_fake_fan):
    try:
        crossllm.ask('task', vendors=['openai:offline-model'], purpose='panel-budget', budget_usd=5, preflight=False)
    except gate.SpendGateRefused:
        pass
    else:
        raise AssertionError('panel swallowed typed stop')
assert config.intent_cap('panel-budget') is None

# Budgeted first-success panels drain stragglers and surface their typed refusals.
# The unbudgeted API keeps its existing early-return behavior.
completed_vendors = []
def scoped_budget_panel_vendor(vendor, model, *args, **kwargs):
    completed_vendors.append(vendor)
    if vendor == 'refusing':
        return vendor_call.Result(vendor_call.GATE_REFUSED, vendor, model, error='budget_usd exhausted; re-approve')
    return vendor_call.Result(vendor_call.OK, vendor, model, text='done')
with patch.object(vendor_call, 'call', side_effect=scoped_budget_panel_vendor), patch.object(vendor_call, 'time_budget', return_value=(None, 'offline')):
    try:
        vendor_call.first_ok([('successful', 'model'), ('refusing', 'model')], 'task', deadline_s=10, wait_for_inflight=True)
    except gate.SpendGateRefused:
        assert set(completed_vendors) == {'successful', 'refusing'}
    else:
        raise AssertionError('first-success panel hid a straggler refusal')
