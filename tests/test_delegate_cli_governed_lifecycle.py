"""CLI lifecycle controls use registered declarations and isolate result stdout."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from unittest.mock import patch

from spendguard import delegate_jobs, delegate_router, fleet
from spendguard.capability_cli_help_generation import render_capability_cli_help

DELEGATE_CLI_TEST_JOB_ID = __name__
DELEGATE_CLI_TEST_TEXT = 'complete delegate result'
DELEGATE_CLI_TEST_TIMEOUT = 1.25


def invoke_delegate_lifecycle_cli(arguments):
    output, errors = io.StringIO(), io.StringIO()
    with patch.object(fleet, 'register_consumer'), redirect_stdout(output), redirect_stderr(errors):
        code = delegate_router.delegate_cli(arguments)
    return code, output.getvalue(), errors.getvalue()


def verify_delegate_cli_lifecycle_controls():
    terminal = {'state': 'killed', 'status': 'killed', 'job_id': DELEGATE_CLI_TEST_JOB_ID}
    for flag, force in (('--cancel', False), ('--kill', True)):
        with patch.object(delegate_jobs, 'cancel_delegate_job', return_value=terminal) as cancel:
            code, output, errors = invoke_delegate_lifecycle_cli([flag, DELEGATE_CLI_TEST_JOB_ID])
        assert code == 0 and json.loads(output) == terminal and not errors
        cancel.assert_called_once_with(DELEGATE_CLI_TEST_JOB_ID, force=force)
    with patch.object(delegate_jobs, 'wait_for_delegate_job', return_value=terminal) as wait:
        code, output, errors = invoke_delegate_lifecycle_cli([
            '--collect', DELEGATE_CLI_TEST_JOB_ID, '--wait', '--timeout-s', str(DELEGATE_CLI_TEST_TIMEOUT)])
    assert code == 0 and json.loads(output) == terminal and not errors
    wait.assert_called_once_with(DELEGATE_CLI_TEST_JOB_ID, timeout=DELEGATE_CLI_TEST_TIMEOUT)


def verify_delegate_cli_execution_streams_and_alias():
    executed = {'status': 'executed', 'execution': {'text': DELEGATE_CLI_TEST_TEXT},
                'receipt': {'real_api_usd': 0, 'subscription_usd': 0, 'plan': __name__,
                            'est_value_usd': 0, 'saved_overage_usd': None}}
    for mixed in (False, True):
        with ExitStack() as stack:
            stack.enter_context(patch.object(delegate_router, '_delegate_release_state_warning', return_value='release diagnostic'))
            stack.enter_context(patch.object(delegate_router, 'classify_task', return_value={'kind': 'agentic'}))
            start = stack.enter_context(patch.object(delegate_router, 'start_delegate_job', return_value={
                'state': 'running', 'job_id': DELEGATE_CLI_TEST_JOB_ID}))
            stack.enter_context(patch.object(delegate_router, 'wait_for_delegate_job_handoff', return_value=executed))
            code, output, errors = invoke_delegate_lifecycle_cli([
                __name__, '--execute', '--warm', *(['--mixed-stream'] if mixed else [])])
        assert code == 0
        assert start.call_args.kwargs['detached'] is True
        assert start.call_args.kwargs['warm'] is True
        if mixed:
            assert 'release diagnostic' in output and 'Subscription' in output and not errors
        else:
            assert output == DELEGATE_CLI_TEST_TEXT + '\n', output
            assert 'release diagnostic' in errors and 'Subscription' in errors, errors


def verify_delegate_cli_generated_lifecycle_help():
    help_text = render_capability_cli_help('delegate')
    for flag in ('--execute', '--warm', '--cancel', '--kill', '--wait', '--timeout-s', '--mixed-stream'):
        assert flag in help_text, flag
    assert 'canonical' in help_text and 'stderr' in help_text


verify_delegate_cli_lifecycle_controls()
verify_delegate_cli_execution_streams_and_alias()
verify_delegate_cli_generated_lifecycle_help()
print('delegate CLI lifecycle: cancellation, waiting, execute alias, warm, generated help and stream separation passed')
