# Capability reference

Generated from the capability registry. Edit the registry, then regenerate this reference.

## Cost and routing controls

For measured money-saving routing, call adapters.call(..., reasoning='best-value', intent=...). An explicit best-value request may select both model and reasoning effort using measured quality for the intent; without qualifying evidence it keeps the requested model.

When best-value is inherited from advisor.default_reasoning rather than explicitly requested, a supplied model stays pinned and the advisor selects its reasoning effort. no_substitution also pins the model.

intent labels adapters.call work for attribution, quality evidence and routing. vendor_call.call uses purpose for the corresponding intent context and accepts reasoning, rather than an intent keyword.

no_metered_fallback=True is the explicit subscription-only contract: a lane failure returns an error instead of billing a metered fallback. metered_only on adapters.call explicitly selects paid API execution.

Normal adapters.call and vendor_call.call output budgets come from adapters.output_budget and the model's published output ceiling. Their max_tokens compatibility argument does not impose a caller-selected normal output cap; billing remains based on actual generated tokens. Input context limits are checked separately.

vendor_call.call requires a positive deadline_s covering the whole call and every retry; attempts and backoff_s govern retry handling. adapters.call timeout_s bounds execution, and retries governs retry handling. Each paid attempt can bill; a shorter output argument is not a money budget.

adapters.call measurement pins model attribution. temperature, top_p and seed are measurement controls that force paid execution and a pinned model; production calls reject these controls. Anthropic does not support seed.

## Capability index

Each absence below carries the registry's reviewed reason.

| ID | Capability | CLI | API | MCP | Core contracts |
| --- | --- | --- | --- | --- | --- |
| C001 | Converge and attest installed consumers onto the selected distribution | distribute | spendguard.distribution.distribute_fleet(*, check=False, roots=None, dry_run=False, settings=None, version=None) | ABSENT (DELIBERATE): Installed package convergence belongs to the external host deployment workflow. | [spendguard.distribution:distribute_fleet](#core-623c13035b1e27f089e5786a20cc25cc60c17c94812688cabe1893620b843e38) |
| C002 | Read enforcement status and current budget usage | status | capability_status_gate_control_gate_runtime | capability_status_gate_control_gate_runtime | [spendguard.gate:control_gate_runtime](#core-42d81a3a5fc253861eddc15b795e36882b01e76fef1c54d938e9df61159fbd95) |
| C003 | Enable enforcement for future processes | on | capability_on_gate_control_gate_runtime | capability_on_gate_control_gate_runtime | [spendguard.gate:control_gate_runtime](#core-42d81a3a5fc253861eddc15b795e36882b01e76fef1c54d938e9df61159fbd95) |
| C004 | Disable enforcement for future processes | off | capability_off_gate_control_gate_runtime | capability_off_gate_control_gate_runtime | [spendguard.gate:control_gate_runtime](#core-42d81a3a5fc253861eddc15b795e36882b01e76fef1c54d938e9df61159fbd95) |
| C005 | Diagnose enforcing interpreter, coverage, routing and configuration | doctor | capability_doctor_gate_control_gate_runtime | capability_doctor_gate_control_gate_runtime | [spendguard.gate:control_gate_runtime](#core-42d81a3a5fc253861eddc15b795e36882b01e76fef1c54d938e9df61159fbd95) |
| C006 | Inspect and optionally reap orphaned warm subscription servers | reap-orphans | spendguard.warm_stdio_daemon.reap_orphaned_warm_servers(lane=None, execute=True) | ABSENT (DELIBERATE): Reaping host server processes requires lifecycle ownership outside the request transport. | [spendguard.warm_stdio_daemon:reap_orphaned_warm_servers](#core-13eed6da6c5f694b78a2450a016e89830b74d6b236fae2c75a79720eadcf0aad) |
| C007 | Recover, archive and optionally process stale lane queue work | queue-manage | spendguard.lane_queue.manage_queue_tick(execute=False, process=True, stale_days=None) | ABSENT (DELIBERATE): Recovering and processing stale worker queues belongs to the host queue operator. | [spendguard.lane_queue:manage_queue_tick](#core-bf32bcd9f460e8fe104df07d2d2a1e9b23e87edb5790ea8a49ea53e72b965601) |
| C008 | Verify protected executor work did not leak to other providers | lane-leak-check | capability_lane_leak_check_cli_check_protected_lane_leaks | capability_lane_leak_check_cli_check_protected_lane_leaks | [spendguard.cli:check_protected_lane_leaks](#core-29db913bfc41ec2562dc18efb968d68668abb2867310880dd6413e4b16a58ad0) |
| C009 | Read typed ledger records, aggregates and counts | ledger-read | spendguard.ledger_read.read_spend_events(since=None, until=None, where=None, category_column=None, limit=None, path=None); spendguard.ledger_read.read_spend_aggregates(group_by=('provider', 'day'), since=None, until=None, path=None); spendguard.ledger_read.read_recent_calls(since=None, until=None, where=None, limit=None, path=None); spendguard.ledger_read.read_filtered_ledger_count(table=LEDGER_EVENTS_TABLE, since=None, until=None, where=None, path=None); spendguard.ledger_read.read_recorded_call_total(path=None, *, time_budget=DEFAULT_LEDGER_READ_TIME_BUDGET_S); spendguard.ledger_read.read_spend_events(since=None, until=None, where=None, category_column=None, limit=None, path=None); spendguard.ledger_read.read_ledger_event(event_id, path=None); spendguard.ledger_read.read_conversation_spend_since(since, conv, *, deadline_s=config.LEDGER_QUERY_DEADLINE_S); spendguard.ledger_read.ledger_read_metrics(); spendguard.budget.spent_since; spendguard.ledger_read.read_spend_since | spendguard_ledger_read(operation, table, group_by, event_id, since, conv, deadline_s, until, path) | [spendguard.ledger_read:read_spend_events](#core-b398bf40107dab1cea7744cd16ecc001901f0ea2385d41431cf38cbafa101fe2); [spendguard.ledger_read:read_spend_aggregates](#core-ddf77ddf36904d1ca7c1735445f192f7c2f647a660bd13f978c0364b9cff0d48); [spendguard.ledger_read:read_recent_calls](#core-2aaec9a980a22b3c742f76f114200ed3f1bc7fffa4229dbc846a0dcd9a15239b); [spendguard.ledger_read:read_filtered_ledger_count](#core-d3ffae247de77b97c78a30abd46b08799555e210d078955e0f674d8ac2dd3d0b); [spendguard.ledger_read:read_recorded_call_total](#core-ae84997608ea7521682713af895e129c5dd07123dc9f987873cbf01bf8e5401c); [spendguard.ledger_read:read_ledger_event](#core-fae328bd5129e10d684c1478adb8482ce57569324c91915aa2019a90f6fceb0f); [spendguard.ledger_read:read_conversation_spend_since](#core-f7572ae4d0cf3263a4263a25b1433bf400c30ee5648cf3950f9ae8160057701c); [spendguard.ledger_read:ledger_read_metrics](#core-a0a020bdbbce41054d34d973da5805386f95fe519d5a0e87d8361b0b2db5fe92); [spendguard.budget:spent_since](#core-29d7d8dee922197176e01c2f6ae85f9faef9b53bd9733d95b4d77e1bae448ddf); [spendguard.ledger_read:read_spend_since](#core-b079f527d01361bb8ce7f9e8d40fe51060c0c336bfaf942993a32f2402d051a7) |
| C010 | Backfill compression of retained database content | compress-content | spendguard.content_compression_backfill.compress_retained_content_database(database_path=None, *, batch_rows=CONTENT_BACKFILL_BATCH_ROWS) | ABSENT (DELIBERATE): Retained database compression is local storage maintenance, independent of user spending operations. | [spendguard.content_compression_backfill:compress_retained_content_database](#core-b6899eaf3192e427fae63d2b256c722cfbaa391c5bc743d47f714837417fec5b) |
| C011 | Evaluate an agent-spawn PreToolUse payload | agent-spawn-gate | spendguard.agent_spawn_gate.evaluate_pretooluse(payload) | ABSENT (DELIBERATE): PreToolUse hook payload evaluation belongs to the assistant lifecycle protocol. | [spendguard.agent_spawn_gate:evaluate_pretooluse](#core-f77c7da4899f31a23ea0dc3cff1e702a0414db79bbc295d14a244803469d2998) |
| C012 | Evaluate a context-residency PostToolUse payload | residency-gate | spendguard.residency_gate.evaluate_posttooluse(payload) | ABSENT (DELIBERATE): PostToolUse residency evaluation belongs to the assistant lifecycle protocol. | [spendguard.residency_gate:evaluate_posttooluse](#core-1e34c5a25669c09195934019309d0e1b75878c7f70c56bb9ad41934abc466ce5) |
| C013 | Evaluate weekly-cap enforcement for a PreToolUse payload | capguard | spendguard.weekly_cap_guard.weekly_cap_guard_pretooluse(payload) | ABSENT (DELIBERATE): Weekly-cap hook decisions belong to the assistant PreToolUse protocol. | [spendguard.weekly_cap_guard:weekly_cap_guard_pretooluse](#core-7c1a8e740c4381ec24a4fcbf193ded74db72075b8c1e0867aeb8eb98bdaee85b) |
| C014 | Install or remove the context-residency hook | install-residency-gate | spendguard.residency_gate.install_residency_gate(settings_path=None, uninstall=False, executable=None) | ABSENT (DELIBERATE): Installing client residency hooks requires local assistant configuration ownership. | [spendguard.residency_gate:install_residency_gate](#core-2f661b732c2c573a9ac2ff1a3e02f424be9211acc79b7262d958de191bfb6391) |
| C015 | Read current plan overage state | overage | spendguard.overage.current_overage_status(max_age_s=None, force=False) | spendguard_overage_status() | [spendguard.overage:current_overage_status](#core-1d58deb95680aa31e9e86f9bb0fb8750c22a4717445e3442e3d78e14dc3dffed) |
| C016 | Generate and optionally email the multi-window spend report | report | capability_report_report_produce_periodic_spend_report | capability_report_report_produce_periodic_spend_report | [spendguard.report:produce_periodic_spend_report](#core-69113dbcf5ecda85e1d1250027799361d9b29f6002425490d7cc319208867d97) |
| C017 | Project route economics for an intent workload | route-cost | spendguard.route_economics.route_report(intent, n_tasks, *, in_tok=None, out_tok=None, lane=None, batch_model=None, reserve_frac=None, now=None) | spendguard_route_cost(intent, n, in_tok, out_tok, lane, batch_model) | [spendguard.route_economics:route_report](#core-f5e5c64c72b4fa74c73ab7bde0027c748a4efda3ff3f0d75702e7c59abbfbd58) |
| C018 | Measure realized route results over a dated window | route-realized | spendguard.route_realized.route_realized_report(intent, *, since=None, as_of=None, limit=20) | spendguard_route_realized(intent, since, as_of, limit) | [spendguard.route_realized:route_realized_report](#core-020d1e50cc35858a5c3cba19cf1ab0292a24d26d429ddeffa9b28108c3edd231) |
| C019 | Compare lane and batch routing for an urgent or deferrable job horizon | route-horizon | spendguard.route_horizon.horizon_report(groups, *, lane=None, batch_model=None, realtime_model=None, reserve_frac=None, now=None) | spendguard_route_horizon(groups, lane, batch_model, realtime_model) | [spendguard.route_horizon:horizon_report](#core-0e7e2a8858fc7f6a5f9755a4c5a18b9ee04dbcf7ecda5a144d3f354335cd46bb) |
| C020 | Export ledger spend as FOCUS CSV or JSON with reconciliation | focus-export | spendguard.focus_export.export_rows(since=None, until=None); spendguard.focus_export.reconciliation_summary(rows); spendguard.focus_export.emit_csv(rows); spendguard.focus_export.emit_json(rows) | ABSENT (DELIBERATE): Writing a FOCUS exchange file is an operator export workflow. | [spendguard.focus_export:export_rows](#core-e0e2e38363bd19ac8d55de1847b6ca38074475c731e6e4ecd98da20c7cadb8bd); [spendguard.focus_export:reconciliation_summary](#core-8cfa7fedd8ff067898b691258877f1895e02a23538cb30bdb431df2226bb7366); [spendguard.focus_export:emit_csv](#core-3f3df827ad109de7d452d496937da7f4ac37bb2db6439c6510e45f504fcf6041); [spendguard.focus_export:emit_json](#core-cc2b8d9ef7b71f37150440535f18fcbe86f5ac7ba5fcab8818d0ba44793fd5bf) |
| C021 | Ingest GenAI telemetry spans into the ledger | otel-ingest | spendguard.otel_ingest.ingest(doc, project=None, dry_run=False) | capability_otel_ingest_otel_ingest_ingest | [spendguard.otel_ingest:ingest](#core-0a36a2f884875b52ca9eb67cab9f0215c513595163c94b6e5d211726d0a18f44) |
| C022 | Resolve and optionally correct stale model identifiers | preflight | spendguard.model_preflight.preflight_models(specs, correct=True) | capability_preflight_model_preflight_preflight_models | [spendguard.model_preflight:preflight_models](#core-24c7aaad1ed7c465b07d18e05496d5a1bff34d7465d95cc7ce4faae9c1ccc076) |
| C023 | Check catalog, keys, fallback economics and optional live failover | verify | spendguard.verify.verify_system(probe=False) | capability_verify_verify_verify_system | [spendguard.verify:verify_system](#core-7557f07359453eb415ba0ed0ad8d61b1ab4febda99a09a5360921f001379315d) |
| C024 | Audit code for key usage and gating coverage | keys-audit | capability_keys_audit_keys_audit_audit_provider_key_shadows | capability_keys_audit_keys_audit_audit_provider_key_shadows | [spendguard.keys_audit:audit_provider_key_shadows](#core-eda57e550bf214b1f2efde61b1c321ed7a0fe5e267369e3291bcc2e15e44a63e) |
| C025 | Compare provider billing truth against recorded spend | reconcile | spendguard.reconcile.report(ptmap=None, since=None) | capability_reconcile_reconcile_report | [spendguard.reconcile:report](#core-6887069946a073c9eb4a0f4d8317bc8f9bc1b7af57a1450b9f5887070e5cf57e) |
| C026 | Project cost for a request count and average token sizes | estimate | spendguard.estimate_cost; governing inputs: model,n,avg_in,avg_out,batch | capability_api_contract_30_spendguard_estimate_cost | [spendguard:estimate_cost](#core-ccc87e67e727901730b31b90826a426ac896675a8f239f4c760d1e471216ea0b) |
| C027 | Audit repository spend integration with optional deep adjudication | audit | capability_audit_audit_audit_repository_price_literals | capability_audit_audit_audit_repository_price_literals | [spendguard.audit:audit_repository_price_literals](#core-8712d8c9d2166d496e41346685492f107557eb2d7793d41a82aca1b60f18a7c3) |
| C028 | Find and adjudicate hardcoded token caps | token-caps | capability_token_caps_token_caps_review_output_token_caps | capability_token_caps_token_caps_review_output_token_caps | [spendguard.token_caps:review_output_token_caps](#core-b853d77c1603634b2e7509ba76a0300c3f329ea583116c26064db6a753fce1fa) |
| C029 | Submit capped OpenAI chat Batch tasks or prebuilt requests | batch-submit | spendguard.submit.submit_chat_tasks(tasks, model, *, system=None, schema=None, reasoning='minimal', max_out=None, cap_dollars=None, submit=True, intent=None, metadata=None, preflight=True, shard_size=None, force=False, avg_out_tokens=None); governing inputs: tasks,model,system,schema,reasoning,max_out,cap_dollars,submit,intent,metadata,preflight,shard_size,force,avg_out_tokens | capability_api_contract_11_submit_submit_chat_tasks | [spendguard.submit:submit_chat_tasks](#core-a52bbe600cbd0d4aa7b08937f2017069531a9173f2908c75a6ec70d33ed536c5) |
| C030 | Collect an OpenAI batch into a result file | batch-fetch | capability_batch_fetch_openai_batch_cli_download_completed_batch_output | capability_batch_fetch_openai_batch_cli_download_completed_batch_output | [spendguard.openai_batch_cli:download_completed_batch_output](#core-69a895154de3fda61f610db97431ed6ea0032fab0f2ebb9fd31d42cc7620bd0c) |
| C031 | Ask a cross-provider panel under a budget and deadline | ask | spendguard.crossllm.ask(prompt, *, vendors=None, n=None, schema=None, system=None, purpose='ask', deadline_s=None, budget_usd=None, mode='all', require=None, max_tokens=None, est_output_tokens=None, preflight=True, refuse_billed=False) | capability_ask_crossllm_ask | [spendguard.crossllm:ask](#core-2b2b2b602d41bb6497173212fa9c761d8af7bdca9bda4e2ad59ba556c6f0787e) |
| C032 | Comprehend a file corpus across subscription lanes | comprehend | spendguard.comprehend.comprehend_corpus(patterns, intent, question=None, model=None, run=False, budget_usd=None, lanes=None, max_chars=_DEFAULT_MAX_CHARS, checkpoint=None, refuse_billed=False) | capability_comprehend_comprehend_comprehend_corpus | [spendguard.comprehend:comprehend_corpus](#core-599ff4f027637b2ed47e9adb0625f40c13c44e39ca007aa586f8bf2406705a21) |
| C033 | Run the authenticated spendguard HTTP server | serve | ABSENT (DELIBERATE): Server lifecycle is an operator process concern; underlying server construction remains public. | ABSENT (DELIBERATE): Starting an HTTP server belongs to external process supervision. |  |
| C034 | Run the MCP transport server | mcp | ABSENT (DELIBERATE): Transport process lifecycle belongs to the protocol host. | ABSENT (DELIBERATE): Starting the current transport belongs to external process supervision. |  |
| C035 | Audit and optionally arm consumer workspaces | consumer-audit | spendguard.consumer_audit.audit_consumer_workspaces(deployed_sha, roots=None, auto_arm=True, dry_run=False, settings=None) | ABSENT (DELIBERATE): Workspace interpreter arming belongs to explicit host installation and deployment. | [spendguard.consumer_audit:audit_consumer_workspaces](#core-ee933086ba7445ad6825aa20577ae73b6a6d9e69fc1554ab7778ff5519ae8f08) |
| C036 | Promote a gated release and converge running consumers | deploy | ABSENT (DELIBERATE): Release promotion owns deployment locks and external process supervision. | ABSENT (DELIBERATE): Promoting the serving release requires deployment locks and external process supervision. |  |
| C037 | Compare candidate models and efforts on measured quality | bakeoff | spendguard.bakeoff.bakeoff(intent, candidates=None, prompts=None, sample_n=5, run=False, budget_usd=None, judge_model=None, parent_reading=None, efforts=None, requirement_aware=False, adjudicator_model=None, temperature=None, top_p=None, seed=None) | spendguard_bakeoff(intent, candidates, prompts, sample_n, efforts, requirement_aware, adjudicator_model, temperature, top_p, seed, budget_usd) | [spendguard.bakeoff:bakeoff](#core-0a386423d2156252798bb6307054e9cf35ca1df1e68efd1d5fd5817741f4b3e8) |
| C038 | Measure output stability by repeated execution | stability | spendguard.stability.measure_stability(prompts, model, k=DEFAULT_REPLICATES, intent=None, system=None, parse=None, reasoning=None) | spendguard_stability(model, intent, prompts, sample_n, k, marker, system) | [spendguard.stability:measure_stability](#core-b1acb3bb101b246854411302a62735567d4ca5a7052f6518982bdbdb91f4a559) |
| C039 | Find the cheapest reasoning effort preserving measured quality | effort-titrate | spendguard.effort_titration.titrate(intent, model=None, efforts=None, sample_n=_MAX_SAMPLE, run=False, budget_usd=None, judge_model=None, requirement_aware=False, adjudicator_model=None) | capability_effort_titrate_effort_titration_titrate | [spendguard.effort_titration:titrate](#core-27e9e0821dd621c864e00d5cd0ab25e6f6b463affc8511076ecf708a52d17b65) |
| C040 | Check and optionally sync spend metadata backbone | metadata | spendguard.metadata_audit.backbone_health() | capability_metadata_metadata_audit_backbone_health | [spendguard.metadata_audit:backbone_health](#core-5f80f7c6c5c55b65e678da7420f530a6e8908a338c5d88d5f2332c8cb2b561b1) |
| C041 | Rebuild exact-decimal spend events while preserving charge totals | migrate | spendguard.migrate_charges.run_cutover(db_path=None) | ABSENT (DELIBERATE): Rebuilding exact-decimal ledger storage is an operator migration workflow. | [spendguard.migrate_charges:run_cutover](#core-f7cc8c03d8c88dcc006b3cab9e208c48b5e4a7ccf273e3dd0f7c2a2f512f5bb1) |
| C042 | Find and adjudicate ungrounded token estimate literals | estimate-literals | capability_estimate_literals_estimate_literals_review_literal_token_estimates | capability_estimate_literals_estimate_literals_review_literal_token_estimates | [spendguard.estimate_literals:review_literal_token_estimates](#core-a14295255b80d68eec0d244a34c644c57953e253ad89ff2ae1c66a0b9b9f81fc) |
| C043 | Check estimator assumptions against live execution paths | estimate-divergence | spendguard.estimate_divergence.enforce(raise_on_fail=True, model=None) | capability_estimate_divergence_estimate_divergence_enforce | [spendguard.estimate_divergence:enforce](#core-3c196c985d979e312b156d47ff947499babeae52f187641a291c5e4cbc892b01) |
| C044 | Read available model prices and price verification | pricing | capability_pricing_pricing_main | capability_pricing_pricing_main | [spendguard.pricing:main](#core-78584dd26c132b4fc7a2312e70f401fe8ae75e5ab8d272af8999ee1f060e3763) |
| C045 | List configured priced providers | providers | spendguard.pricing.providers() | capability_providers_pricing_providers | [spendguard.pricing:providers](#core-9ec1948e817480a8c1d513055d84debe5e0a1edc51da5cd62d78a8bc7128594d) |
| C046 | Audit per-venv gate capture coverage | coverage | spendguard.coverage.audit_gating_coverage(roots=None) | capability_coverage_coverage_audit_gating_coverage | [spendguard.coverage:audit_gating_coverage](#core-a570c02e8bcb02050257f2641b80c6fd63fc53838f59964bf69f3906c95f8878) |
| C047 | Audit enforcement across discovered interpreters | gate-coverage | spendguard.setup.coverage(extra=None) | capability_gate_coverage_setup_coverage | [spendguard.setup:coverage](#core-47d7e6b4683dd01ad67105d6a7b01f03b6584df0cf64eb6790d0b8b317fef27a) |
| C048 | Recommend measured max_tokens bound for a call class | maxtokens | spendguard.maxtokens; governing inputs: sig,current_max,model | capability_api_contract_31_spendguard_maxtokens | [spendguard:maxtokens](#core-f2a56626b234885ed491f5e895fbbbc6b3d9aefde96754571595ebab679eedc6) |
| C049 | Interactively initialize and connect a local installation | init | ABSENT (DELIBERATE): Interactive onboarding owns terminal prompts and browser consent. | ABSENT (DELIBERATE): Interactive onboarding owns terminal prompts and browser consent. |  |
| C050 | Install or remove enforcement in a chosen interpreter | install-hook | spendguard.setup.install_hook(venv=None, uninstall=False, install_pkg=True, user=False, python=None) | ABSENT (DELIBERATE): Interpreter startup hook installation precedes tool execution in that environment. | [spendguard.setup:install_hook](#core-4169f7db5fb119327ad3df05d32a361253df4b86c9bf9aabc0c9ebd499b261a6) |
| C051 | Discover and optionally arm ungated host interpreters | gate-host | ABSENT (DELIBERATE): Host interpreter discovery and arming belongs to installation lifecycle. | ABSENT (DELIBERATE): Arming discovered host interpreters belongs to explicit installation lifecycle. |  |
| C052 | Install or remove the agent-spawn gate | install-agent-gate | spendguard.agent_spawn_gate.install_agent_gate(settings_path=None, uninstall=False, executable=None) | ABSENT (DELIBERATE): Installing assistant spawn hooks changes client host configuration. | [spendguard.agent_spawn_gate:install_agent_gate](#core-32a35d127dfac3137d94292ff042230753e4b5f62945c92053279ea0abea54b1) |
| C053 | Install or remove the weekly-cap hook | install-capguard | spendguard.weekly_cap_guard.install_weekly_cap_guard(settings_path=None, uninstall=False, executable=None) | ABSENT (DELIBERATE): Installing assistant cap hooks changes client host configuration. | [spendguard.weekly_cap_guard:install_weekly_cap_guard](#core-7b8735c1796f23f5b8146b42260771813a25024bf7469edbcf27e61975dcbaf5) |
| C054 | Install or remove scheduled spend reports | schedule | spendguard.schedule.install_schedule(interval='hourly', remove=False) | ABSENT (DELIBERATE): Installing scheduled reports requires host scheduler ownership. | [spendguard.schedule:install_schedule](#core-c6be327575f65cb0a2a40e21dbbcbda731ee5bb778b5b0de695f39b50fd418d5) |
| C055 | Deploy assistant skills into a destination | install-skills | spendguard.setup.install_skills(dest=None) | ABSENT (DELIBERATE): Deploying assistant skill files belongs to host setup. | [spendguard.setup:install_skills](#core-3a242780fcf1ad323ba13bdbe18ecac6f3ce2f6d97b0d0ce90e7439fbf4febc8) |
| C056 | Install assistant governance rules into project or global files | install-rule | spendguard.setup.install_rule(target=None, glob_=False) | ABSENT (DELIBERATE): Installing assistant governance files belongs to host setup. | [spendguard.setup:install_rule](#core-3a1ef21e6048434a64600f0fd3e8207b91727ecfcc61cc708fc43a957ad4dbb3) |
| C057 | Install or remove host receipt integrations | install-receipts | ABSENT (DELIBERATE): Shell and assistant receipt installation belongs to host configuration lifecycle. | ABSENT (DELIBERATE): Installing shell and assistant receipt integrations belongs to host setup. |  |
| C058 | Register or remove this MCP server in a client | install-mcp | spendguard.mcp_server.register_client(remove=False) | ABSENT (DELIBERATE): Registering this transport requires client configuration before tools are reachable. | [spendguard.mcp_server:register_client](#core-55a92f3c43f9f3700470fea7650728885a83cf67950e6999794380ef36917377) |
| C059 | Move project attribution or estimate semantic reattribution | tag | spendguard.tag.move_project(old, new); spendguard.tag.estimate_llm_retag() | capability_tag_tag_move_project; capability_tag_tag_estimate_llm_retag | [spendguard.tag:move_project](#core-c66552e492e2a2bee7f156517b79fc7872516b440a8b37d2ef3a685427979521); [spendguard.tag:estimate_llm_retag](#core-4413b4ff5e539e30cc97beb6e33bac73757931e81fb4dcf1035aefca628fbd06) |
| C060 | Summarize recorded calls by intent | calls | spendguard.calls.cost_summary(intent=None) | capability_calls_calls_cost_summary | [spendguard.calls:cost_summary](#core-ff8fcc845d5b8f6400dab80e066583b4e7e801effe6da5281f6de5f1c1c916d6) |
| C061 | Backfill provider batch usage into historical run records | backfill | spendguard.backfill.backfill(intent_map=None, providers=('openai', 'anthropic')) | capability_backfill_backfill_backfill | [spendguard.backfill:backfill](#core-e7b3e0ec183e79e320a0a194baf0c3dcec9b6f6799c982378f2f52acb27940a9) |
| C062 | Rank cheapest configurations that held quality for an intent | advise | spendguard.advise.ranked(intent=None, as_of=None, by_effort=False) | spendguard_advise(intent, plan, as_of) | [spendguard.advise:ranked](#core-b5bfe0fbc9438ce6c08f2548bac4f8d1c63171c01fe17ba50a498055cfc1f0a6) |
| C063 | Optimize an intent using recorded evidence | optimize | spendguard.advisor.optimize(intent=None, plan=None, run=False) | capability_optimize_advisor_optimize | [spendguard.advisor:optimize](#core-e67f73c0e429d4ccef8d0923aa934cdc75a98501502e8331734f603875aa7358) |
| C064 | Mine quality and cost learnings from recorded runs | mine | spendguard.advisor.mine(run=False, intent=None) | capability_mine_advisor_mine | [spendguard.advisor:mine](#core-c40a37e5989fa4c4746161ddb687ebc7ecf5681c614f5696ce3c8c5aea11f4d9) |
| C065 | Reconstruct advisor history from stored runs | reconstruct | spendguard.advisor.reconstruct_advisor_history(run=False, per=15, limit=None) | capability_reconstruct_advisor_reconstruct_advisor_history | [spendguard.advisor:reconstruct_advisor_history](#core-d9e1b853512a82afa78b40431bcd147dd11ca22c435da133f66b929f044bec20) |
| C066 | Recommend cheaper model candidates for an intent | recommend | spendguard.advisor.recommend_models(intent=None, k=5, quality_bar=None, run=False) | spendguard_recommend(intent, k, quality_bar, budget_usd) | [spendguard.advisor:recommend_models](#core-a17c133ed591f37bb800091483a2a96a14fe2e3013f48724b37a39353cadb617) |
| C067 | Attribute provider usage to projects from conversation segments | accounting | spendguard.conv.attribute_usage(since='2026-06-01', tdir=None); spendguard.conv.attribute_segments(tdir=None, run=False, recls=False, spend_only=True, tau=_SEG_TAU) | capability_accounting_conv_attribute_usage; capability_accounting_conv_attribute_segments | [spendguard.conv:attribute_usage](#core-dba9ce92a60a76a4aaae6aeecda30510bf724ee45c9afcd8a99a32e32881e6a5); [spendguard.conv:attribute_segments](#core-29c8df38b24cd50784657618f693bc4807cfd55bcb3a93465a88840d936e4a37) |
| C068 | Build and optionally push organization opportunity signals | signal | spendguard.signal.push_signal(dry=False); spendguard.signal.build_signals(since=None) | capability_signal_signal_push_signal; capability_signal_signal_build_signals | [spendguard.signal:push_signal](#core-da3755bd7630c6bc9be819b67c710d4d2630fed507e025e94a980371b7ad1ba8); [spendguard.signal:build_signals](#core-c9abf9fa868b78a61f44eeb1b113d38f84c1a97c25f7254bc86f6affb0ce2832) |
| C069 | Summarize and roll up completed work with optional organization push | workdone | spendguard.workdone.summarize(since=None, run=False, model=None); spendguard.workdone.rollup_workdone(since=None, by='day'); spendguard.saas.push_workdone(since=None, by='month', dry=False) | capability_workdone_workdone_summarize; capability_workdone_workdone_rollup_workdone; capability_workdone_saas_push_workdone | [spendguard.workdone:summarize](#core-e59bc32f9ae4540a46d0e05886cb8ce4ca5932fb80adef0de15aed77c1c04e9a); [spendguard.workdone:rollup_workdone](#core-1467dc4a4c4ccec2d7a4e338c9f441086c88a5fc44df28bf57090e2f7c21a632); [spendguard.saas:push_workdone](#core-bced83578c706735ba276bb332cf29b745bc1d9736ada143b7255df6a200a2ba) |
| C070 | Record a sourced price override for one model | price | spendguard.pricing.set_price(model, provider, in_usd, out_usd, source, batch_in=None, batch_out=None, cached_in=None, verified=None) | capability_price_pricing_set_price | [spendguard.pricing:set_price](#core-8165f9ee2b860ac9dc79631d403f9fb71214d6d9f7879b53b797bbf7ec0cdf6e) |
| C071 | Revalue previously unpriced ledger records | reprice | spendguard.budget.reprice_unpriced(model=None, apply=False, actor='reprice') | capability_reprice_budget_reprice_unpriced | [spendguard.budget:reprice_unpriced](#core-a32b2761c6af674017384df0af14a08e52ca728b74a689d3c087ffd1e6dd0da6) |
| C072 | Revalue and synchronize subscription-lane usage | lanevalue | spendguard.lane_value.stamp_from_ledger(window_days: int=_WINDOW_DAYS); spendguard.lane_value.sync(dry=False) | capability_lanevalue_lane_value_stamp_from_ledger; capability_lanevalue_lane_value_sync | [spendguard.lane_value:stamp_from_ledger](#core-5e97e6bff77bafa5edbe4be73ce29903ce640152bff79463014aa58b2aaca918); [spendguard.lane_value:sync](#core-7d2f4e35c79636a1c63722e3fb431709173a3bb901194fed5491c4eb529c275a) |
| C073 | Read available replayable corpus coverage | callio-status | spendguard.callio.status_rows(intents=None) | capability_callio_status_callio_status_rows | [spendguard.callio:status_rows](#core-b9b23e491c9e01b062cb1215bef1787a3bd8aeb423939f584a03e01c2148c97f) |
| C074 | Inspect and optionally prune stale Codex shell snapshots | codex-gc | spendguard.codex_exec.gc_shell_snapshots(max_age_days=7, apply=False) | ABSENT (DELIBERATE): Pruning local Codex shell snapshots belongs to host storage maintenance. | [spendguard.codex_exec:gc_shell_snapshots](#core-286e9aa4c23193ed1be23d025baf4e7d90aef24d7ed72b431a280f6c906d7a31) |
| C075 | Inspect and garbage collect embedding checkpoints | embed-gc | spendguard.adapters.gc_embed_checkpoints(max_age_days=None, max_total_gb=None, apply=False, keep_path=None, include_legacy_top_level=True); governing inputs: max_age_days,max_total_gb,apply,keep_path,include_legacy_top_level | ABSENT (DELIBERATE): Local cache maintenance belongs where files live; remote clients need not delete checkpoints. | [spendguard.adapters:gc_embed_checkpoints](#core-61668d561b03727d4e3e35fe99cec36525d4a671fdbc2d38ab790fb532f308eb) |
| C076 | Review the highest-cost optimization opportunities | review | spendguard.review.review(run=False, top=10) | capability_review_review_review | [spendguard.review:review](#core-296fff9bbaf0df12bcdccc3de486768f6f1bd90b7a17da5e3f9075682ef14bd5) |
| C077 | Audit repository prompt-cache opportunities | cache-audit | spendguard.cacheaudit.audit_cache_opportunities(repo=None) | capability_cache_audit_cacheaudit_audit_cache_opportunities | [spendguard.cacheaudit:audit_cache_opportunities](#core-433da6e40256bb6ce6a7321a3a95a78d5556bd9c1aed3a57b266d74cbe11cef2) |
| C078 | Measure prompt-cache behavior on a script or stored intent | cache-test | spendguard.cachetest.cache_test(system, users, model=None, run=False) | capability_cache_test_cachetest_cache_test | [spendguard.cachetest:cache_test](#core-cacc7c885585b768e262aa1bd88d9a8af5cb64d1b3f6746df3556463c59b43ef) |
| C079 | Build a token-efficient task brief | brief | spendguard.brief.brief(task, intent=None, run_llm=False) | capability_brief_brief_brief | [spendguard.brief:brief](#core-cd9e70ee7c4814e749cc50e5700383f4070a51f29048fd960f86c8b0417c2e13) |
| C080 | A/B test cheaper model and prompt variants on stored samples | experiment | spendguard.experiment.experiment(intent, models=None, instructions=None, n=20, run=False, reconsider=False, mode='auto') | capability_experiment_experiment_experiment | [spendguard.experiment:experiment](#core-d5a99048eb6eca05285e4dbc9a5499f3d6b2631509b259160d0e0c7d80bd48ae) |
| C081 | Read model profiles and learned capability facts | models | spendguard.models.add_fact(model, key, value, confidence=0.9, source='manual', verified=True); spendguard.models.clear_fact(model, key); spendguard.models.facts(model); spendguard.models.profile(model) | capability_models_models_add_fact; capability_models_models_clear_fact; capability_models_models_facts; capability_models_models_profile | [spendguard.models:add_fact](#core-72f469725265bb5d296e87a2e1345c0c2678a53a6e32cb6c254f81fcef964baa); [spendguard.models:clear_fact](#core-4d61408f35787546e435139f01903fc2671c6fe9703b1e0bc52b8bbdeae01320); [spendguard.models:facts](#core-4bbf09490ba58768da16cf794a6b89dc396aace323e8baf1bc9fabaa60d5da29); [spendguard.models:profile](#core-5a5acc2d52c9db8d4d48ce750ce5dfd5eacfb20de58d5ba24304427aaf27a5cb) |
| C082 | Run an adopted winning configuration on production inputs | promote | spendguard.experiment.promote(intent, model, instruction='', input=None, out=None, n=None, run=False, batch=False) | capability_promote_experiment_promote | [spendguard.experiment:promote](#core-c3c635086527a63123a21c0000fb302716963e821d0ca3d77a41c869ef739a47) |
| C083 | Read persistent semantic-cache statistics | cache-stats | spendguard.semcache.stats() | capability_cache_stats_semcache_stats | [spendguard.semcache:stats](#core-b46715adf6600b630b45758b2d16ee628268430200b603a911590fa7ac30b2f4) |
| C084 | Deduplicate JSONL requests against persistent cached outputs | dedup | spendguard.semcache.dedup_jsonl(input_path, out_path, model=_SCOPE_ANY, map_path=None) | ABSENT (DELIBERATE): Rewriting local batch JSONL files is a file-preparation workflow. | [spendguard.semcache:dedup_jsonl](#core-ba29c5f8d123dbbd6c6c5601e126769e181657f82dccba9b0d8e836673ac0ab7) |
| C085 | Populate persistent cache from batch request/result files | dedup-populate | spendguard.semcache.populate_jsonl(input_path, results_path, model=_SCOPE_ANY) | ABSENT (DELIBERATE): Importing paired local batch files is a cache-maintenance workflow. | [spendguard.semcache:populate_jsonl](#core-c2cc0f3850836b9329b684185701435dafc85cd9d79f296e5dc703cfdaeb81a7) |
| C086 | Escalate a prompt through a verified model ladder | cascade | spendguard.cascade.cascade(prompt, ladder, verify=None, intent=None, _caller=None) | capability_cascade_cascade_cascade | [spendguard.cascade:cascade](#core-d1f1430b4b40ae43360370c6cb3729c5405c102e811d6aa21c14cc1e9a7a96a6) |
| C087 | Reconcile provider batch truth into the local ledger | reconcile-ledger | spendguard.ledger_sync.sync(since=None) | capability_reconcile_ledger_ledger_sync_sync | [spendguard.ledger_sync:sync](#core-37d9901b1f114021d51f5ea71f507bc2b00ac2fd3aaafb41c78d48887210c6ee) |
| C088 | Read a provider/local/server accounting trust verdict | trust | spendguard.trust.trust_report(since=None, with_server=True) | capability_trust_trust_trust_report | [spendguard.trust:trust_report](#core-52fe78df70e11466d6e34c0b500b3739acf924ebae51f41e07fb83bd0f193190) |
| C089 | Compare recorded model prices with provider catalog prices | cross-check | spendguard.pricing.cross_check_openrouter(run=False, scope=None) | capability_cross_check_pricing_cross_check_openrouter | [spendguard.pricing:cross_check_openrouter](#core-8b3ec2ebe70fca1529f4b14d6787c943eb5b5588ce61d460616fc51406a59a2e) |
| C090 | Bootstrap advisor evidence from repository and transcripts | bootstrap | spendguard.bootstrap.bootstrap(repo=None, transcripts=None, run=False, cap=50) | capability_bootstrap_bootstrap_bootstrap | [spendguard.bootstrap:bootstrap](#core-b6cb85c431fbc089694468330259d25681e6f54390f4101e99d9b71e2f7e9f45) |
| C091 | Validate the historical advisor against stored evidence | validate | spendguard.validate.validate(verbose=True) | capability_validate_validate_validate | [spendguard.validate:validate](#core-d7aededd48b7e4be54ae90907288c404511e3b6c15c85c35d55cfcdbd53c7b24) |
| C092 | Measure adopted-insight savings and optionally publish guarded deltas | realized | spendguard.realized.measure(intent=None, min_each=MIN_EACH); spendguard.realized.sync_to_guarded(rows=None) | capability_realized_realized_measure; capability_realized_realized_sync_to_guarded | [spendguard.realized:measure](#core-20358bc26412c54dfc04b25bb456afb9f03bb558af38b001fed1172984a9dcca); [spendguard.realized:sync_to_guarded](#core-52d49445a95739e6e14a81aed9c8e9a01741cb5919500e7f72889cbd15eb5166) |
| C093 | Read measured savings, decision and shedding summaries | savings | spendguard.guard.saved_since(since=None); spendguard.guard.decisions_summary(since=None, intent_limit=None); spendguard.guard.shed_summary(since=None) | spendguard_savings() | [spendguard.guard:saved_since](#core-276c0cde09dc2bc98b03ff0d572343051feff1447f0bb22e56a85665b9ea2a8d); [spendguard.guard:decisions_summary](#core-09f864bb727cece42a26a81cb08f84b4728f6b416b25b9b10011fe39cadc8a20); [spendguard.guard:shed_summary](#core-4148b398c6c6c0607f24cb02d758270dbb7dd9cecacb1f4b8bafdf514a9494cb) |
| C094 | Find prompt waste and optionally judge batchability | prompts | spendguard.prompts.lint(intent=None, since=None, min_calls=MIN_CALLS, judge_batchable=False) | capability_prompts_prompts_lint | [spendguard.prompts:lint](#core-ec4078f7cbb4c682715cba32a22e5e0645c5ac88cd996483a022776ac4c29634) |
| C095 | Read and optionally push provider-truth daily totals | truth | spendguard.truth.rows(since=None); spendguard.truth.push_truth(since=None, dry=False, truth_rows=None) | capability_truth_truth_rows; capability_truth_truth_push_truth | [spendguard.truth:rows](#core-4e3153b7a00383bd4f2f78881a19de7269632d2ae73a910d987450da60822fb6); [spendguard.truth:push_truth](#core-eb9fb14f8850a21cb4556f0512d545c0eb561f4691e1b880c9f2fbdc468d30b0) |
| C096 | Discover local spend and transcript sources | sources | spendguard.sources.discover_sources(days=None) | capability_sources_sources_discover_sources | [spendguard.sources:discover_sources](#core-b7b829b42a30bffb565c1bb98ddc63c88756419d635b45e4ba1f373cedeeedf5) |
| C097 | Summarize discovered session sources and usage | scan | spendguard.scan.collect(days=None) | capability_scan_scan_collect | [spendguard.scan:collect](#core-3dd568a724d46665511eefb11f79f5b9a484b8c8ee44c07f77796b85d4c450c4) |
| C098 | Run an arbitrary child command with gate bootstrap injected | run | ABSENT (DELIBERATE): Arbitrary child process launch is the invoking shell's contract. | ABSENT (DELIBERATE): Arbitrary child command execution belongs to the invoking host shell. |  |
| C099 | Inspect, set and probe lane capability tier assignments | tiers | spendguard.tier_config.tier_config_report(); spendguard.tier_config.reachability_probe(save=True) | capability_tiers_tier_config_tier_config_report; capability_tiers_tier_config_reachability_probe | [spendguard.tier_config:tier_config_report](#core-8716c532d16cc8478e93dc0478979cb0bc8cecd70b46567a0b63b1959a864553); [spendguard.tier_config:reachability_probe](#core-a78130fd5d3c65d0510c9036010c2ee03ced5e5bcc71107c831bcb48541b0cf7) |
| C100 | Register critical intents that must not be substituted | register-critical | spendguard.lane_balance.register_critical(patterns, source=None); spendguard.lane_balance.bandit_list_coverage() | capability_register_critical_lane_balance_register_critical; capability_register_critical_lane_balance_bandit_list_coverage | [spendguard.lane_balance:register_critical](#core-37c4c731f0144a74954d208ffa637875b991ae2b96585ec326f4ae72569339ad); [spendguard.lane_balance:bandit_list_coverage](#core-7e13577e7cf518fb23a200d3e3b03f5e18e8334c07185ca3455f9fd9043664ec) |
| C101 | Summarize workload spend by provider key fingerprint | keys | spendguard.budget.by_key(since=None) | capability_keys_budget_by_key | [spendguard.budget:by_key](#core-7c40f8353d3f358d9d6d9a6450bb4e8b660c1e5c7f773457428908418693869b) |
| C102 | Compare actual model answers for a supplied prompt | compare | spendguard.adapters.call(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None, sig=None, intent=None, retries=2, files=None, _no_guard=False, no_metered_fallback=False, images=None, no_substitution=False, measurement=False, metered_only=False, base_fallback=None, _probe=False, _route=True, _internal_pin=False, _coalesce_eligible=True, temperature=None, top_p=None, seed=None, soft_deadline_s=None, auth_over_quota=False, decomposition=None, **aliases) | capability_compare_adapters_call | [spendguard.adapters:call](#core-a03c9815845a6ed20a964a37c103c6b63fe691be8ac9e6050aa38130f17c9db5) |
| C103 | Synchronize model price records | sync-prices | spendguard.sync.sync() | capability_sync_prices_sync_sync | [spendguard.sync:sync](#core-f185508c09966120ac63b09dc26e5ebf8cd30a74c273af53b09feb41f5aea688) |
| C104 | Synchronize served model identifiers from providers | sync-catalog | spendguard.catalog.pull_live_catalog(providers=None, timeout_s=20) | capability_sync_catalog_catalog_pull_live_catalog | [spendguard.catalog:pull_live_catalog](#core-3e64d2cfd9c646d6b699458744435a73e0de3cf1a1c4f6ba0bda82092561e82d) |
| C105 | Synchronize and audit model capability bounds | sync-capabilities | spendguard.sync_capabilities.sync_capabilities(dry_run=False) | capability_sync_capabilities_sync_capabilities_sync_capabilities | [spendguard.sync_capabilities:sync_capabilities](#core-b1f8055a1292e26e60da46aaa6b58218eaebdf28ed41e75358a12e6fff49b9f6) |
| C106 | Read cached or refreshed provider balances | balances | spendguard.balances.all_balances(refresh=False); spendguard.balances.refresh_balances(vendors=None) | capability_balances_balances_all_balances; capability_balances_balances_refresh_balances | [spendguard.balances:all_balances](#core-72a6b6e21315b4fe9774ac9699ce45b256f22111f798380ccb8c40cb171d95da); [spendguard.balances:refresh_balances](#core-73ce6edb6b10c3c98a01f1444af743e0732d056374df12f30baf70f22ca0e098) |
| C107 | Probe lane/provider reachability with optional remediation | reliability | spendguard.reliability.sweep(run=False, timeout_s=20); spendguard.reliability.remediate(sweep_result, model=None) | capability_reliability_reliability_sweep; capability_reliability_reliability_remediate | [spendguard.reliability:sweep](#core-f90a7adf4fa593e11fc57b73b62464c3739031cb0e75acd8f452f3f46840af9f); [spendguard.reliability:remediate](#core-ff68b50581fddd0031141957736e71cdb2940f4f7ebd8bf83d1fea30ea15622a) |
| C108 | Read overall lane/provider health and diagnosis | health | spendguard.reliability.sweep(run=False, timeout_s=20); spendguard.reliability.remediate(sweep_result, model=None) | spendguard_health(run, remediate, timeout_s) | [spendguard.reliability:sweep](#core-f90a7adf4fa593e11fc57b73b62464c3739031cb0e75acd8f452f3f46840af9f); [spendguard.reliability:remediate](#core-ff68b50581fddd0031141957736e71cdb2940f4f7ebd8bf83d1fea30ea15622a) |
| C109 | Research and optionally write changed model prices | refresh-prices | capability_refresh_prices_refresh_refresh_verified_catalog_prices | capability_refresh_prices_refresh_refresh_verified_catalog_prices | [spendguard.refresh:refresh_verified_catalog_prices](#core-d86705726b02fe02ca859b52bd99415b61a2908135f4cace85eafa89fa457286) |
| C110 | Check stale model-price evidence | check-prices | spendguard.pricing.freshness(today=None) | capability_check_prices_pricing_freshness | [spendguard.pricing:freshness](#core-abfa7c8e78dafcb0985002e7811f19bc59f7f0c95b51b43f467791ca3b22a3e9) |
| C111 | Predict calibrated cost for a labeled workload | calibrate | spendguard.calibrate.predict_cost(label, n=1, model=None, transport='batch', est_in_tokens=None, est_out_max=None, as_of=None) | capability_calibrate__1_calibrate_predict_cost | [spendguard.calibrate:predict_cost](#core-472d9c47e3796a9c3c592a3bc5452700891aefeb6aa3e10c6a9f2593d6af26c4) |
| C112 | Read calibration cell evidence | calibrate | spendguard.calibrate.show(label=None) | capability_calibrate__2_calibrate_show | [spendguard.calibrate:show](#core-58f1db2228460c48a6f5ce5814264a2223f46f6f0549a1659fd72bed4438a619) |
| C113 | Pair recorded predictions with realized costs | calibrate | spendguard.calibrate.pair(now=None) | capability_calibrate__3_calibrate_pair | [spendguard.calibrate:pair](#core-f8dccbb7bf58302418c4907a6a169e5bfaa19622a5d59fac1d4f4b1e6b69f7ab) |
| C114 | Push shared calibration evidence | calibrate | spendguard.calibrate.push_shared() | capability_calibrate__4_calibrate_push_shared | [spendguard.calibrate:push_shared](#core-98486b3d303a93551f31063bc3a903d45894fd819cf4b97516a2afe2a751f27f) |
| C115 | Fetch shared calibration evidence | calibrate | spendguard.calibrate.fetch_shared() | capability_calibrate__5_calibrate_fetch_shared | [spendguard.calibrate:fetch_shared](#core-e84bfce560d1b757ab84e26e5ba75422b20d5ac3fa545a7790d3cd07e0f7a581) |
| C116 | Backtest calibration against historical predictions | calibrate | spendguard.calibrate.backtest(as_json=False, min_cell=MIN_CELL_BACKTEST) | capability_calibrate__6_calibrate_backtest | [spendguard.calibrate:backtest](#core-d03047976b8a014f07d77d9ea3c437d97afd0d408220a83418e0bdf5d633c246) |
| C117 | Inspect one recorded quality measurement | measurement | spendguard.measurement.inspect(reading_id) | capability_measurement__1_measurement_inspect | [spendguard.measurement:inspect](#core-eee384b60fd4218e6afe19313b8456dcbba0de0be60c485baa328018039dbd31) |
| C118 | List quality measurements for an intent | measurement | spendguard.measurement.list_readings(intent=None, limit=25) | capability_measurement__2_measurement_list_readings | [spendguard.measurement:list_readings](#core-376c86bbaadf06591fc00bebb0aa553e4823518e4f84c505395c9925d977421e) |
| C119 | Reconstruct a quality measurement from stored results | measurement | spendguard.measurement.reconstruct_reading(intent) | capability_measurement__3_measurement_reconstruct_reading | [spendguard.measurement:reconstruct_reading](#core-8eee5362e3659eb54329ef61c17a23f031add8119b5bb0ad3c87a04d390fb4f5) |
| C120 | Rerun a measurement with controlled sample freshness and budget | measurement | spendguard.measurement.rerun(reading_id, *, budget_usd=None, same_sample=True) | capability_measurement__4_measurement_rerun | [spendguard.measurement:rerun](#core-f9c8b4c7962886ca1c988ad06d67f39f170a3b39a7ed876530abcb9e94b84248) |
| C121 | Compare two recorded quality measurements | measurement | spendguard.measurement.compare(a_id, b_id) | capability_measurement__5_measurement_compare | [spendguard.measurement:compare](#core-3194de0820d507046e1d01e515ae23d15061817e9132824218d14bf082879b07) |
| C122 | Read organization connection readiness | saas | spendguard.saas.ready() | capability_saas__1_saas_ready | [spendguard.saas:ready](#core-521ae65689bc498f3deb3695333f42cce4c52af7003a21da6a43b9b78c160300) |
| C123 | Test organization server liveness | saas | spendguard.saas.ping() | capability_saas__2_saas_ping | [spendguard.saas:ping](#core-3820509460b1850692de02af2f928a7e1a7b102c0c33a220214707f09ed06e8e) |
| C124 | Push spend, insights and completed-work rollups to the organization | saas | spendguard.saas.sync(if_due=False, since=None) | capability_saas__3_saas_sync | [spendguard.saas:sync](#core-b674645088ae831f263e5b90490ae72bb001fba25884b0b8eda7b69c0e6991c7) |
| C125 | Push organization spend rollup with an optional dry preview | saas | spendguard.saas.push_rollup(since=None, dry=False) | capability_saas__4_saas_push_rollup | [spendguard.saas:push_rollup](#core-005d079603e4e8649215dd120f839ba1193128e6938b32904d862e36d0aaa6bc) |
| C126 | Reconcile provider batch truth into organization-facing local ledger | saas | spendguard.ledger_sync.reconcile_into_ledger(since=None) | capability_saas__5_ledger_sync_reconcile_into_ledger | [spendguard.ledger_sync:reconcile_into_ledger](#core-3b294fad2d72933749669cebe8c7015e385bb34bce9437265355849572e0e1e7) |
| C127 | Repair historically misassigned provider attribution | saas | spendguard.budget.reattribute_providers(apply=False, actor='reattribute_providers') | capability_saas__6_budget_reattribute_providers | [spendguard.budget:reattribute_providers](#core-12507e4a027ddb6af31bc106bc1a7107de12de4d0c4b3dc7f761f7e22cbaf633) |
| C128 | Audit completeness of provider batch usage capture | saas | spendguard.ledger_sync.audit_completeness() | capability_saas__7_ledger_sync_audit_completeness | [spendguard.ledger_sync:audit_completeness](#core-5f041357820d1ca3ab2fdc26a8f9d5f21340af1d9f909730e9e78b77593946e3) |
| C129 | Compare local actual-dollar ledger with organization server rows | saas | spendguard.saas.crosscheck(since=None) | capability_saas__8_saas_crosscheck | [spendguard.saas:crosscheck](#core-2413114d0e799845d40365402eaa4fb76162d262959d39e4dec529a3a2c2e33f) |
| C130 | Link this installation to an organization using device consent | saas | spendguard.saas.link(open_browser=True, timeout=900) | ABSENT (DELIBERATE): Organization device linking requires interactive member consent in the local installation. | [spendguard.saas:link](#core-508d17187b4e4d1c203f94257048892156621c93ec2586b5c0564e94273ec8ce) |
| C131 | Run organization-requested accounting operations locally | saas | spendguard.saas.run_commands(since=None) | capability_saas__10_saas_run_commands | [spendguard.saas:run_commands](#core-0311663dc7d7278026a60d4743b0a03e4e6c33c1fe868321697ef15d7def5473) |
| C132 | Pull scrubbed organization learnings as low-trust priors | saas | spendguard.saas.pull_insights(scope='team') | capability_saas__11_saas_pull_insights | [spendguard.saas:pull_insights](#core-246135974cd517e9fe3c037923355c436032d895f926dc6bd75e517214ad7070) |
| C133 | Capture live GPU instance history before instance destruction | resources | spendguard.resources.record_instance_history() | capability_resources__1_resources_record_instance_history | [spendguard.resources:record_instance_history](#core-12184a326aa90fce9a036286fdfa59b249b1bc474e8a546a1304eb8381c5e966) |
| C134 | Synchronize attributed GPU consumption with organization accounting | resources | spendguard.resources.sync(dry=False) | capability_resources__2_resources_sync | [spendguard.resources:sync](#core-ad5a4cf6937909ba18638ece2a431e169df982255106ce40f0c683566eaaa9bd) |
| C135 | Recover GPU lifecycles from transcripts with estimate-first adjudication | resources | spendguard.resources.discover_agentic(run=False, record=False, max_sessions=None, now=None) | capability_resources__3_resources_discover_agentic | [spendguard.resources:discover_agentic](#core-c290680b6e493307ac0115c2ea8fafc652fd5314ebed68da80928303efb78c43) |
| C136 | Recover GPU instance records from recorded provider data | resources | spendguard.resources.discover_instances(record=False, now=None) | capability_resources__4_resources_discover_instances | [spendguard.resources:discover_instances](#core-a47190c88ccbe61edc8819fe4ee971300ed3d3bf61a18c413f66749cfcb691e5) |
| C137 | Read GPU consumption and account residual reconciliation | resources | spendguard.resources.vast_gpu_accounting_rows_by_day(since_ts=None, now=None, label_map=None); spendguard.resources.account_gpu_total(since_ts=None) | capability_resources__5_resources_vast_gpu_accounting_rows_by_day; capability_resources__5_resources_account_gpu_total | [spendguard.resources:vast_gpu_accounting_rows_by_day](#core-03af0892b06aedf0e05d8fcfba8f9d7bbaa20babb6c3cf36be8b6d6a66681f34); [spendguard.resources:account_gpu_total](#core-4c67532231aba94b45c36de6c23861f2955308f72a749489d49eece450b39c5c) |
| C138 | Generate a remote host bootstrap snippet | remote | spendguard.remote.onstart_snippet(home: str=_DEFAULT_HOME, from_git: bool=False) | ABSENT (DELIBERATE): Generating host startup scripts belongs to remote deployment setup. | [spendguard.remote:onstart_snippet](#core-a8159467ebed72ac15f9bcaf23122cddaab4f2c46556c6ea33965e9bf7e04a4a) |
| C139 | Verify enforcement inside a remote interpreter | remote | spendguard.remote.verify_remote_enforcement(ssh: str, timeout: int=30, _run=None) | capability_remote__2_remote_verify_remote_enforcement | [spendguard.remote:verify_remote_enforcement](#core-f9d640307ab5be2c4e3a6282f25dbea2ad3209b3f0c718405a0343d0fb4de537) |
| C140 | Import a remote host ledger with project attribution | remote | spendguard.remote.sync(ssh: str, project: str, label: str=None, home: str=_DEFAULT_HOME, timeout: int=60, _run=None) | capability_remote__3_remote_sync | [spendguard.remote:sync](#core-38bcee45de98175d3c29d27feb70f53d10625d596df0d9d4f8e6e1ed7252fb8e) |
| C141 | Synchronize all configured remote hosts | remote | spendguard.remote.sync_all(dry=False, home: str=_DEFAULT_HOME, _instances=None, _sync=None, _attrib=None) | capability_remote__4_remote_sync_all | [spendguard.remote:sync_all](#core-677160830bd9c596dd627317db085c0efccd88c635bb061acba1da2a512301d4) |
| C142 | Recover historical run intent labels from repository artifacts | mine-history | spendguard.history.reconstruct_intents(repo, apply=False) | capability_mine_history__1_history_reconstruct_intents | [spendguard.history:reconstruct_intents](#core-50c519383b35875eeee66d2331e2c94482cbf0b754bdaf1f2eebcb84c7f8b415) |
| C143 | Enrich historical run and insight graph relationships | mine-history | spendguard.history.enrich_graph() | capability_mine_history__2_history_enrich_graph | [spendguard.history:enrich_graph](#core-c78880e65f8c3404b23e258ae17227180ba2558a63ec4f5edf5f4c87c031fa5e) |
| C144 | Mine quality evidence from repository commit history | mine-history | spendguard.history.mine_git(repo, apply=False, max_commits=400) | capability_mine_history__3_history_mine_git | [spendguard.history:mine_git](#core-9cbbef118f87cd2e3444bdb0045c43b976d481c04f7ea9840a93d050f809915e) |
| C145 | Index transcript evidence for historical runs | mine-conv | spendguard.conv.index_cmd(tdir=None, apply=False, rebuild=False) | capability_mine_conv__1_conv_index_cmd | [spendguard.conv:index_cmd](#core-375b25629b9594d6d293a155faa4f6ae0cbc653ab9bdb726fbc15824ab87cda8) |
| C146 | Synthesize cost lessons from transcript evidence | mine-conv | spendguard.conv.synth(tdir=None, run=False, limit=40) | capability_mine_conv__2_conv_synth | [spendguard.conv:synth](#core-596687b43ad44be8bcb8426454ea5dc8c1aabbb5dd9710370fb70f20b4d4e9ea) |
| C147 | Read effective spendguard configuration | config | capability_config_setup_show_declared_configuration | spendguard_config() | [spendguard.setup:show_declared_configuration](#core-5d23f0a882e35bfd1405cb18f6c7295b51cf73e8aa65a11d32d2a32d4da18235) |
| C148 | Update one effective configuration setting | config | capability_config__2_setup_set_declared_configuration_value | capability_config__2_setup_set_declared_configuration_value | [spendguard.setup:set_declared_configuration_value](#core-1304f9ccb5e8e5adcb9a74ede8f81001e323da97bd79cd8ea4353b731e0dfc82) |
| C149 | Deregister one interpreter from the serving fleet | fleet | spendguard.fleet.deregister_interpreter(python_executable) | ABSENT (DELIBERATE): Interpreter deregistration belongs to host fleet lifecycle ownership. | [spendguard.fleet:deregister_interpreter](#core-28c5e1c4cb6e8e2bf76f2c4d1e5c3b5ed56904cd9e3a9e77c6d29df0da0d1f63) |
| C150 | List registered serving consumers | fleet | spendguard.fleet.running_consumers(prune=True) | capability_fleet__2_fleet_running_consumers | [spendguard.fleet:running_consumers](#core-fe4c9387a10d770272001a14ffd07bde4a526d55666a93d146a776dad512f343) |
| C151 | Inspect or restart selected supervised consumers | fleet | spendguard.fleet.restart_stale(apply=False, consumer_id=None, all_supervised=False) | ABSENT (DELIBERATE): Restarting supervised consumers belongs to external host process supervision. | [spendguard.fleet:restart_stale](#core-09b69dcbd2f4b678c431485c83c989e895508d30bbc61146b4a152dd8f0d04d9) |
| C152 | Stop one supervised consumer | fleet | spendguard.fleet.stop_supervised_consumer(consumer_id, apply=False) | ABSENT (DELIBERATE): Stopping supervised consumers belongs to external host process supervision. | [spendguard.fleet:stop_supervised_consumer](#core-8eeae857b1177d8b3c25e670f78964984534875106c330ced5eb9b845f29421c) |
| C153 | Read fleet freshness and release alignment | fleet | spendguard.fleet.fleet_summary() | capability_fleet__5_fleet_fleet_summary | [spendguard.fleet:fleet_summary](#core-cd1911edba24790145b56a748ab1b86d73a60a24125b3e29adc009fd900562cc) |
| C154 | Build a monthly provider-truth close view | close | spendguard.close.build_close(month) | capability_close__1_close_build_close | [spendguard.close:build_close](#core-a350a430c63f60cbf0ddae9db08b70771c58070731dfae38f71415809bbed5ff) |
| C155 | Export a monthly close statement as CSV | close | spendguard.close.to_csv(stmt); spendguard.close.build_close(month) | ABSENT (DELIBERATE): Writing a monthly-close CSV is an operator file-export workflow. | [spendguard.close:to_csv](#core-918699d20ffd15e30ea3a5cb7ff89a957a927370f557b853cfdeadf30e61a8dd); [spendguard.close:build_close](#core-a350a430c63f60cbf0ddae9db08b70771c58070731dfae38f71415809bbed5ff) |
| C156 | Read shared provider account reconciliation axis | close | spendguard.ledger_sync.leak_line(since=None); spendguard.close.month_window(month) | capability_close__3_ledger_sync_leak_line; capability_close__3_close_month_window | [spendguard.ledger_sync:leak_line](#core-9362401b412b1b805c125045e4ee7d7de9c47aaf0ced3ddfadb9def5512f1657); [spendguard.close:month_window](#core-61b64bd3142812e6d1c7cd900e7c9cce0d613d092b3b21c46109bec455b7dedb) |
| C157 | Delegate a self-contained task to another provider plan | delegate | spendguard.delegate_router.delegate_task(task, files=None, intent=None, provider='auto', execute=False, timeout=None, cwd=None, warm=False); spendguard.delegate_router.start_delegate_job(task, files=None, intent=None, provider='auto', cwd=None, timeout=None, detached=False, warm=False); spendguard.delegate_router.wait_for_delegate_job_handoff(job_id) | spendguard_delegate(task, files, intent, provider, cwd, execute) | [spendguard.delegate_router:delegate_task](#core-68ddabbd5490ea060407af6d2e38f0366a724e8bcf5ad0394a71f0852c521d8d); [spendguard.delegate_router:start_delegate_job](#core-27b0596e32b63a7c3e4de5028aa61d0e8ca760de16235afe09ba3988e43cc00e); [spendguard.delegate_router:wait_for_delegate_job_handoff](#core-23cfa6dd59c35987fb1c680583bd0660eb48994df194155ee711f3a5a92988f6) |
| C158 | Read delegated job progress | delegate | spendguard.delegate_jobs.read_delegate_job(job_id) | spendguard_delegate_status(job_id) | [spendguard.delegate_jobs:read_delegate_job](#core-531c16b0caeca81416ad6014208e8847bad71c05ccaf71428cb7a17e57f4dc8a) |
| C159 | Collect a delegated job result | delegate | spendguard.delegate_jobs.read_delegate_job_result(job_id) | spendguard_delegate_result(job_id) | [spendguard.delegate_jobs:read_delegate_job_result](#core-2cf61e6857bb3198179cbe8eee0415f30de52f1a88fff5df3f7ca826f8cbf66d) |
| C160 | Recover an interrupted delegated execution | delegate | spendguard.execution_journal.recover_durable_execution(execution_id, *, root=None) | capability_delegate__4_execution_journal_recover_durable_execution | [spendguard.execution_journal:recover_durable_execution](#core-4febdb316dd1d47a25609770d714af32c1449c1567a9b8d0f68836de1b3e2705) |
| C161 | Build the distribution surface manifest | manifest | spendguard.manifest.build_surface_manifest(*, roots=None, settings=None, now=None) | capability_manifest__1_manifest_build_surface_manifest | [spendguard.manifest:build_surface_manifest](#core-ea5874a8af3dd6893dfe4cb22bf1e71c6eaaff0c5a8c6229271a54d8a078319d) |
| C162 | Read the persisted distribution surface manifest | manifest | spendguard.manifest.read_surface_manifest() | capability_manifest__2_manifest_read_surface_manifest | [spendguard.manifest:read_surface_manifest](#core-14ef0906be3a20daa5a0c3ff196863e92439006764906946bdad4298b8fd0b49) |
| C163 | List scrubbed local cost learnings | insights | spendguard.share.scrubbed_abstracts(min_conf=0.6, active_only=True) | capability_insights__1_share_scrubbed_abstracts | [spendguard.share:scrubbed_abstracts](#core-abbcea1acc11e030d4f289a2437c1aa25eb4a1a30a735bd4a6bd0d13ca20d422) |
| C164 | Export scrubbed cost learnings to a file | insights | capability_insights__2_share_export_scrubbed_insights | ABSENT (DELIBERATE): Writing scrubbed learnings to exchange files is an operator export workflow. | [spendguard.share:export_scrubbed_insights](#core-4be058be90090e01e2f82cfc04bd153afda0848087bad1e1ad983b04e85b88a7) |
| C165 | Import cost learnings with explicit trust handling | insights | capability_insights__3_share_import_community_insights | capability_insights__3_share_import_community_insights | [spendguard.share:import_community_insights](#core-aeeaecf8023e2c3e145e3d403d05d5fd480ae38a0c0f700677deb6cdf6225d78) |
| C166 | Read coding-session usage and subscription-value report | claude-code | spendguard.claudecode.show_claudecode_report(days=None) | capability_claude_code__1_claudecode_show_claudecode_report | [spendguard.claudecode:show_claudecode_report](#core-c9aa6d1a112a80dc1e2c2b12805268fd38e33878d961bb0a3a7944c40e9e54d8) |
| C167 | Rebuild coding-session usage from local transcripts | claude-code | spendguard.claudecode.update(st=None) | capability_claude_code__2_claudecode_update | [spendguard.claudecode:update](#core-9f365f15bd99e84a5f14e19ce8ae19e93b6b107ac30db2042dc0c3c02c352101) |
| C168 | Synchronize attributed coding-session usage to the organization | claude-code | spendguard.claudecode.sync(dry=False) | capability_claude_code__3_claudecode_sync | [spendguard.claudecode:sync](#core-1f59ee55a88ef04ad79ef1cb56c81e98e416b4863e8cb8c6504dcdabef30501b) |
| C169 | Ingest coding-session events into the spend ledger | claude-code | spendguard.claudecode.ingest_events(days=None, limit=None, reset=False, dry=False) | capability_claude_code__4_claudecode_ingest_events | [spendguard.claudecode:ingest_events](#core-1d9cae7d9cf964ad9905db9e0c3fc933e42e84897649f17cde3eae78fc08cbaf) |
| C170 | Reconcile subscription overflow and billing state | claude-code | spendguard.claudecode.reconcile_billing_state(dry=False) | capability_claude_code__5_claudecode_reconcile_billing_state | [spendguard.claudecode:reconcile_billing_state](#core-f87029799039d1f3b7cb2938512faa85a523981b0c9c6a9126956cf7d772b7f8) |
| C171 | Read measured coding-plan overage tally | claude-code | spendguard.claudecode.overage_tally() | capability_claude_code__6_claudecode_overage_tally | [spendguard.claudecode:overage_tally](#core-2c80db0db80ae022b2f5d3601d32afc3fa84143b54943348bd8cc84419321f58) |
| C172 | Read one conversation context trajectory and residency decomposition | claude-code | spendguard.claudecode.context_trajectory(conv_id) | capability_claude_code__7_claudecode_context_trajectory | [spendguard.claudecode:context_trajectory](#core-2cac4c9e07619f9487de1afe65ba935829d7064f6e620c12c1f35d83c39c2b0e) |
| C173 | Rank conversation costs and compaction opportunities | claude-code | spendguard.ledger_read.read_cc_top_conversations(limit=None, path=None); spendguard.claudecode.attribute_overage(top=12) | spendguard_top_conversations(by, limit) | [spendguard.ledger_read:read_cc_top_conversations](#core-4898a48fcf24a81148d55271f972b8d1b144069ef475e3beff388c188bf0215f); [spendguard.claudecode:attribute_overage](#core-29fbd504a96239601a42d5978a7868b66a95ea5b8ec7f84d70d8ef2468a3d18a) |
| C174 | Ingest coding-plan invoice data | claude-code | spendguard.claudecode.ingest_invoices(csv_path=None, dry=False) | capability_claude_code__9_claudecode_ingest_invoices | [spendguard.claudecode:ingest_invoices](#core-b139ba8238cdfbb4ce69c12b5708ed577a5c958f15ae97b0cc15cb9cdcaf3cef) |
| C175 | Attribute coding-plan overage to conversations | claude-code | spendguard.claudecode.attribute_overage(top=12) | capability_claude_code__10_claudecode_attribute_overage | [spendguard.claudecode:attribute_overage](#core-29fbd504a96239601a42d5978a7868b66a95ea5b8ec7f84d70d8ef2468a3d18a) |
| C176 | Generate a tailored compaction prompt from a conversation | claude-code | spendguard.compaction.agentic_advise(transcript_path, run=False, model=None) | capability_claude_code__11_compaction_agentic_advise | [spendguard.compaction:agentic_advise](#core-9f90499f2fa86d109c568d05e7bbe904a25005b4d5562095ae0cbe99f1a2cb08) |
| C177 | Classify coding-session project and organization attribution | claude-code | spendguard.claudecode.classify(run=False, days=None, recls=False) | capability_claude_code__12_claudecode_classify | [spendguard.claudecode:classify](#core-1ede0e708fc12da7d3a919663754a76f4f1d2071ee80e3fe7ac9ba625821f93b) |
| C178 | Read coding-session completed-work rollup | claude-code | spendguard.claudecode.show_claudecode_work(by='week', days=None) | capability_claude_code__13_claudecode_show_claudecode_work | [spendguard.claudecode:show_claudecode_work](#core-22c0bebff0fd97ac4effe995d15e3617d285c680cdf7116dbe839115c8e8669b) |
| C179 | Summarize coding-session work narrative | claude-code | spendguard.claudecode.story(by='week', days=7, run=False) | capability_claude_code__14_claudecode_story | [spendguard.claudecode:story](#core-1b105145c5500fa758a2650831566d24a2ca468dd845512bd06cd1efb95573d7) |
| C180 | Read Codex subscription usage report | codex | spendguard.codex.show_codex_report(days=None) | capability_codex__1_codex_show_codex_report | [spendguard.codex:show_codex_report](#core-1ef4b1b007645d4d16061893217cf94c01bda586a3d5d61ff121d971aa775d6b) |
| C181 | Rebuild Codex usage from transcript sources | codex | spendguard.codex.update(st=None) | capability_codex__2_codex_update | [spendguard.codex:update](#core-117d123a9aee976586207b966d4b814e1a1e032faeaac2a22e5acb7de8f6caad) |
| C182 | Classify Codex session attribution | codex | spendguard.codex.classify(run=False, days=None, recls=False) | capability_codex__3_codex_classify | [spendguard.codex:classify](#core-0bb0c552b8f2f94df1a4986a4ec42f09f0e0a57bdd66c293b6c823e541b22719) |
| C183 | Synchronize Codex usage to organization accounting | codex | spendguard.codex.sync(dry=False) | capability_codex__4_codex_sync | [spendguard.codex:sync](#core-700262b3620f05c8617829c1e88cef6d3f433270d02219aadc6c9554dd550a5d) |
| C184 | Read refreshed browser-chat subscription usage | chat | spendguard.chat.show_chat_report(days=None, refresh=True) | capability_chat__1_chat_show_chat_report | [spendguard.chat:show_chat_report](#core-1c74a075f6e7dd7a8cf1a2bf3c90295cb59a5e6440ce83beb146de12bc8b165e) |
| C185 | Read browser-chat usage by project | chat | spendguard.chat.show_by_project(days=None) | capability_chat__2_chat_show_by_project | [spendguard.chat:show_by_project](#core-00f0659dfbccc9874fe9370841518330f062c5cf5238dd22a4166fd93c9f6952) |
| C186 | Diagnose browser-chat source connectivity | chat | spendguard.chat.diagnose_connection() | capability_chat__3_chat_diagnose_connection | [spendguard.chat:diagnose_connection](#core-556cc1a8d2f8911bc043ddae16e2670631bb813f78ff4b9623c2b91d84ec2710) |
| C187 | Consent to browser-chat attribution ingestion | chat | capability_chat__4_chat_set_chat_consent | ABSENT (DELIBERATE): Enabling personal browser-chat ingestion requires explicit local member consent. | [spendguard.chat:set_chat_consent](#core-eabbf4460903fce45385927ac898fb5a7d8527e22ad3ebd569dae50163cb0bfb) |
| C188 | Revoke browser-chat attribution ingestion consent | chat | capability_chat__5_chat_set_chat_consent | ABSENT (DELIBERATE): Revoking personal browser-chat ingestion consent remains an explicit member action. | [spendguard.chat:set_chat_consent](#core-eabbf4460903fce45385927ac898fb5a7d8527e22ad3ebd569dae50163cb0bfb) |
| C189 | Read browser-chat attribution consent and source state | chat | capability_chat__6_chat_show_chat_adapter_status | capability_chat__6_chat_show_chat_adapter_status | [spendguard.chat:show_chat_adapter_status](#core-ce9c57855b3d9b03d0b3286466070ecb6ec87ec259e4df65ad636be25c7ba62c) |
| C190 | Accept the organization attribution taxonomy | chat | capability_chat__7_chat_accept_chat_org_taxonomy | ABSENT (DELIBERATE): Accepting an organization taxonomy requires explicit local member agreement. | [spendguard.chat:accept_chat_org_taxonomy](#core-b54ba257b4ca32b9421b050fd32f842bf0b561892e8ffcebe98a3c3d5b485558) |
| C191 | Push the local attribution taxonomy to the organization | chat | capability_chat__8_chat_publish_chat_org_taxonomy | capability_chat__8_chat_publish_chat_org_taxonomy | [spendguard.chat:publish_chat_org_taxonomy](#core-da50b836306356f0afb3ceae461f9643fcf91e60c5366dca494a7c398d29bcb6) |
| C192 | Discover project taxonomy from browser-chat usage | chat | spendguard.chat.discover_taxonomy(run=False, days=None, sample=None, apply=True) | capability_chat__9_chat_discover_taxonomy | [spendguard.chat:discover_taxonomy](#core-eb5f6e6f6c49d2a7bc4fd91350abbba198cc78d1e5e3828d87d56886d470a1c9) |
| C193 | Classify browser-chat conversations into project allocations | chat | spendguard.chat.classify_chat_allocations(run=False, days=None, recls=False, batch_size=25) | capability_chat__10_chat_classify_chat_allocations | [spendguard.chat:classify_chat_allocations](#core-68a183d1490cca16f5b2f1762864dd6cbfcf7d690b8a0f96c7e1f5887b11e260) |
| C194 | Run browser-chat attribution convergence loop | chat | spendguard.chat.loop(run=False, force_discover=False, quiet=False) | capability_chat__11_chat_loop | [spendguard.chat:loop](#core-0d3b02df6e948747be2ec9928fbb5fef640a8a4fac7b3cc73e7f9f4b2807235a) |
| C195 | Synchronize browser-chat usage to the organization | chat | spendguard.chat.sync(dry=False) | capability_chat__12_chat_sync | [spendguard.chat:sync](#core-2c89d5fdec60f47c99adb513a0c4ce721f925f6e2b5ac80ea1b4719e34b29d0e) |
| C196 | Read browser-chat completed-work rollup | chat | spendguard.chat.show_chat_work(by='week', days=None) | capability_chat__13_chat_show_chat_work | [spendguard.chat:show_chat_work](#core-7168401015ccf497937c0cda76c566ba395f0e34655a8034d626303a49df45ad) |
| C197 | Summarize browser-chat completed-work narrative | chat | spendguard.chat.story(by='week', days=7, run=False) | capability_chat__14_chat_story | [spendguard.chat:story](#core-380f107d8ba37dcb03f45c076a554751f9f7f7909849806e43f430bf553fb39b) |
| C198 | Read subscription-lane activation and readiness | lanes | spendguard.lanes.lanes_status() | capability_lanes__1_lanes_lanes_status | [spendguard.lanes:lanes_status](#core-8711ae3ddefdf7a4350d173765a59eb738e52d33fb78555109372dba51d538f3) |
| C199 | Declare a validated subscription lane model assignment | lanes | capability_lanes__2_lanes_set_declared_lane_model | capability_lanes__2_lanes_set_declared_lane_model | [spendguard.lanes:set_declared_lane_model](#core-811b8693fb88d34de4deae4d4b4a7d34e9fbf23935f0bbcfee1ac4fd75c329ea) |
| C200 | Probe enabled subscription lanes with bounded prompts | lanes | spendguard.lanes.probe(timeout_s=None) | capability_lanes__3_lanes_probe | [spendguard.lanes:probe](#core-317bb746f8055d1dc99b6fd2169415945c0c310f3b748914813412bbb923b31c) |
| C201 | Read subscription quota headroom and measured quota cost | lanes | spendguard.lanes.lane_headroom(do_fetch=True) | capability_lanes__4_lanes_lane_headroom | [spendguard.lanes:lane_headroom](#core-b15e1d895b15d4e6bb8080223cf5e798a27d3fae56cedd57b391cbd1d7f84f8c) |
| C202 | Recover formerly unserved lane-model combinations | lanes | spendguard.lane_servability.refresh_lane_servability(lane=None, announce=True, reprobe=False) | capability_lanes__5_lane_servability_refresh_lane_servability | [spendguard.lane_servability:refresh_lane_servability](#core-bdd0fa8273740598314d8a7c0f94b5ff083ecd8389e65aeff7319a0bdc3e1a69) |
| C203 | Estimate or adjudicate avoidable metered intent usage | lanes | spendguard.lane_eligibility.lane_eligible_report(since=None, execute=False) | capability_lanes__6_lane_eligibility_lane_eligible_report | [spendguard.lane_eligibility:lane_eligible_report](#core-be26dee7a15789982c814ac30f6401b4d2d7eec71ab5de4cdc8f665a1361d6ff) |
| C204 | Read served lane model and fallback catalog | lanes | spendguard.lane_catalog.catalog() | capability_lanes__7_lane_catalog_catalog | [spendguard.lane_catalog:catalog](#core-02c3faf57d29410f840bf770b52c29175b6a11451c4a3c02c969dc473349f053) |
| C205 | Read measured cross-lane bandit learning | lanes | spendguard.lane_bandit.learned_table(intent=None) | capability_lanes__8_lane_bandit_learned_table | [spendguard.lane_bandit:learned_table](#core-4d269fb22816632e9215ae3b0be5bdf40d941879c1023dd353d6a1c1b5097379) |
| C206 | Run a cross-lane bakeoff and record its winner | lanes | spendguard.lane_bandit.run_bakeoff(intent, task, system=None, reasoning=None, timeout_s=None) | capability_lanes__9_lane_bandit_run_bakeoff | [spendguard.lane_bandit:run_bakeoff](#core-fbf2bd88e1402f846dc39fef7cdab67e403f6651d4052db5c60bb149ec7960cf) |
| C207 | Estimate cross-lane bakeoff judge cost without spending | lanes | spendguard.lane_bandit.estimate_judge_cost(bakeoffs=(10, 100, 1000)) | capability_lanes__10_lane_bandit_estimate_judge_cost | [spendguard.lane_bandit:estimate_judge_cost](#core-cfbb4ab8138c26e70e0a9f8c70d4662e850c6a3b623fdb26e5215a1ed5d2541f) |
| C208 | Read subscription plan utilization balance | lanes | spendguard.lane_balance.format_utilization() | capability_lanes__11_lane_balance_format_utilization | [spendguard.lane_balance:format_utilization](#core-602afd1cb98557e7b1c5288f94ea93dd1ffa8008d7175c71e89d7e8c7b45215d) |
| C209 | Read measured lane token economics and fee at risk | lanes | spendguard.lane_economics.format_economics(headroom_rows=None) | capability_lanes__12_lane_economics_format_economics | [spendguard.lane_economics:format_economics](#core-3d48a9dd4df10dc2b90952096e3946663352212258d8cfadf983db2cc333f616) |
| C210 | Read lane-to-metered fallback model mappings | lanes | spendguard.lane_catalog.format_lane_fallback() | capability_lanes__13_lane_catalog_format_lane_fallback | [spendguard.lane_catalog:format_lane_fallback](#core-53af2eeb48467a2eef169b9ff28df83d60229725aad7311f909a45c63f5477b5) |
| C211 | Read actual metered spending caused by lane failures | lanes | spendguard.reliability.lane_fallback_spend(since=None) | capability_lanes__14_reliability_lane_fallback_spend | [spendguard.reliability:lane_fallback_spend](#core-50b08c061170775a558481ddc11e90285ce0a31c03bd9823615140699596769d) |
| C212 | Read lane-to-metered reasoning equivalence | lanes | spendguard.reasoning_equivalence.format_map() | capability_lanes__15_reasoning_equivalence_format_map | [spendguard.reasoning_equivalence:format_map](#core-b65f75899acffccfc70ff1769f463645cbf938d99fdcade7a67a255f766381a3) |
| C213 | Propose acceptable model substitutions pending confirmation | lanes | spendguard.lane_balance.propose_substitutes(intent, primary_model, candidates=None) | capability_lanes__16_lane_balance_propose_substitutes | [spendguard.lane_balance:propose_substitutes](#core-f802c7bc16c272770f48be601912ebfccd701a135bc09340c37b1b3a7bbd418d) |
| C214 | Confirm a proposed model substitution | lanes | spendguard.lane_balance.confirm_substitute(intent, substitute) | capability_lanes__17_lane_balance_confirm_substitute | [spendguard.lane_balance:confirm_substitute](#core-3f109b35b1bf688a76d42f91663c9d8c85ea1691cd3a1ae8352fbee7f0a9f211) |
| C215 | Run a one-shot task on a viable idle subscription lane | lanes | spendguard.lane_balance.delegate(task, system=None, lanes=None, reasoning='low', max_tokens=None, intent=None, enqueue=False, priority=None, schema=None, refuse_billed=False) | capability_lanes__18_lane_balance_delegate | [spendguard.lane_balance:delegate](#core-d25c092df161c0f7945bda126685f54b33aa813e858eb57cf8fe78b8c2c872f2) |
| C216 | Execute generic resilient concurrent task fan | lanes | spendguard.lane_balance.bulk_delegate(tasks, intent, system=None, reasoning=None, max_workers=None, deadline_s=120.0, checkpoint=None, chunk_size=100, refuse_billed=False, stats=None, force=False, tier=None, schema=None, expect_ids=None, gate_sig=None, lanes=None, images_for=None, vision_model=None, model_for=None, prompt_for=None, task_key=None, return_keyed=False, on_miss=None, batch_submit=None, batch_model=None, batch_cap=None, hedge_ms=None, strategy=None, metered_only=False, sla_class=None, record_route=True, budget_usd=None, auth_over_quota=False); governing inputs: tasks,intent,system,reasoning,max_workers,deadline_s,checkpoint,chunk_size,refuse_billed,stats,force,tier,schema,expect_ids,gate_sig,lanes,images_for,vision_model,model_for,prompt_for,task_key,return_keyed,on_miss,batch_submit,batch_model,batch_cap,hedge_ms,strategy,metered_only,sla_class,record_route,budget_usd,auth_over_quota | capability_api_contract_10_lane_balance_bulk_delegate | [spendguard.lane_balance:bulk_delegate](#core-3a95c6dbcc309be1f46006b559696b715b926059da92d2a097eb45bfe3e99237) |
| C217 | Estimate a confined bulk fan and fallback ceiling | lanes | spendguard.lane_balance.estimate_fan(tasks, intent, system=None, lanes=None, tier=None, out_est=None) | capability_lanes__20_lane_balance_estimate_fan | [spendguard.lane_balance:estimate_fan](#core-3ef84fd65013aac7caa70bc75e435d895b158bb30362778f25cca47a77fd4145) |
| C218 | Durably enqueue subscription-lane tasks | lanes | spendguard.lane_queue.enqueue_many(intent, tasks, system=None, reasoning=None, priority=0, max_attempts=None, sla_class='batch', deadline_ts=None, dedup=True) | capability_lanes__21_lane_queue_enqueue_many | [spendguard.lane_queue:enqueue_many](#core-54e5561dce65f9d44ec8f456e9519013568678fbd6fc48be7c6a03a5b1b86fe2) |
| C219 | Drain durable tasks onto idle subscription lanes | lanes | spendguard.lane_queue.drain(worker=None, batch=None, lease_s=None, idle_rounds=None, idle_sleep=None, load_ceiling=None, forever=False, max_iters=None) | ABSENT (DELIBERATE): Draining the durable queue owns host worker process lifecycle. | [spendguard.lane_queue:drain](#core-cfc85a9600677ff20dfe9c0a25629293e8eca80e9ca7bf0e92dd2f7a780e0aed) |
| C220 | Read durable subscription-lane queue depth | lanes | spendguard.lane_queue.queue_depth() | capability_lanes__23_lane_queue_queue_depth | [spendguard.lane_queue:queue_depth](#core-e3f6a9901b2e0b4ba8cd452283f226371fa1c46dad81658632613a578397ad83) |
| C221 | Archive and purge old terminal lane-queue records | lanes | spendguard.lane_queue.purge(retain_days=None, archive_path=None, chunk=None, stale_days=None) | ABSENT (DELIBERATE): Archiving terminal queue records belongs to host storage maintenance. | [spendguard.lane_queue:purge](#core-59ad89c22f339a54b6fa4eae408944fbba3f0e9180c93e6638215082ce8c73af) |
| C222 | Migrate lane-queue storage out of the spend ledger | lanes | spendguard.lane_queue.migrate_from_ledger() | ABSENT (DELIBERATE): Queue database migration belongs to host storage lifecycle maintenance. | [spendguard.lane_queue:migrate_from_ledger](#core-394d72c86f387eaae6bd5e2d2cafac6c280e38cb78e220d53b33568fc3cc84b9) |
| C223 | Read aggregate admission and execution queue state | dispatch | spendguard.dispatch.admission_state(); spendguard.lane_queue.queue_row_status(row_id, archive_path=None) | spendguard_dispatch_state(id) | [spendguard.dispatch:admission_state](#core-ca799ffcbf23d86f2e0e5171395b3d2252b3dce0e8e23ba688b8eb3064f69f32); [spendguard.lane_queue:queue_row_status](#core-227ce4e2f1a86c6683cfcafec5ac3eae3fd6fbe34a6170d06407c63b8fbaac95) |
| C224 | Read the next admission queue plan | plan-queue | spendguard.queue_planner.tick(avg_call_tokens=NOMINAL_CALL_TOKENS, pending_by_intent=None); spendguard.batch_tracker.status() | spendguard_plan_queue() | [spendguard.queue_planner:tick](#core-82fea51d1aebe63a6af678794ebf08e958692b87b88ef9b13e15a0cee4c944c3); [spendguard.batch_tracker:status](#core-be53ce2abbc8feb169b28c9cfc604e5cef3d13d962cf106aa4f4e6b62b60a219) |
| C225 | Watch queue plans at a bounded or continuous cadence | plan-queue | spendguard.queue_planner.plan_loop(interval_s=None, iterations=None, on_tick=None, avg_call_tokens=NOMINAL_CALL_TOKENS, stop_event=None) | ABSENT (DELIBERATE): A continuous queue watcher owns a host process; tools expose finite planning snapshots. | [spendguard.queue_planner:plan_loop](#core-0e359380813e4fec8c4f012774068f7f81c1a53af3fab16bc5131e124bce7c61) |
| C226 | Read split real-dollar and subscription-value receipt | receipt | spendguard.receipt.tally(project=None, conv=None); spendguard.receipt.render_tree(scope_org=None, project=None) | capability_receipt__1_receipt_tally; capability_receipt__1_receipt_render_tree | [spendguard.receipt:tally](#core-e3f1e34ed8684336ce6523bbbb51bedf56966a1b4c4815fb631f2c570be97679); [spendguard.receipt:render_tree](#core-ff54ec28ea4b2901430b56b4dcc44c02cbf19c0454e73e405abb3dbdc2ea8b8e) |
| C227 | Read receipt allocation by intent | receipt | capability_receipt__2_receipt_render_intent_spend_receipt | capability_receipt__2_receipt_render_intent_spend_receipt | [spendguard.receipt:render_intent_spend_receipt](#core-0cbeb93f39616435a0c6cf5f1ece04e7331ded7d461ce5f85094fa7d8cf94e70) |
| C228 | Render compact receipt statusline | receipt | spendguard.receipt.render_line(t: Optional[dict]=None) | ABSENT (DELIBERATE): Terminal statusline rendering belongs to the shell integration protocol. | [spendguard.receipt:render_line](#core-59169e5e8683ddee91c773df8afb0828295e3011c3a6084091168cadceddab39) |
| C229 | Emit receipt through an assistant lifecycle hook | receipt | ABSENT (DELIBERATE): Lifecycle-hook output belongs to the assistant client event protocol. | ABSENT (DELIBERATE): Receipt lifecycle-hook output belongs to the assistant client event protocol. |  |
| C230 | Read provider token-count calibration factors | tokens | spendguard.provider_tokens.factor(provider) | capability_tokens__1_provider_tokens_factor | [spendguard.provider_tokens:factor](#core-24e8ac9b590f22a563af6fb66741cd40c5d1200be1a8cbba7068737b2a3d1c33) |
| C231 | Calibrate provider token-count estimates | tokens | spendguard.provider_tokens.calibrate(store=True, model=None) | capability_tokens__2_provider_tokens_calibrate | [spendguard.provider_tokens:calibrate](#core-24c4d357d8ae8c8de17790907e11a4e3ac5c86bc3e8d0be04f8fcff15152ccd3) |
| C232 | Recover provider batch prompt/output samples | fetch-io | spendguard.callio.fetch_history(cap=DEFAULT_CAP, sample_n=None, limit_batches=None) | capability_fetch_io__1_callio_fetch_history | [spendguard.callio:fetch_history](#core-b09ec82c7d4e35675d6110f99587223c10127ce7fb94d874e8de8a83953177e3) |
| C233 | Link recovered samples to transcript conversations | fetch-io | spendguard.callio.link_conversations(tdir=None, turns=10) | capability_fetch_io__2_callio_link_conversations | [spendguard.callio:link_conversations](#core-2cca63d5198d65f5b17141dde17b7944d7ee0a7ed97b2e12b56ec541690804b6) |
| C234 | Inspect suspicious ledger charges | quarantine | spendguard.budget.suspect_batches(since) | capability_quarantine__1_budget_suspect_batches | [spendguard.budget:suspect_batches](#core-5abbb770d9b0ed78ddc275faefaab001dc08d7bfc372b69907b34ecc25bb0ecf) |
| C235 | Quarantine a selected impossible ledger charge | quarantine | spendguard.budget.quarantine_charge(ts=None, reason='', row=None) | capability_quarantine__2_budget_quarantine_charge | [spendguard.budget:quarantine_charge](#core-3e8b82031ed5205f9685a5d0e4b5fdd69e918060a897a412fd566d552dc3bd3b) |
| C236 | Plan a whole job set under budget and urgency | submit-jobs | spendguard.whole_job.plan_jobs(jobs, goal=None) | capability_submit_jobs__1_whole_job_plan_jobs | [spendguard.whole_job:plan_jobs](#core-d7560e6b5b5de16891610f1e872d4bba7bf03cb2eecce87c830ffaeef12f7923) |
| C237 | Execute a whole job set under the same planner | run-jobs | spendguard.whole_job.run_jobs(jobs, goal=None, checkpoint=None); spendguard.whole_job.plan_jobs(jobs, goal=None) | spendguard_run_jobs(jobs, goal) | [spendguard.whole_job:run_jobs](#core-fe7c6b7700ca6245e820f73a7eb1103c0fba6eaa7e00702ff590826091f48951); [spendguard.whole_job:plan_jobs](#core-d7560e6b5b5de16891610f1e872d4bba7bf03cb2eecce87c830ffaeef12f7923) |
| C238 | Collect submitted whole-job batch results | submit-jobs | spendguard.whole_job.collect_jobs(pending); spendguard.whole_job.load_pending(pending_path) | capability_submit_jobs__3_whole_job_collect_jobs; capability_submit_jobs__3_whole_job_load_pending | [spendguard.whole_job:collect_jobs](#core-ced4e6dea034361e8f1fa08a7620343c48170414360a6a65b7abc34f40e07c3f); [spendguard.whole_job:load_pending](#core-b18419cfba5c2fee4e321842f113f23e578f870ab9506863def975e6c0279c54) |
| C239 | Read serving and green release alignment | version | spendguard.release.release_status() | spendguard_version() | [spendguard.release:release_status](#core-09c7d0490739d52d23e294b6759d3828232c8e629dd413e61923fb2770600181) |
| C240 | Attest release convergence across consumers | release | spendguard.release.converge_deployed_release(*, check=False, roots=None, dry_run=False, settings=None) | capability_release__2_release_converge_deployed_release | [spendguard.release:converge_deployed_release](#core-d2ce5e36f9ae7960f2a528f1b65f014da7c94717295f6237fe96503eb5a8ad63) |
| C241 | Read canonical actionable catalogue and provider-qualified token prices | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.model_catalog.all_records(); spendguard.pricing.price(model, provider=record.provider) | spendguard_models() | [spendguard.model_catalog:all_records](#core-57b94d9c05a84cb88cfe670204b279029c19687a51709c294bd66ce8f55ee9b6); [spendguard.pricing:price](#core-48684c4a316361c99450b70639660d96ad37238bfa1200244eccf5ff4a6e772d) |
| C242 | Run ONE metered VISION call (image(s) + prompt) through spendguard's governed adapter. The subscription lanes are text-only CLIs, so vision rides the metered API (executor='api'), pixel-priced and bounded by a deadline. Estimate-first: with NO budget_usd it returns ONLY the estimate ($0); pass budget_usd to run (refused over it). A dynamic-key MAP schema is refused before any spend (strict mode empties it to {} — use an ARRAY of {key,value}); paid results are cached by request content, so an identical re-request is $0. | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.adapters.vision(model, prompt, images, *, schema=None, system=None, sig=None, reasoning=None, max_tokens=None, timeout_s=None, retries=2, no_metered_fallback=False, no_substitution=False) | spendguard_vision(model, prompt, images, schema, system, budget_usd, max_out_tokens, no_cache, timeout_s) | [spendguard.adapters:vision](#core-c28fbbc6d9e7b81fbf3073b3c41732395486919179eccbd63aa1ed11fb87c955) |
| C243 | The headline for THIS account: REAL $ out the door (subscription base + Claude Code overage + API credits) shown SEPARATELY from est-value (plan-covered Claude Code usage, what it's worth at API rates). The two axes are never summed. $0, read-only. | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | capability_spend_overview_mcp_server_capability_spend_overview_operation | spendguard_spend_overview() | [spendguard.mcp_server:capability_spend_overview_operation](#core-35c9492d1c30fb54dd8eef2633765ca4f615093ee823d66cc14259468e608b7b) |
| C244 | Cost of ONE conversation by its transcript id: plan-covered est-value + observed overage upper bound (the two axes kept separate). Get ids from spendguard_top_conversations. $0, read-only. | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.ledger_read.read_cc_conversation_cost(conv, path=None) | spendguard_conversation_cost(conversation_id) | [spendguard.ledger_read:read_cc_conversation_cost](#core-9b9f14c10ac3d2ecf6ecdbe4ada57f7f5326618a2756a57a70c7d5b6166d316c) |
| C245 | Open conversations that are expensive to keep alive — large re-read context billing every turn — with the $/turn cost, what compacting would save, and the ready-to-paste effective /compact command. Surfaces WHEN compacting a conversation is a good trade. $0, read-only. | claude-code | spendguard.claudecode.compaction_candidates(min_context=None, min_turns=None, min_day=None); spendguard.compaction.compact_snippet() | spendguard_compaction_candidates(limit) | [spendguard.claudecode:compaction_candidates](#core-e0c6bc027509f51ee52b069465806e4e4b3d48ef243a1d36501954d0e78ca2e1); [spendguard.compaction:compact_snippet](#core-7d538e5d056753beb062583208e79f285f934d311981f976c815677b75db1c19) |
| C246 | Estimate, budget-gate the estimate, and cache one governed image request | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | capability_vision_estimate_budget_cache_mcp_server_capability_vision_operation | spendguard_vision(model,prompt,images,budget_usd,max_out_tokens,no_cache,timeout_s) | [spendguard.mcp_server:capability_vision_operation](#core-368fb7d5e641a3b98fa1c329756c2e52007fd0115d365bde59acac295596c715) |
| C247 | Constrain recommendation count, quality bar and one-call estimate budget | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.advisor.recommend_models(intent,k,quality_bar,run) with config.scoped_intent_cap | spendguard_recommend(intent,k,quality_bar,budget_usd) | [spendguard.advisor:recommend_models](#core-a17c133ed591f37bb800091483a2a96a14fe2e3013f48724b37a39353cadb617) |
| C248 | Choose whether endpoint health generates cached agentic remediation | reliability | spendguard.reliability.remediate(sweep_result, model=None) | spendguard_health(run,remediate,timeout_s) | [spendguard.reliability:remediate](#core-ff68b50581fddd0031141957736e71cdb2940f4f7ebd8bf83d1fea30ea15622a) |
| C249 | Choose metered realtime model for urgent horizon overflow | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.route_horizon.horizon_report(groups,realtime_model=M) | spendguard_route_horizon(groups,realtime_model=M) | [spendguard.route_horizon:horizon_report](#core-0e7e2a8858fc7f6a5f9755a4c5a18b9ee04dbcf7ecda5a144d3f354335cd46bb) |
| C250 | Supply caller-owned prompts instead of sampling recorded corpus | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.bakeoff.bakeoff(intent,candidates,prompts=[…]) | spendguard_bakeoff(intent,candidates,prompts=[…]) | [spendguard.bakeoff:bakeoff](#core-0a386423d2156252798bb6307054e9cf35ca1df1e68efd1d5fd5817741f4b3e8) |
| C251 | Execute measured best-value model and effort selection on a real call | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.adapters.call(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None, sig=None, intent=None, retries=2, files=None, _no_guard=False, no_metered_fallback=False, images=None, no_substitution=False, measurement=False, metered_only=False, base_fallback=None, _probe=False, _route=True, _internal_pin=False, _coalesce_eligible=True, temperature=None, top_p=None, seed=None, soft_deadline_s=None, auth_over_quota=False, decomposition=None, **aliases); governing inputs: model,prompt,reasoning="best-value",intent,no_substitution | capability_api_contract_0_adapters_call | [spendguard.adapters:call](#core-a03c9815845a6ed20a964a37c103c6b63fe691be8ac9e6050aa38130f17c9db5) |
| C252 | Execute a governed single text call with explicit caller controls | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.adapters.call(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None, sig=None, intent=None, retries=2, files=None, _no_guard=False, no_metered_fallback=False, images=None, no_substitution=False, measurement=False, metered_only=False, base_fallback=None, _probe=False, _route=True, _internal_pin=False, _coalesce_eligible=True, temperature=None, top_p=None, seed=None, soft_deadline_s=None, auth_over_quota=False, decomposition=None, **aliases); governing inputs: model,prompt,system,schema,timeout_s,retries,intent,sig,reasoning,files,soft_deadline_s,base_fallback,auth_over_quota,decomposition | capability_api_contract_1_adapters_call | [spendguard.adapters:call](#core-a03c9815845a6ed20a964a37c103c6b63fe691be8ac9e6050aa38130f17c9db5) |
| C253 | Constrain actual executor and forbid substitutions or billed fallbacks | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.adapters.call(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None, sig=None, intent=None, retries=2, files=None, _no_guard=False, no_metered_fallback=False, images=None, no_substitution=False, measurement=False, metered_only=False, base_fallback=None, _probe=False, _route=True, _internal_pin=False, _coalesce_eligible=True, temperature=None, top_p=None, seed=None, soft_deadline_s=None, auth_over_quota=False, decomposition=None, **aliases); governing inputs: metered_only,measurement,no_substitution,no_metered_fallback,auth_over_quota | capability_api_contract_2_adapters_call | [spendguard.adapters:call](#core-a03c9815845a6ed20a964a37c103c6b63fe691be8ac9e6050aa38130f17c9db5) |
| C254 | Set generation controls and inspect whether each applied | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.adapters.call(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None, sig=None, intent=None, retries=2, files=None, _no_guard=False, no_metered_fallback=False, images=None, no_substitution=False, measurement=False, metered_only=False, base_fallback=None, _probe=False, _route=True, _internal_pin=False, _coalesce_eligible=True, temperature=None, top_p=None, seed=None, soft_deadline_s=None, auth_over_quota=False, decomposition=None, **aliases); spendguard.adapters.generation_support_details(provider, model); governing inputs: temperature,top_p,seed;provider,model | capability_api_contract_3_adapters_call; capability_api_contract_3_adapters_generation_support_details | [spendguard.adapters:call](#core-a03c9815845a6ed20a964a37c103c6b63fe691be8ac9e6050aa38130f17c9db5); [spendguard.adapters:generation_support_details](#core-69050de5faba0211df71f4256c1c82a7bf505ecd01a5bb08cc04773449e66fc5) |
| C255 | Inspect actual executor provider model and substitution provenance | ABSENT (DELIBERATE): Wrappers return these fields; language-specific result inspection needs no standalone command. | spendguard.adapters.served_by(result); spendguard.adapters.was_substituted(result); spendguard.adapters.panel_providers(results); governing inputs: result;results | ABSENT (DELIBERATE): Wrappers return these fields; language-specific result inspection needs no standalone command. | [spendguard.adapters:served_by](#core-eea380d84c003c970235e014651306a4d1f127792921bcee4f469522ce5e4761); [spendguard.adapters:was_substituted](#core-94c5f0267e76eb0720077029f2a8140fea908e5a4d7cb211a78a40af49b06c7e); [spendguard.adapters:panel_providers](#core-2964e7d7b3db7338eb0f4d4319d4750e3925f3d2d1ffde6e8d18881679ccf1a4) |
| C256 | Generate gated realtime embeddings with resumable checkpoint | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.adapters.embed(texts, model=None, *, dimensions=None, max_batch=None, timeout_s=None, checkpoint=None); governing inputs: texts,model,dimensions,max_batch,timeout_s,checkpoint | capability_api_contract_6_adapters_embed | [spendguard.adapters:embed](#core-ab52ba0149c314f1b9f89cdee406e2f6b82d561b9ac7aef3f363d9ff09bdda4f) |
| C257 | Submit capped embedding Batch API work | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.adapters.embed_batch(texts, model=None, *, dimensions=None, jsonl_path=None, cap_dollars=None, submit=True, preflight=True); governing inputs: texts,model,dimensions,jsonl_path,cap_dollars,submit,preflight | capability_api_contract_7_adapters_embed_batch | [spendguard.adapters:embed_batch](#core-35acdd39e41504e3005a2c4fe685c4f5fe166831bde1aa417403174590f0a648) |
| C258 | Compare embedding quality and cost on same texts | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.adapters.embed_compare(texts, models=None, *, sample=None); governing inputs: texts,models,sample | capability_api_contract_8_adapters_embed_compare | [spendguard.adapters:embed_compare](#core-d8f59f72792b19ec7997b313d2a6f886df33af39c9ef29bc876f57debd59a7b6) |
| C259 | Submit Anthropic native Messages Batch | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.submit.submit_message_batch(tasks, model, *, system=None, schema=None, max_out=None, expected_out_tokens=None, cap_dollars=None, submit=True, request_cap=_ANTHROPIC_BATCH_REQUEST_CAP, intent=None, preflight=True, shard_size=None, force=False); governing inputs: tasks,model,system,schema,max_out,expected_out_tokens,cap_dollars,submit,request_cap,intent,preflight,shard_size,force | capability_api_contract_12_submit_submit_message_batch | [spendguard.submit:submit_message_batch](#core-0dc752289be6c59fe664eb585217ed3206540c87f8ca147ab2ce9543f1a694d6) |
| C260 | Collect all OpenAI chat Batch results with explicit pending/error states | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.callio.collect_chat_tasks(batch_ids, intent, model, require_ready=True, record_io=False); governing inputs: batch_ids,intent,model,require_ready,record_io | capability_api_contract_13_callio_collect_chat_tasks | [spendguard.callio:collect_chat_tasks](#core-31e3e87d5d0c535161cf8afad6c64bd151823006b9de9d4203ecfcc005584877) |
| C261 | Collect all Anthropic native Messages Batch results | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.callio.collect_message_batch(batch_ids, intent, model, require_ready=True, record_io=False); governing inputs: batch_ids,intent,model,require_ready,record_io | capability_api_contract_14_callio_collect_message_batch | [spendguard.callio:collect_message_batch](#core-ac1d68b3ff1225ef8e5c9ba0b345f249d5a866a3fca16b7240c95dbab8e72b6e) |
| C262 | Issue typed deadline-bounded vendor call | ABSENT (DELIBERATE): Typed in-process result protocol wraps existing call capability; no separate user job needed. | spendguard.vendor_call.call(vendor, model, prompt, *, deadline_s, purpose='', system=None, max_tokens=None, schema=None, attempts=IN_PROCESS_RETRY_ATTEMPTS, backoff_s=2.0, reasoning=None, no_metered_fallback=False); governing inputs: vendor,model,prompt,deadline_s,purpose,system,max_tokens,schema,attempts,backoff_s,reasoning,no_metered_fallback | ABSENT (DELIBERATE): Typed in-process result protocol wraps existing call capability; no separate user job needed. | [spendguard.vendor_call:call](#core-5578d64dce4c619e3414984f06f207068bbcd363fc04250243b740351af5f23d) |
| C263 | Build provider-native strict JSON schema request | ABSENT (DELIBERATE): SDK request-shape construction is an integration primitive; structured-call wrappers deliver its outcome. | spendguard.adapters.json_schema_request(kind, schema, name='result'); spendguard.adapters.schema_capability(provider); governing inputs: kind,schema,name;provider | ABSENT (DELIBERATE): SDK request-shape construction is an integration primitive; structured-call wrappers deliver its outcome. | [spendguard.adapters:json_schema_request](#core-37856c37c23162559aa68a492c4db1558c9fa7f8895a36acfb4ddda45b99c442); [spendguard.adapters:schema_capability](#core-e48066cc549119da9d2e09d9f49ad1450f36f38c97b556ea13b77ab01059e80a) |
| C264 | Set scoped generation defaults | ABSENT (DELIBERATE): A process context manager has no independent meaning outside caller code. | spendguard.adapters.generation(temperature=None, top_p=None, seed=None); governing inputs: temperature,top_p,seed | ABSENT (DELIBERATE): A process context manager has no independent meaning outside caller code. | [spendguard.adapters:generation](#core-7e40d0f70a93d5394ede11017981141a79cb69e3608f4d5598400031d2242a22) |
| C265 | Compute model-safe input budget and measured call deadline | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.adapters.safe_input_chars(model, intent=None, reply_tokens=8000, prompt_tokens=1500, chars_per_token=3.0); spendguard.adapters.deadline_for(model, intent=None, in_chars=None, default_s=None); governing inputs: model,intent,reply_tokens,prompt_tokens,chars_per_token;model,intent,in_chars,default_s | capability_api_contract_18_adapters_safe_input_chars; capability_api_contract_18_adapters_deadline_for | [spendguard.adapters:safe_input_chars](#core-f5abf0761ded424f5fe42d04883afdb26c31aec8804c7ac86f4a0fdcfb3ca9cb); [spendguard.adapters:deadline_for](#core-7cd63734d7e1d279db772c3c0ceedc9250715358d971ca640fb258b59a4f9d9d) |
| C266 | Install and require runtime SDK gate; register interceptor | ABSENT (DELIBERATE): Interpreter patching and callback registration require direct access to caller runtime. | spendguard.install; spendguard.require; spendguard.register; spendguard.install_litellm(); spendguard.install_bedrock(); spendguard.install_vertex(); spendguard.gate_external; spendguard.refuse_metered; governing inputs: cap;cap;module_path,class_name,method,gate_fn,is_async;gate_external(est_cost,service);refuse_metered() | ABSENT (DELIBERATE): Interpreter patching and callback registration require direct access to caller runtime. | [spendguard:install](#core-f8873328457c38a56be977e0ce2fad39a4f3da94e84f9385c8df1f218f97f24c); [spendguard:require](#core-e0c930d4b5eb2879addf3ad6ec6383db0ae771a080d2563fce11d39c4d431de1); [spendguard:register](#core-0ac9cd175081fc3e8d787e1be342ee88c5be12fe1613b1449351d0dc9097a94a); [spendguard:install_litellm](#core-a542cdd1648c36d5a17c728f8d917e99c3fd61ab7e0de493e627f178f13913cc); [spendguard:install_bedrock](#core-5b3bae2956fa557a0d37a93f909e696edc4e65bf293ab60bec8ade6ff07a39e5); [spendguard:install_vertex](#core-b1e78e1db844c6b75f8270e1c36a3f6fb0ec24950b2c1a28c6b0a0d318a3af20); [spendguard:gate_external](#core-2ec147b5670c7c7a5c4b8ef8e79c4ddd204cf1afcabdfa4ca6a844bc38fc5ffc); [spendguard:refuse_metered](#core-9f7136dcdfb596387709617bee917a23c22edf689422e792d0ca25f91c7873be) |
| C267 | Attach contextual attribution and feedback to direct calls | ABSENT (DELIBERATE): Thread-local scopes and executable callbacks require application runtime access. | spendguard.context; spendguard.set_context; spendguard.feedback; spendguard.on_event; governing inputs: context(intent,chain,contract,batch_eligible);set_context(attribution and routing metadata);feedback(call_id,ok,source,confidence);on_event(fn) | ABSENT (DELIBERATE): Thread-local scopes and executable callbacks require application runtime access. | [spendguard:context](#core-10eab0d40f277d653a12fc5fec0e329f40168382e68654eea0ef2caa395140da); [spendguard:set_context](#core-5061da829a33afe56a2d03fea757a7084dc935de9270b871b35ef2ce16818780); [spendguard:feedback](#core-f808d412b970120f1f3895bf185b3b418f36529625c8f29a2bfdd4c184e42a2f); [spendguard:on_event](#core-54036d81c75a01380b5bc3b3455ca3a25f749bb4c01d40ff40ea0a9825ff9728) |
| C268 | Guard programmatic job estimates tests realtime and compute | ABSENT (DELIBERATE): Callback/context guard primitives are application integration; user-facing estimate and job workflows are separate. | spendguard.estimate_job; spendguard.test_job; spendguard.check_bulk; spendguard.check_realtime; spendguard.check_compute; spendguard.gated_batch; spendguard.record_estimate; spendguard.record_tested; spendguard.note_response; governing inputs: sig,model,est_usd,est_count;run_fn,verify_fn,contract,items;force,batch_eligible;signature-specific estimate/test metadata,response and model token budget inputs | ABSENT (DELIBERATE): Callback/context guard primitives are application integration; user-facing estimate and job workflows are separate. | [spendguard:estimate_job](#core-2949aa6b7d3fa53339f6b9bbff7a03e6ea9877343472ad6c6c8cefd28643d5e8); [spendguard:test_job](#core-c5253852e0d5836f776d168a4330118aaaa8c9ec643a4a724ad4a9015200503d); [spendguard:check_bulk](#core-c3d2bdd52d48de18859c4ce1c2c706178dd1248bcaddce83072a170b2ee97c9c); [spendguard:check_realtime](#core-a5c0afe9c0cb378e0815f34e17294e35df17b621c6240263bb06c1b1a7a9d573); [spendguard:check_compute](#core-46b52fd209ed0cc3759ecbba0a434eeb944749d967e386a813c5dcfc025609ff); [spendguard:gated_batch](#core-9d11a99806d9cf1a7ffe8ee77adf124b6bbebd44492cbc28aa085416e0fefc04); [spendguard:record_estimate](#core-134f612ebf4c0d0cf2d1dfee925720c0ec1612f8bfa7254cce48c604d768641e); [spendguard:record_tested](#core-e532948b35a88a8c55a1920a4caf8a288e4c78314dcf67497040bd0e70bb04ae); [spendguard:note_response](#core-e8208dfc253ba229a720bb463395c6ec7239adffb6b2fe16754df8714e19ee11) |
| C269 | Persist item-level completeness and bounded retry state | ABSENT (DELIBERATE): Application-controlled item state is an integration primitive consumed by workflow wrappers. | spendguard.ItemLedger; governing inputs: requested,path,job;settle_item(item,state,answer,reason),note_attempt(items),close_exhausted(rounds),reopen(),assert_complete() | ABSENT (DELIBERATE): Application-controlled item state is an integration primitive consumed by workflow wrappers. | [spendguard:ItemLedger](#core-c74fa4882b1bb1ba92145027d474712bef4341003c76ad9241b691bc89869c35) |
| C270 | Guard resume identity and escalation/cancellation policy | ABSENT (DELIBERATE): Caller callbacks, transport cancellation and local resume contracts require in-process integration. | spendguard.compute_job_fingerprint; spendguard.read_fingerprint; spendguard.write_fingerprint; spendguard.compare_fingerprints; spendguard.guard_resume; spendguard.relative_escalation_gate; spendguard.cancel_on_zero_billable; governing inputs: compute_job_fingerprint(task,pack,item_ids,prompt_sample,model);read_fingerprint(path);write_fingerprint(path,fingerprint,detail);compare_fingerprints(recorded,current);guard_resume(path,fingerprint,detail,allow_replan,on_method_change);relative_escalation_gate(baseline,escalation,absolute_cap,quality_bar_met,ratio_limit,baseline_epsilon);cancel_on_zero_billable(run,soft_deadline_s,transport,cancel,zero_billable_proven,provider,model,intent) | ABSENT (DELIBERATE): Caller callbacks, transport cancellation and local resume contracts require in-process integration. | [spendguard:compute_job_fingerprint](#core-5f78d13b4400dd9116a331347d75e6ff95989fd87f43c9c766b6e79e89da8212); [spendguard:read_fingerprint](#core-40ebb5ae4573eb761ddce76a00b6f27bfbfa508c7e7823445a20c6aa9a35d0da); [spendguard:write_fingerprint](#core-74653bbde9b62bcebc0bbc05d3d0e0662bd02ac72f72b90ab9957eba2c4dae7b); [spendguard:compare_fingerprints](#core-c9aad690202bd3424a30246ab26d7c752ccbb86224ab6f13c516c558a26f0c44); [spendguard:guard_resume](#core-6611ff5f3ed0b047fdd2a8f63a1c3d23181931dc9e7e27a015ee081b5faca667); [spendguard:relative_escalation_gate](#core-f0ad74cf6637a87050ae60296fe46b6f663c9988332f94e559ee7fb791fe77b2); [spendguard:cancel_on_zero_billable](#core-a020cbc8fda01e7960eebf5674f9d43c9fe86873762f8a9304c5a7750f2ac7d9) |
| C271 | Read canonical provider-qualified model token rates | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.price; spendguard.pricing.price(model: str, provider: str=None); governing inputs: model,provider | spendguard_models() | [spendguard:price](#core-a2e565b075231747d08516f48a9169b2add159ced190a99bb2cdf7bd94dcd5c9); [spendguard.pricing:price](#core-48684c4a316361c99450b70639660d96ad37238bfa1200244eccf5ff4a6e772d) |
| C272 | Calculate exact token cost including cache read and cache creation | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.batch_cost; spendguard.realtime_cost; governing inputs: model,in_tok,out_tok,cached_in_tok,provider,cache_creation_tok | capability_api_contract_25_spendguard_batch_cost; capability_api_contract_25_spendguard_realtime_cost | [spendguard:batch_cost](#core-6f243e6ee93947337a3de5c0708af43a9403791cad41fde06377898b30e9003e); [spendguard:realtime_cost](#core-cac3a41b9172e9a7bb62d70d1e52228abe149706d7191551360842e00f77eb45) |
| C273 | Normalize catalog model aliases for programmatic integration | ABSENT (DELIBERATE): Alias normalization is internal to public model-taking operations; standalone remote conversion adds no job. | spendguard.normalize; governing inputs: model | ABSENT (DELIBERATE): Alias normalization is internal to public model-taking operations; standalone remote conversion adds no job. | [spendguard:normalize](#core-5c9201587e7a686a27675f71bdd4e5cc94218836415308eb98a030b9e8c1ee4c) |
| C274 | Use full governed bulk routing input controls | ABSENT (DELIBERATE): Full governed bulk routing controls include Python per-task callables and mutable process-local stats. JSON surfaces cannot preserve executable identity and mutation; ordinary JSON task fan is separately exposed by C216. | spendguard.lane_balance.bulk_delegate(tasks, intent, system=None, reasoning=None, max_workers=None, deadline_s=120.0, checkpoint=None, chunk_size=100, refuse_billed=False, stats=None, force=False, tier=None, schema=None, expect_ids=None, gate_sig=None, lanes=None, images_for=None, vision_model=None, model_for=None, prompt_for=None, task_key=None, return_keyed=False, on_miss=None, batch_submit=None, batch_model=None, batch_cap=None, hedge_ms=None, strategy=None, metered_only=False, sla_class=None, record_route=True, budget_usd=None, auth_over_quota=False); governing inputs: schema,expect_ids,reasoning,max_workers,deadline_s,images_for,vision_model,model_for,on_miss,batch_model,batch_cap,strategy,metered_only,sla_class,budget_usd,auth_over_quota | ABSENT (DELIBERATE): Full governed bulk routing controls include Python per-task callables and mutable process-local stats. JSON surfaces cannot preserve executable identity and mutation; ordinary JSON task fan is separately exposed by C216. | [spendguard.lane_balance:bulk_delegate](#core-3a95c6dbcc309be1f46006b559696b715b926059da92d2a097eb45bfe3e99237) |
| C275 | Use OpenAI Batch schema effort sharding metadata and preflight controls | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.submit.submit_chat_tasks(tasks, model, *, system=None, schema=None, reasoning='minimal', max_out=None, cap_dollars=None, submit=True, intent=None, metadata=None, preflight=True, shard_size=None, force=False, avg_out_tokens=None); spendguard.submit.guarded_submit(jsonl_path, model, cap_dollars, batch=True, avg_out_tokens=None, expected_cost=None, submit=True, request_cap=25000, overrun_tolerance=DEFAULT_OVERRUN_TOLERANCE, endpoint='/v1/chat/completions', intent=None, metadata=None, preflight=True, force=False, *, deadline_at: float \| None=None); governing inputs: schema,reasoning,shard_size,metadata,preflight,force,deadline_at | capability_api_contract_28_submit_submit_chat_tasks; capability_api_contract_28_submit_guarded_submit | [spendguard.submit:submit_chat_tasks](#core-a52bbe600cbd0d4aa7b08937f2017069531a9173f2908c75a6ec70d33ed536c5); [spendguard.submit:guarded_submit](#core-a974e1222b3cc14a610664eebd44b8cbc0e24e14506a23b3b5bd00b3ede9cad2) |
| C276 | Register custom provider and inspect resolved provider or base model | ABSENT (DELIBERATE): Runtime provider registration is application integration; provider discovery is covered by catalog surfaces. | spendguard.adapters.register_provider(name, base_url, key_env, prefixes, kind='openai', sdk_import=None); spendguard.adapters.provider_for(model); spendguard.adapters.provider_base_model(provider); governing inputs: name,base_url,key_env,prefixes,kind,sdk_import;model;provider | ABSENT (DELIBERATE): Runtime provider registration is application integration; provider discovery is covered by catalog surfaces. | [spendguard.adapters:register_provider](#core-801cff9bffeebfdc73456e17a8e6031b400b92ce8fdbeff9626c693203fa28a3); [spendguard.adapters:provider_for](#core-b1e49d4dd77e13f8b8c082eaaf9a0a6bbf79f2afb88b83736f3521281e779aca); [spendguard.adapters:provider_base_model](#core-0fcfc826d0da102e10c7b5ee25c322302cb4a4efcf17280371a4196275c8750c) |
| C277 | Interpret response truncation from finish reason and output cap | ABSENT (DELIBERATE): In-process response interpretation is consumed by wrappers; no separate user workflow is needed. | spendguard.is_truncated; governing inputs: finish_reason,out_tok,max_tokens | ABSENT (DELIBERATE): In-process response interpretation is consumed by wrappers; no separate user workflow is needed. | [spendguard:is_truncated](#core-e4f73e0b02341949055b5b7a2808cdefa24c6001630e267bfb996b97803790f0) |
| C278 | Record a quality outcome for a recorded call | ABSENT (DELIBERATE): Programmatic API or MCP contract, not a user-facing CLI command. | spendguard.feedback(call_id, ok=True, source="explicit", confidence=None) | capability_record_quality_feedback_spendguard_feedback | [spendguard:feedback](#core-f808d412b970120f1f3895bf185b3b418f36529625c8f29a2bfdd4c184e42a2f) |
| C279 | Assess versioned project readiness with fail-closed evidence and coverage | selfcheck | capability_readiness_selfcheck_capability_readiness_assess_capability_readiness; capability_readiness_selfcheck_capability_readiness_run_capability_selfcheck | capability_readiness_selfcheck_capability_readiness_assess_capability_readiness; capability_readiness_selfcheck_capability_readiness_run_capability_selfcheck | [spendguard.capability_readiness:assess_capability_readiness](#core-338ee42e079697612ca024dd53e80591dc88fba822e615dfb983a69793a6e9d5); [spendguard.capability_readiness:run_capability_selfcheck](#core-af3c9425d056c61cb4885c201af073c5d2d56a68f9bd8f7356a7276f2f9c647a) |
| C280 | Measure or check nightly ledger hot-path performance against a same-runner committed baseline | bench | ledger_performance_benchmark_ledger_performance_benchmark_run_ledger_performance_benchmark | ledger_performance_benchmark_ledger_performance_benchmark_run_ledger_performance_benchmark | [spendguard.ledger_performance_benchmark:run_ledger_performance_benchmark](#core-f67b31ebb2ff87950b46bd6abb6b2ce5534113fae77503d7eccd3d78893f69b6) |

Counts: ABSENT 102; DELIBERATE 102; PRESENT 738

## Capability parameter contracts

### C001: Converge and attest installed consumers onto the selected distribution

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard distribute --check --dry-run --json --roots --version.

Takes no parameters.

### C002: Read enforcement status and current budget usage

Bind cmd=status; retain install, stdout and exit0. CLI pre-dispatch first-run nudge lives cli.py:510. Audited CLI invocation: spendguard status --live.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| live | bool | no | false | Bind cmd=status; retain install, stdout and exit0. CLI pre-dispatch first-run nudge lives cli.py:510. |

### C003: Enable enforcement for future processes

Bind cmd=on; remove FLAG if exists; retain print and return. Audited CLI invocation: spendguard on (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C004: Disable enforcement for future processes

Bind cmd=off; write disabled flag; retain print and return. Audited CLI invocation: spendguard off (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C005: Diagnose enforcing interpreter, coverage, routing and configuration

Bind cmd=doctor; preserve stale-editable/fleet/startup failure exit logic, cached-default/live-only network and healing distinction, every diagnostic. Audited CLI invocation: spendguard doctor --live.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| live | bool | no | false | Bind cmd=doctor; preserve stale-editable/fleet/startup failure exit logic, cached-default/live-only network and healing distinction, every diagnostic. |

### C006: Inspect and optionally reap orphaned warm subscription servers

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard reap-orphans --bounded-log --execute --json --lane.

Takes no parameters.

### C007: Recover, archive and optionally process stale lane queue work

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard queue-manage --bounded-log --execute --json --no-process --stale-days.

Takes no parameters.

### C008: Verify protected executor work did not leak to other providers

Extract parsed-namespace body; executor/provider both-or-neither; omitted targets=config.shed_lanes; preserve terminal precedence LEAK/PROBE_FAILED/INCONCLUSIVE/CLEAN and optional bounded log. Audited CLI invocation: spendguard lane-leak-check --bounded-log --hours --ledger-path --min-other-calls --protect-executor --protect-provider.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| protect_executor | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract parsed-namespace body; executor/provider both-or-neither; omitted targets=config.shed_lanes; preserve terminal precedence LEAK/PROBE_FAILED/INCONCLUSIVE/CLEAN and optional bounded log. |
| protect_provider | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract parsed-namespace body; executor/provider both-or-neither; omitted targets=config.shed_lanes; preserve terminal precedence LEAK/PROBE_FAILED/INCONCLUSIVE/CLEAN and optional bounded log. |
| hours | float \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "number"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "float \| None", "type": "any"} | no | null | Extract parsed-namespace body; executor/provider both-or-neither; omitted targets=config.shed_lanes; preserve terminal precedence LEAK/PROBE_FAILED/INCONCLUSIVE/CLEAN and optional bounded log. |
| min_other_calls | int \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "integer"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "int \| None", "type": "any"} | no | null | Extract parsed-namespace body; executor/provider both-or-neither; omitted targets=config.shed_lanes; preserve terminal precedence LEAK/PROBE_FAILED/INCONCLUSIVE/CLEAN and optional bounded log. |
| ledger_path | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract parsed-namespace body; executor/provider both-or-neither; omitted targets=config.shed_lanes; preserve terminal precedence LEAK/PROBE_FAILED/INCONCLUSIVE/CLEAN and optional bounded log. |
| bounded_log | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract parsed-namespace body; executor/provider both-or-neither; omitted targets=config.shed_lanes; preserve terminal precedence LEAK/PROBE_FAILED/INCONCLUSIVE/CLEAN and optional bounded log. |

### C009: Read typed ledger records, aggregates and counts

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard ledger-read OPERATION [--table TABLE] [--event-id ID] [--since S] [--until U] [--path P] [--group-by FIELD…] [--conv ID] [--deadline-s S] [--json]. JSON returns preserve Decimal as exact strings, dates and paths as strings, sets as deterministic arrays, and non-string-key mappings as entries containing structured key/value pairs.

Takes no parameters.

### C010: Backfill compression of retained database content

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard compress-content --batch-rows --run.

Takes no parameters.

### C011: Evaluate an agent-spawn PreToolUse payload

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard agent-spawn-gate --pretooluse-hook.

Takes no parameters.

### C012: Evaluate a context-residency PostToolUse payload

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard residency-gate --posttooluse-hook.

Takes no parameters.

### C013: Evaluate weekly-cap enforcement for a PreToolUse payload

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard capguard --pretooluse-hook.

Takes no parameters.

### C014: Install or remove the context-residency hook

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard install-residency-gate --uninstall.

Takes no parameters.

### C015: Read current plan overage state

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard overage.

Takes no parameters.

### C016: Generate and optionally email the multi-window spend report

Extract body after parsing; generate_report + watchdog partial output/exit3 + optional configured email + best-effort SaaS sync. generate_report alone loses behavior. Audited CLI invocation: spendguard report --alert-threshold --email --email-to --max-seconds.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| alert_threshold | float \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "number"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "float \| None", "type": "any"} | no | null | Extract body after parsing; generate_report + watchdog partial output/exit3 + optional configured email + best-effort SaaS sync. generate_report alone loses behavior. |
| email | bool | no | false | Extract body after parsing; generate_report + watchdog partial output/exit3 + optional configured email + best-effort SaaS sync. generate_report alone loses behavior. |
| email_to | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract body after parsing; generate_report + watchdog partial output/exit3 + optional configured email + best-effort SaaS sync. generate_report alone loses behavior. |
| max_seconds | float \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "number"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "float \| None", "type": "any"} | no | null | Extract body after parsing; generate_report + watchdog partial output/exit3 + optional configured email + best-effort SaaS sync. generate_report alone loses behavior. |

### C017: Project route economics for an intent workload

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard route-cost INTENT --n N [--in TOKENS] [--out TOKENS] [--lane L] [--batch-model M] [--json].

Takes no parameters.

### C018: Measure realized route results over a dated window

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard route-realized --intent INTENT [--since S] [--as-of D] [--limit N] [--json].

Takes no parameters.

### C019: Compare lane and batch routing for an urgent or deferrable job horizon

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard route-horizon INTENT:N[:urgent|deferrable]… [--in TOKENS] [--out TOKENS] [--lane L] [--batch-model M] [--json].

Takes no parameters.

### C020: Export ledger spend as FOCUS CSV or JSON with reconciliation

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard focus-export --format --out --since --until.

Takes no parameters.

### C021: Ingest GenAI telemetry spans into the ledger

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard otel-ingest [aliases: otel] --dry-run --project.

Takes no parameters.

### C022: Resolve and optionally correct stale model identifiers

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard preflight --no-correct.

Takes no parameters.

### C023: Check catalog, keys, fallback economics and optional live failover

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard verify --probe.

Takes no parameters.

### C024: Audit code for key usage and gating coverage

Compose config dotenv scan/report and renderer; exit1 on differing shadow or strict any findings; help is presentation. Audited CLI invocation: spendguard keys-audit --json --strict.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| paths | list[str] {"alternatives": [], "enum": [], "items": {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, "properties": [], "python_annotation": "list[str]", "type": "array"} | no | {"source": "src/spendguard/keys_audit.py:cmd", "expression": "nonflag rest or ['.']"} | Compose config dotenv scan/report and renderer; exit1 on differing shadow or strict any findings; help is presentation. |
| strict | bool | no | false | Compose config dotenv scan/report and renderer; exit1 on differing shadow or strict any findings; help is presentation. |
| json | bool | no | false | Compose config dotenv scan/report and renderer; exit1 on differing shadow or strict any findings; help is presentation. |

### C025: Compare provider billing truth against recorded spend

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard reconcile --by-day --estimate --since.

Takes no parameters.

### C026: Project cost for a request count and average token sizes

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard estimate --items N --prefix-tok N --in-per-item N --out-per-item N --models CSV --packs CSV --mode batch|realtime [--assume-cache --cap-dollars USD --label INTENT].

Takes no parameters.

### C027: Audit repository spend integration with optional deep adjudication

Extract scan orchestration over module SCRIPTS/EXCLUDE_DIRS; preserve skipped coverage, optional deep(scanned,PRICING,run), ci-only failures. Audited CLI invocation: spendguard audit --ci --deep --run.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| ci | bool | no | false | Extract scan orchestration over module SCRIPTS/EXCLUDE_DIRS; preserve skipped coverage, optional deep(scanned,PRICING,run), ci-only failures. |
| deep | bool | no | false | Extract scan orchestration over module SCRIPTS/EXCLUDE_DIRS; preserve skipped coverage, optional deep(scanned,PRICING,run), ci-only failures. |
| run | bool | no | false | Extract scan orchestration over module SCRIPTS/EXCLUDE_DIRS; preserve skipped coverage, optional deep(scanned,PRICING,run), ci-only failures. |

### C028: Find and adjudicate hardcoded token caps

Extract scan, stale/unjudged verdict display, optional paid adjudication + verdict save + rescan; preserve exit on unresolved coverage. Audited CLI invocation: spendguard token-caps --judge --scan.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| judge | bool | no | false | Extract scan, stale/unjudged verdict display, optional paid adjudication + verdict save + rescan; preserve exit on unresolved coverage. |
| scan | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract scan, stale/unjudged verdict display, optional paid adjudication + verdict save + rescan; preserve exit on unresolved coverage. |

### C029: Submit capped OpenAI chat Batch tasks or prebuilt requests

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard batch-submit (--tasks PATH | --jsonl PATH) --model MODEL [--system TEXT --system-file PATH --max-out N --cap USD --avg-out N --endpoint PATH --dry-run --intent INTENT --chain ID].

Takes no parameters.

### C030: Collect an OpenAI batch into a result file

Bind existing _fetch_under_context with OpenAI client under calls.context; preserve nonterminal3/failed2, out/error files, refusal to clobber and force backup. Not callio.collect. Audited CLI invocation: spendguard batch-fetch --batch-id --chain --force --intent --out.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| batch_id | str | yes | null | Bind existing _fetch_under_context with OpenAI client under calls.context; preserve nonterminal3/failed2, out/error files, refusal to clobber and force backup. Not callio.collect. |
| out | str | yes | null | Bind existing _fetch_under_context with OpenAI client under calls.context; preserve nonterminal3/failed2, out/error files, refusal to clobber and force backup. Not callio.collect. |
| force | bool | no | false | Bind existing _fetch_under_context with OpenAI client under calls.context; preserve nonterminal3/failed2, out/error files, refusal to clobber and force backup. Not callio.collect. |
| intent | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Bind existing _fetch_under_context with OpenAI client under calls.context; preserve nonterminal3/failed2, out/error files, refusal to clobber and force backup. Not callio.collect. |
| chain | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Bind existing _fetch_under_context with OpenAI client under calls.context; preserve nonterminal3/failed2, out/error files, refusal to clobber and force backup. Not callio.collect. |

### C031: Ask a cross-provider panel under a budget and deadline

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard ask --budget --deadline --json --mode --n --purpose --refuse-billed --require --schema --system --vendors.

Takes no parameters.

### C032: Comprehend a file corpus across subscription lanes

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard comprehend --budget-usd --checkpoint --intent --lanes --max-chars --model --out --question --refuse-billed --run.

Takes no parameters.

### C033: Run the authenticated spendguard HTTP server

Reviewed CLI schema projections bind the existing operator handler; lifecycle operation remains deliberately absent from API/MCP. cli_projection_id identifies the original declaration; defaults with default_source are evaluated only at their existing callsite. Audited CLI invocation: spendguard serve --host --port.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| host | string | no | DEFAULT_HOST | bind host (default 127.0.0.1; a non-loopback host needs a token) |
| port | integer | no | DEFAULT_PORT | Existing serve control --port; source contract src/spendguard/serve.py:162 |

### C034: Run the MCP transport server

Reviewed CLI schema projections bind the existing operator handler; lifecycle operation remains deliberately absent from API/MCP. cli_projection_id identifies the original declaration; defaults with default_source are evaluated only at their existing callsite. Audited CLI invocation: spendguard mcp --host --http --port.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| http | boolean | no | false | serve over HTTP (stdlib, POST /mcp) as one WARM daemon every session CONNECTS to via urllib, instead of spawning a stdio `spendguard mcp` per session. |
| host | string | no | _os.environ.get('SPENDGUARD_MCP_HOST', '127.0.0.1') | Existing mcp control --host; source contract src/spendguard/mcp_server.py:1363 |
| port | integer | no | int(_os.environ.get('SPENDGUARD_MCP_PORT', '4913')) | Existing mcp control --port; source contract src/spendguard/mcp_server.py:1364 |

### C035: Audit and optionally arm consumer workspaces

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard consumer-audit --dry-run --json --no-auto-arm --roots.

Takes no parameters.

### C036: Promote a gated release and converge running consumers

Reviewed CLI schema projections bind the existing operator handler; lifecycle operation remains deliberately absent from API/MCP. cli_projection_id identifies the original declaration; defaults with default_source are evaluated only at their existing callsite. Audited CLI invocation: spendguard deploy --allow-dirty --dry-run --no-auto-arm --no-gate --no-refresh --no-restart --skip-consumer-verify.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| skip_consumer_verify | boolean | no | false | Existing command control; see command contract. |
| no_restart | boolean | no | false | Existing command control; see command contract. |
| no_refresh | boolean | no | false | Existing command control; see command contract. |
| no_gate | boolean | no | false | Existing command control; see command contract. |
| allow_dirty | boolean | no | false | Existing command control; see command contract. |
| dry_run | boolean | no | false | Existing command control; see command contract. |
| no_auto_arm | boolean | no | false | Existing command control; see command contract. |

### C037: Compare candidate models and efforts on measured quality

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard bakeoff INTENT --candidates MODELS [--sample N] [--run] [--budget USD] [--efforts LADDER] [--requirement-aware] [--adjudicator M] [--temperature T] [--top-p P] [--seed N].

Takes no parameters.

### C038: Measure output stability by repeated execution

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard stability INTENT --model M [--prompts-file FILE] [--sample N] [--k N] [--marker TEXT] [--system TEXT].

Takes no parameters.

### C039: Find the cheapest reasoning effort preserving measured quality

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard effort-titrate --adjudicator --budget --efforts --model --requirement-aware --run --sample.

Takes no parameters.

### C040: Check and optionally sync spend metadata backbone

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard metadata --sync.

Takes no parameters.

### C041: Rebuild exact-decimal spend events while preserving charge totals

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard migrate (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C042: Find and adjudicate ungrounded token estimate literals

Extract unruled_and_quotes scan, optional adjudicate_literal+verdict ledger save, rescan and terminal status. Audited CLI invocation: spendguard estimate-literals --judge.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| judge | bool | no | false | Extract unruled_and_quotes scan, optional adjudicate_literal+verdict ledger save, rescan and terminal status. |

### C043: Check estimator assumptions against live execution paths

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard estimate-divergence (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C044: Read available model prices and price verification

Existing main is already no-arg composition; verify_pricing, full table, worked example and return0. Audited CLI invocation: spendguard pricing (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C045: List configured priced providers

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard providers (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C046: Audit per-venv gate capture coverage

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard coverage (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C047: Audit enforcement across discovered interpreters

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard gate-coverage (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C048: Recommend measured max_tokens bound for a call class

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard maxtokens <sig> [current_max] [provider:model].

Takes no parameters.

### C049: Interactively initialize and connect a local installation

Reviewed CLI schema projections bind the existing operator handler; lifecycle operation remains deliberately absent from API/MCP. cli_projection_id identifies the original declaration; defaults with default_source are evaluated only at their existing callsite. Audited CLI invocation: spendguard init --all --chat --connect --local --quick --yes -y.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| all | boolean | no | false | quick if any --quick/--yes/-y; --all/--connect/--local/--chat membership; unknown ignored |
| connect | boolean | no | false | quick if any --quick/--yes/-y; --all/--connect/--local/--chat membership; unknown ignored |
| chat | boolean | no | false | quick if any --quick/--yes/-y; --all/--connect/--local/--chat membership; unknown ignored |
| local | boolean | no | false | quick if any --quick/--yes/-y; --all/--connect/--local/--chat membership; unknown ignored |
| quick | boolean | no | false | quick if any --quick/--yes/-y; --all/--connect/--local/--chat membership; unknown ignored |
| yes | boolean | no | false | quick if any --quick/--yes/-y; --all/--connect/--local/--chat membership; unknown ignored |
| y | boolean | no | false | quick if any --quick/--yes/-y; --all/--connect/--local/--chat membership; unknown ignored |

### C050: Install or remove enforcement in a chosen interpreter

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard install-hook [aliases: gate-venv] --no-pkg --python --uninstall --user --venv.

Takes no parameters.

### C051: Discover and optionally arm ungated host interpreters

Reviewed CLI schema projections bind the existing operator handler; lifecycle operation remains deliberately absent from API/MCP. cli_projection_id identifies the original declaration; defaults with default_source are evaluated only at their existing callsite. Audited CLI invocation: spendguard gate-host --apply.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| apply | boolean | no | false | --apply membership; unknown ignored |

### C052: Install or remove the agent-spawn gate

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard install-agent-gate --uninstall.

Takes no parameters.

### C053: Install or remove the weekly-cap hook

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard install-capguard --uninstall.

Takes no parameters.

### C054: Install or remove scheduled spend reports

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard schedule --daily --remove.

Takes no parameters.

### C055: Deploy assistant skills into a destination

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard install-skills --dest.

Takes no parameters.

### C056: Install assistant governance rules into project or global files

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard install-rule --global --project.

Takes no parameters.

### C057: Install or remove host receipt integrations

Reviewed CLI schema projections bind the existing operator handler; lifecycle operation remains deliberately absent from API/MCP. cli_projection_id identifies the original declaration; defaults with default_source are evaluated only at their existing callsite. Audited CLI invocation: spendguard install-receipts --host --remove.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| host | string | no | null | --host first plus next; absent or missing uses default; --remove membership; claude-code/claude/cc aliases; codex informational branch; unknown host exit1; other tokens ignored |
| remove | boolean | no | false | --host first plus next; absent or missing uses default; --remove membership; claude-code/claude/cc aliases; codex informational branch; unknown host exit1; other tokens ignored |

### C058: Register or remove this MCP server in a client

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard install-mcp --remove.

Takes no parameters.

### C059: Move project attribution or estimate semantic reattribution

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard tag (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C060: Summarize recorded calls by intent

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard calls --intent.

Takes no parameters.

### C061: Backfill provider batch usage into historical run records

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard backfill --intent-map --providers.

Takes no parameters.

### C062: Rank cheapest configurations that held quality for an intent

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard advise [--intent I] [--plan M] [--as-of D].

Takes no parameters.

### C063: Optimize an intent using recorded evidence

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard optimize --intent --limit --per --plan --run.

Takes no parameters.

### C064: Mine quality and cost learnings from recorded runs

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard mine --intent --limit --per --plan --run.

Takes no parameters.

### C065: Reconstruct advisor history from stored runs

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard reconstruct --intent --limit --per --plan --run.

Takes no parameters.

### C066: Recommend cheaper model candidates for an intent

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard recommend [--intent I] [--run].

Takes no parameters.

### C067: Attribute provider usage to projects from conversation segments

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard accounting --run.

Takes no parameters.

### C068: Build and optionally push organization opportunity signals

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard signal --dry.

Takes no parameters.

### C069: Summarize and roll up completed work with optional organization push

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard workdone [aliases: work] --by --push --run --since --summarize.

Takes no parameters.

### C070: Record a sourced price override for one model

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard price --batch-in --batch-out --cached-in --in --out --provider --source --verified.

Takes no parameters.

### C071: Revalue previously unpriced ledger records

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard reprice --apply --model.

Takes no parameters.

### C072: Revalue and synchronize subscription-lane usage

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanevalue [aliases: lane-value] --dry --sync.

Takes no parameters.

### C073: Read available replayable corpus coverage

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard callio-status [aliases: corpus-status] --intent --json.

Takes no parameters.

### C074: Inspect and optionally prune stale Codex shell snapshots

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard codex-gc [aliases: codex-prune] --apply --max-age-days.

Takes no parameters.

### C075: Inspect and garbage collect embedding checkpoints

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard embed-gc --max-age-days DAYS --max-total-gb GB --apply (all optional; dry-run default).

Takes no parameters.

### C076: Review the highest-cost optimization opportunities

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard review --run --top.

Takes no parameters.

### C077: Audit repository prompt-cache opportunities

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard cache-audit [aliases: cacheaudit] --repo.

Takes no parameters.

### C078: Measure prompt-cache behavior on a script or stored intent

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard cache-test [aliases: cachetest] --from-intent --model --n --run --script.

Takes no parameters.

### C079: Build a token-efficient task brief

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard brief --intent --llm --task.

Takes no parameters.

### C080: A/B test cheaper model and prompt variants on stored samples

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard experiment --instruction --intent --model --n --reconsider --run --semantic.

Takes no parameters.

### C081: Read model profiles and learned capability facts

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard models (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C082: Run an adopted winning configuration on production inputs

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard promote --batch --input --instruction --intent --model --n --out --run.

Takes no parameters.

### C083: Read persistent semantic-cache statistics

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard cache-stats [aliases: semcache] (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C084: Deduplicate JSONL requests against persistent cached outputs

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard dedup --input --map --model --out.

Takes no parameters.

### C085: Populate persistent cache from batch request/result files

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard dedup-populate --input --model --results.

Takes no parameters.

### C086: Escalate a prompt through a verified model ladder

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard cascade --intent --ladder --prompt --prompt-file --run.

Takes no parameters.

### C087: Reconcile provider batch truth into the local ledger

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard reconcile-ledger [aliases: ledger-sync, leaks] --since.

Takes no parameters.

### C088: Read a provider/local/server accounting trust verdict

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard trust [aliases: trust-check] --since.

Takes no parameters.

### C089: Compare recorded model prices with provider catalog prices

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard cross-check [aliases: crosscheck] (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C090: Bootstrap advisor evidence from repository and transcripts

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard bootstrap --cap --repo --run --transcripts.

Takes no parameters.

### C091: Validate the historical advisor against stored evidence

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard validate (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C092: Measure adopted-insight savings and optionally publish guarded deltas

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard realized --intent --json --sync.

Takes no parameters.

### C093: Read measured savings, decision and shedding summaries

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard savings [--json].

Takes no parameters.

### C094: Find prompt waste and optionally judge batchability

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard prompts --intent --json --judge-batchable --min-calls --since.

Takes no parameters.

### C095: Read and optionally push provider-truth daily totals

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard truth --push --since.

Takes no parameters.

### C096: Discover local spend and transcript sources

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard sources --days --json.

Takes no parameters.

### C097: Summarize discovered session sources and usage

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard scan --days.

Takes no parameters.

### C098: Run an arbitrary child command with gate bootstrap injected

Reviewed CLI schema projections bind the existing operator handler; lifecycle operation remains deliberately absent from API/MCP. cli_projection_id identifies the original declaration; defaults with default_source are evaluated only at their existing callsite. Audited CLI invocation: spendguard run -- --show.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| show | boolean | no | false | --show anywhere overrides child execution; strip first leading -- separator only; remaining raw argv goes child; empty exit2 |

### C099: Inspect, set and probe lane capability tier assignments

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard tiers --probe.

Takes no parameters.

### C100: Register critical intents that must not be substituted

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard register-critical --list --source.

Takes no parameters.

### C101: Summarize workload spend by provider key fingerprint

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard keys --since. JSON returns preserve Decimal as exact strings, dates and paths as strings, sets as deterministic arrays, and non-string-key mappings as entries containing structured key/value pairs.

Takes no parameters.

### C102: Compare actual model answers for a supplied prompt

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard compare --max-tokens --models --prompt --prompt-file --show --system.

Takes no parameters.

### C103: Synchronize model price records

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard sync-prices [aliases: sync] (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C104: Synchronize served model identifiers from providers

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard sync-catalog (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C105: Synchronize and audit model capability bounds

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard sync-capabilities --audit --dry-run.

Takes no parameters.

### C106: Read cached or refreshed provider balances

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard balances --cached.

Takes no parameters.

### C107: Probe lane/provider reachability with optional remediation

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard reliability --json --notify --remediate --run.

Takes no parameters.

### C108: Read overall lane/provider health and diagnosis

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard health [--run|--no-run] [--timeout_s S] [--json].

Takes no parameters.

### C109: Research and optionally write changed model prices

Extract existing _llm_lookup pricing refresh; requires Anthropic key; paid lookup even preview, diff, optional validated _write and status. Default model is source declared, never invented. Audited CLI invocation: spendguard refresh-prices [aliases: refresh] --model --write.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | str | no | {"source": "src/spendguard/refresh.py:main", "expression": "argparse --model default"} | Extract existing _llm_lookup pricing refresh; requires Anthropic key; paid lookup even preview, diff, optional validated _write and status. Default model is source declared, never invented. |
| write | bool | no | false | Extract existing _llm_lookup pricing refresh; requires Anthropic key; paid lookup even preview, diff, optional validated _write and status. Default model is source declared, never invented. |

### C110: Check stale model-price evidence

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard check-prices [aliases: freshness] (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

### C111: Predict calibrated cost for a labeled workload

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard calibrate predict; governing flags: see command inventory `calibrate`.

Takes no parameters.

### C112: Read calibration cell evidence

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard calibrate show; governing flags: see command inventory `calibrate`.

Takes no parameters.

### C113: Pair recorded predictions with realized costs

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard calibrate pair; governing flags: see command inventory `calibrate`.

Takes no parameters.

### C114: Push shared calibration evidence

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard calibrate push; governing flags: see command inventory `calibrate`.

Takes no parameters.

### C115: Fetch shared calibration evidence

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard calibrate fetch; governing flags: see command inventory `calibrate`.

Takes no parameters.

### C116: Backtest calibration against historical predictions

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard calibrate backtest; governing flags: see command inventory `calibrate`.

Takes no parameters.

### C117: Inspect one recorded quality measurement

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard measurement inspect; governing flags: see command inventory `measurement`.

Takes no parameters.

### C118: List quality measurements for an intent

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard measurement list; governing flags: see command inventory `measurement`.

Takes no parameters.

### C119: Reconstruct a quality measurement from stored results

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard measurement reconstruct; governing flags: see command inventory `measurement`.

Takes no parameters.

### C120: Rerun a measurement with controlled sample freshness and budget

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard measurement rerun; governing flags: see command inventory `measurement`.

Takes no parameters.

### C121: Compare two recorded quality measurements

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard measurement compare; governing flags: see command inventory `measurement`.

Takes no parameters.

### C122: Read organization connection readiness

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard saas status; governing flags: see command inventory `saas`.

Takes no parameters.

### C123: Test organization server liveness

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard saas ping; governing flags: see command inventory `saas`.

Takes no parameters.

### C124: Push spend, insights and completed-work rollups to the organization

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard saas sync; governing flags: see command inventory `saas`.

Takes no parameters.

### C125: Push organization spend rollup with an optional dry preview

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard saas push; governing flags: see command inventory `saas`.

Takes no parameters.

### C126: Reconcile provider batch truth into organization-facing local ledger

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard saas reconcile; governing flags: see command inventory `saas`.

Takes no parameters.

### C127: Repair historically misassigned provider attribution

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard saas reattribute; governing flags: see command inventory `saas`.

Takes no parameters.

### C128: Audit completeness of provider batch usage capture

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard saas audit; governing flags: see command inventory `saas`.

Takes no parameters.

### C129: Compare local actual-dollar ledger with organization server rows

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard saas crosscheck; governing flags: see command inventory `saas`.

Takes no parameters.

### C130: Link this installation to an organization using device consent

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard saas link; governing flags: see command inventory `saas`.

Takes no parameters.

### C131: Run organization-requested accounting operations locally

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard saas commands; governing flags: see command inventory `saas`.

Takes no parameters.

### C132: Pull scrubbed organization learnings as low-trust priors

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard saas pull; governing flags: see command inventory `saas`.

Takes no parameters.

### C133: Capture live GPU instance history before instance destruction

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard resources snapshot; governing flags: see command inventory `resources`.

Takes no parameters.

### C134: Synchronize attributed GPU consumption with organization accounting

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard resources sync; governing flags: see command inventory `resources`.

Takes no parameters.

### C135: Recover GPU lifecycles from transcripts with estimate-first adjudication

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard resources discover --agentic; governing flags: see command inventory `resources`.

Takes no parameters.

### C136: Recover GPU instance records from recorded provider data

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard resources discover; governing flags: see command inventory `resources`.

Takes no parameters.

### C137: Read GPU consumption and account residual reconciliation

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard resources show; governing flags: see command inventory `resources`.

Takes no parameters.

### C138: Generate a remote host bootstrap snippet

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard remote onstart; governing flags: see command inventory `remote`.

Takes no parameters.

### C139: Verify enforcement inside a remote interpreter

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard remote verify; governing flags: see command inventory `remote`.

Takes no parameters.

### C140: Import a remote host ledger with project attribution

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard remote sync; governing flags: see command inventory `remote`.

Takes no parameters.

### C141: Synchronize all configured remote hosts

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard remote sync-all; governing flags: see command inventory `remote`.

Takes no parameters.

### C142: Recover historical run intent labels from repository artifacts

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard mine-history intents; governing flags: see command inventory `mine-history`.

Takes no parameters.

### C143: Enrich historical run and insight graph relationships

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard mine-history graph; governing flags: see command inventory `mine-history`.

Takes no parameters.

### C144: Mine quality evidence from repository commit history

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard mine-history git; governing flags: see command inventory `mine-history`.

Takes no parameters.

### C145: Index transcript evidence for historical runs

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard mine-conv index; governing flags: see command inventory `mine-conv`.

Takes no parameters.

### C146: Synthesize cost lessons from transcript evidence

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard mine-conv synth; governing flags: see command inventory `mine-conv`.

Takes no parameters.

### C147: Read effective spendguard configuration

Extract show/list SETTINGS iteration, _resolve_setting precedence and secret masking; includes source/store paths. Audited CLI invocation: spendguard config [show|list].

Takes no parameters.

### C148: Update one effective configuration setting

Extract set branch: SETTINGS lookup/suggestions, _coerce by kind, _write_store, _resolve_setting after write, secret masking. Existing CLI raw joins trailing tokens with spaces. Audited CLI invocation: spendguard config set <dotted.key> <value>; governing flags: see command inventory `config`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| dotted | str | yes | null | Extract set branch: SETTINGS lookup/suggestions, _coerce by kind, _write_store, _resolve_setting after write, secret masking. Existing CLI raw joins trailing tokens with spaces. |
| raw | str | yes | null | Extract set branch: SETTINGS lookup/suggestions, _coerce by kind, _write_store, _resolve_setting after write, secret masking. Existing CLI raw joins trailing tokens with spaces. |

### C149: Deregister one interpreter from the serving fleet

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard fleet deregister <python>; governing flags: see command inventory `fleet`.

Takes no parameters.

### C150: List registered serving consumers

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard fleet consumers; governing flags: see command inventory `fleet`.

Takes no parameters.

### C151: Inspect or restart selected supervised consumers

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard fleet restart; governing flags: see command inventory `fleet`.

Takes no parameters.

### C152: Stop one supervised consumer

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard fleet stop --consumer ID; governing flags: see command inventory `fleet`.

Takes no parameters.

### C153: Read fleet freshness and release alignment

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard fleet; governing flags: see command inventory `fleet`.

Takes no parameters.

### C154: Build a monthly provider-truth close view

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard close --month MONTH; governing flags: see command inventory `close`.

Takes no parameters.

### C155: Export a monthly close statement as CSV

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard close --month MONTH --csv PATH; governing flags: see command inventory `close`.

Takes no parameters.

### C156: Read shared provider account reconciliation axis

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard close --account; governing flags: see command inventory `close`.

Takes no parameters.

### C157: Delegate a self-contained task to another provider plan

Governed delegate dry-run is default. Execute with --yes or --execute; --warm requests the existing broker. CLI jobs survive launcher exit. Use --status JOB_ID or --collect JOB_ID; state is the canonical lifecycle key (running/done/failed/killed), while status preserves legacy execution semantics on collected results. --collect --wait [--timeout-s SECONDS] blocks until terminal; timeout leaves work running. --cancel JOB_ID terminates and --kill JOB_ID force-kills the governed worker. Result text uses stdout; diagnostics and receipts use stderr; --mixed-stream restores legacy output.

Takes no parameters.

### C158: Read delegated job progress

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard delegate --status JOB_ID.

Takes no parameters.

### C159: Collect a delegated job result

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard delegate --collect JOB_ID.

Takes no parameters.

### C160: Recover an interrupted delegated execution

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard delegate --recover EXECUTION_ID; governing flags: see command inventory `delegate`.

Takes no parameters.

### C161: Build the distribution surface manifest

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard manifest build; governing flags: see command inventory `manifest`.

Takes no parameters.

### C162: Read the persisted distribution surface manifest

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard manifest show; governing flags: see command inventory `manifest`.

Takes no parameters.

### C163: List scrubbed local cost learnings

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard insights list; governing flags: see command inventory `insights`.

Takes no parameters.

### C164: Export scrubbed cost learnings to a file

Extract _shareable(min_conf, require_active=not all), scrub/generalize, preview, optional schema envelope JSON write. Audited CLI invocation: spendguard insights export; governing flags: see command inventory `insights`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| out | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract _shareable(min_conf, require_active=not all), scrub/generalize, preview, optional schema envelope JSON write. |
| min_conf | float | no | {"source": "src/spendguard/share.py:cmd_export", "expression": "argparse --min-conf default"} | Extract _shareable(min_conf, require_active=not all), scrub/generalize, preview, optional schema envelope JSON write. |
| all | bool | no | false | Extract _shareable(min_conf, require_active=not all), scrub/generalize, preview, optional schema envelope JSON write. |

### C165: Import cost learnings with explicit trust handling

Extract dict-envelope/list parse and add_insight candidate community entries; preserve confidence zero/clamping/trust cap and existing lesson500 truncation. Audited CLI invocation: spendguard insights import PATH; governing flags: see command inventory `insights`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| path | str | yes | null | Extract dict-envelope/list parse and add_insight candidate community entries; preserve confidence zero/clamping/trust cap and existing lesson500 truncation. |
| trust | float | no | {"source": "src/spendguard/share.py:cmd_import", "expression": "argparse --trust default"} | Extract dict-envelope/list parse and add_insight candidate community entries; preserve confidence zero/clamping/trust cap and existing lesson500 truncation. |

### C166: Read coding-session usage and subscription-value report

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code show; governing flags: see command inventory `claude-code`.

Takes no parameters.

### C167: Rebuild coding-session usage from local transcripts

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code --rebuild; governing flags: see command inventory `claude-code`.

Takes no parameters.

### C168: Synchronize attributed coding-session usage to the organization

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code sync; governing flags: see command inventory `claude-code`.

Takes no parameters.

### C169: Ingest coding-session events into the spend ledger

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code ingest; governing flags: see command inventory `claude-code`.

Takes no parameters.

### C170: Reconcile subscription overflow and billing state

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code overflow; governing flags: see command inventory `claude-code`.

Takes no parameters.

### C171: Read measured coding-plan overage tally

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code overage; governing flags: see command inventory `claude-code`.

Takes no parameters.

### C172: Read one conversation context trajectory and residency decomposition

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code context --conv ID [--limit N].

Takes no parameters.

### C173: Rank conversation costs and compaction opportunities

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code conversations [--limit N]; spendguard claude-code attribute [--limit N].

Takes no parameters.

### C174: Ingest coding-plan invoice data

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code invoices; governing flags: see command inventory `claude-code`.

Takes no parameters.

### C175: Attribute coding-plan overage to conversations

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code attribute; governing flags: see command inventory `claude-code`.

Takes no parameters.

### C176: Generate a tailored compaction prompt from a conversation

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code compact; governing flags: see command inventory `claude-code`.

Takes no parameters.

### C177: Classify coding-session project and organization attribution

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code classify; governing flags: see command inventory `claude-code`.

Takes no parameters.

### C178: Read coding-session completed-work rollup

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code work; governing flags: see command inventory `claude-code`.

Takes no parameters.

### C179: Summarize coding-session work narrative

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code story; governing flags: see command inventory `claude-code`.

Takes no parameters.

### C180: Read Codex subscription usage report

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard codex show; governing flags: see command inventory `codex`.

Takes no parameters.

### C181: Rebuild Codex usage from transcript sources

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard codex --rebuild; governing flags: see command inventory `codex`.

Takes no parameters.

### C182: Classify Codex session attribution

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard codex classify; governing flags: see command inventory `codex`.

Takes no parameters.

### C183: Synchronize Codex usage to organization accounting

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard codex sync; governing flags: see command inventory `codex`.

Takes no parameters.

### C184: Read refreshed browser-chat subscription usage

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard chat show; governing flags: see command inventory `chat`.

Takes no parameters.

### C185: Read browser-chat usage by project

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard chat show --by-project; governing flags: see command inventory `chat`.

Takes no parameters.

### C186: Diagnose browser-chat source connectivity

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard chat test; governing flags: see command inventory `chat`.

Takes no parameters.

### C187: Consent to browser-chat attribution ingestion

Promote unchanged; bind on=True; config.save_config and opt-in message. Audited CLI invocation: spendguard chat enable; governing flags: see command inventory `chat`.

Takes no parameters.

### C188: Revoke browser-chat attribution ingestion consent

Promote unchanged; bind on=False. Audited CLI invocation: spendguard chat disable; governing flags: see command inventory `chat`.

Takes no parameters.

### C189: Read browser-chat attribution consent and source state

Promote unchanged; pending org request/local conversation cache/taxonomy state, best-effort SaaS state. Audited CLI invocation: spendguard chat status; governing flags: see command inventory `chat`.

Takes no parameters.

### C190: Accept the organization attribution taxonomy

Promote unchanged; enable, clear pending request, pull taxonomy; deferred pull errors still returns0. Audited CLI invocation: spendguard chat accept; governing flags: see command inventory `chat`.

Takes no parameters.

### C191: Push the local attribution taxonomy to the organization

Extract push-taxonomy branch; saas.push_taxonomy, print prefixed result, return0. Audited CLI invocation: spendguard chat push-taxonomy; governing flags: see command inventory `chat`.

Takes no parameters.

### C192: Discover project taxonomy from browser-chat usage

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard chat discover; governing flags: see command inventory `chat`.

Takes no parameters.

### C193: Classify browser-chat conversations into project allocations

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard chat classify; governing flags: see command inventory `chat`.

Takes no parameters.

### C194: Run browser-chat attribution convergence loop

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard chat loop; governing flags: see command inventory `chat`.

Takes no parameters.

### C195: Synchronize browser-chat usage to the organization

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard chat sync; governing flags: see command inventory `chat`.

Takes no parameters.

### C196: Read browser-chat completed-work rollup

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard chat work; governing flags: see command inventory `chat`.

Takes no parameters.

### C197: Summarize browser-chat completed-work narrative

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard chat story; governing flags: see command inventory `chat`.

Takes no parameters.

### C198: Read subscription-lane activation and readiness

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes; governing flags: see command inventory `lanes`.

Takes no parameters.

### C199: Declare a validated subscription lane model assignment

Extract set-model branch; registered lane/model/priced validation, preserve other tiers on tier update, scalar replacement otherwise; _write_advisor_cfg and served-tier print. Audited CLI invocation: spendguard lanes set-model LANE [TIER] MODEL; governing flags: see command inventory `lanes`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| lane | str | yes | null | Extract set-model branch; registered lane/model/priced validation, preserve other tiers on tier update, scalar replacement otherwise; _write_advisor_cfg and served-tier print. |
| model | str | yes | null | Extract set-model branch; registered lane/model/priced validation, preserve other tiers on tier update, scalar replacement otherwise; _write_advisor_cfg and served-tier print. |
| tier | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract set-model branch; registered lane/model/priced validation, preserve other tiers on tier update, scalar replacement otherwise; _write_advisor_cfg and served-tier print. |

### C200: Probe enabled subscription lanes with bounded prompts

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --probe; governing flags: see command inventory `lanes`.

Takes no parameters.

### C201: Read subscription quota headroom and measured quota cost

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --usage; governing flags: see command inventory `lanes`.

Takes no parameters.

### C202: Recover formerly unserved lane-model combinations

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --reprobe; governing flags: see command inventory `lanes`.

Takes no parameters.

### C203: Estimate or adjudicate avoidable metered intent usage

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --judge-eligibility; governing flags: see command inventory `lanes`.

Takes no parameters.

### C204: Read served lane model and fallback catalog

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --catalog; governing flags: see command inventory `lanes`.

Takes no parameters.

### C205: Read measured cross-lane bandit learning

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --learn; governing flags: see command inventory `lanes`.

Takes no parameters.

### C206: Run a cross-lane bakeoff and record its winner

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --bakeoff INTENT TASK; governing flags: see command inventory `lanes`.

Takes no parameters.

### C207: Estimate cross-lane bakeoff judge cost without spending

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --estimate; governing flags: see command inventory `lanes`.

Takes no parameters.

### C208: Read subscription plan utilization balance

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --balance; governing flags: see command inventory `lanes`.

Takes no parameters.

### C209: Read measured lane token economics and fee at risk

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --economics; governing flags: see command inventory `lanes`.

Takes no parameters.

### C210: Read lane-to-metered fallback model mappings

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --fallback; governing flags: see command inventory `lanes`.

Takes no parameters.

### C211: Read actual metered spending caused by lane failures

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --fallback-spend; governing flags: see command inventory `lanes`.

Takes no parameters.

### C212: Read lane-to-metered reasoning equivalence

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --reasoning-map; governing flags: see command inventory `lanes`.

Takes no parameters.

### C213: Propose acceptable model substitutions pending confirmation

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --propose INTENT MODEL; governing flags: see command inventory `lanes`.

Takes no parameters.

### C214: Confirm a proposed model substitution

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --confirm INTENT MODEL; governing flags: see command inventory `lanes`.

Takes no parameters.

### C215: Run a one-shot task on a viable idle subscription lane

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --delegate TASK; governing flags: see command inventory `lanes`.

Takes no parameters.

### C216: Execute generic resilient concurrent task fan

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --bulk INTENT [--file PATH --jsonl --system TEXT --system-file PATH --tier GROUP --lanes CSV --hedge-ms N --estimate --refuse-billed --checkpoint PATH --out PATH --force].

Takes no parameters.

### C217: Estimate a confined bulk fan and fallback ceiling

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --bulk INTENT --estimate; governing flags: see command inventory `lanes`.

Takes no parameters.

### C218: Durably enqueue subscription-lane tasks

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --enqueue INTENT; governing flags: see command inventory `lanes`.

Takes no parameters.

### C219: Drain durable tasks onto idle subscription lanes

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --drain; governing flags: see command inventory `lanes`.

Takes no parameters.

### C220: Read durable subscription-lane queue depth

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --queue; governing flags: see command inventory `lanes`.

Takes no parameters.

### C221: Archive and purge old terminal lane-queue records

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --purge; governing flags: see command inventory `lanes`.

Takes no parameters.

### C222: Migrate lane-queue storage out of the spend ledger

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard lanes --migrate-queue-db; governing flags: see command inventory `lanes`.

Takes no parameters.

### C223: Read aggregate admission and execution queue state

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard dispatch [--json]; spendguard dispatch --id ID [--json].

Takes no parameters.

### C224: Read the next admission queue plan

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard plan-queue [--watch] [--interval S] [--ticks N] [--json].

Takes no parameters.

### C225: Watch queue plans at a bounded or continuous cadence

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard plan-queue --watch; governing flags: see command inventory `plan-queue`.

Takes no parameters.

### C226: Read split real-dollar and subscription-value receipt

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard receipt; governing flags: see command inventory `receipt`.

Takes no parameters.

### C227: Read receipt allocation by intent

Promote typed window inputs; preserve month default, today>week>all, explicit since overrides, budget.by_intent exact countable filter, cost order and JSON/text; receipt.cli upfront lane-value refresh remains existing CLI behavior. Audited CLI invocation: spendguard receipt --by-intent; governing flags: see command inventory `receipt`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| today | bool | no | false | Promote typed window inputs; preserve month default, today>week>all, explicit since overrides, budget.by_intent exact countable filter, cost order and JSON/text; receipt.cli upfront lane-value refresh remains existing CLI behavior. |
| week | bool | no | false | Promote typed window inputs; preserve month default, today>week>all, explicit since overrides, budget.by_intent exact countable filter, cost order and JSON/text; receipt.cli upfront lane-value refresh remains existing CLI behavior. |
| all | bool | no | false | Promote typed window inputs; preserve month default, today>week>all, explicit since overrides, budget.by_intent exact countable filter, cost order and JSON/text; receipt.cli upfront lane-value refresh remains existing CLI behavior. |
| since | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Promote typed window inputs; preserve month default, today>week>all, explicit since overrides, budget.by_intent exact countable filter, cost order and JSON/text; receipt.cli upfront lane-value refresh remains existing CLI behavior. |
| json | bool | no | false | Promote typed window inputs; preserve month default, today>week>all, explicit since overrides, budget.by_intent exact countable filter, cost order and JSON/text; receipt.cli upfront lane-value refresh remains existing CLI behavior. |

### C228: Render compact receipt statusline

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard receipt --line; governing flags: see command inventory `receipt`.

Takes no parameters.

### C229: Emit receipt through an assistant lifecycle hook

Reviewed CLI schema projections bind the existing operator handler; lifecycle operation remains deliberately absent from API/MCP. cli_projection_id identifies the original declaration; defaults with default_source are evaluated only at their existing callsite. Audited CLI invocation: spendguard receipt --stop-hook|--precompact-hook|--sessionstart-hook; governing flags: see command inventory `receipt`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| stop_hook | boolean | no | false | ordered --by-intent first then --json then --stop-hook then --precompact-hook then --sessionstart-hook then --statusline then --line; default scope --all wins --org first-plus-next else conversation org; by-intent window today before week before all, then first --since with next overrides; unknown ignored; lane-value refresh before branch except statusline |
| precompact_hook | boolean | no | false | ordered --by-intent first then --json then --stop-hook then --precompact-hook then --sessionstart-hook then --statusline then --line; default scope --all wins --org first-plus-next else conversation org; by-intent window today before week before all, then first --since with next overrides; unknown ignored; lane-value refresh before branch except statusline |
| sessionstart_hook | boolean | no | false | ordered --by-intent first then --json then --stop-hook then --precompact-hook then --sessionstart-hook then --statusline then --line; default scope --all wins --org first-plus-next else conversation org; by-intent window today before week before all, then first --since with next overrides; unknown ignored; lane-value refresh before branch except statusline |
| all | boolean | no | false | ordered --by-intent first then --json then --stop-hook then --precompact-hook then --sessionstart-hook then --statusline then --line; default scope --all wins --org first-plus-next else conversation org; by-intent window today before week before all, then first --since with next overrides; unknown ignored; lane-value refresh before branch except statusline |
| org | string | no | null | ordered --by-intent first then --json then --stop-hook then --precompact-hook then --sessionstart-hook then --statusline then --line; default scope --all wins --org first-plus-next else conversation org; by-intent window today before week before all, then first --since with next overrides; unknown ignored; lane-value refresh before branch except statusline |

### C230: Read provider token-count calibration factors

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard tokens show; governing flags: see command inventory `tokens`.

Takes no parameters.

### C231: Calibrate provider token-count estimates

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard tokens calibrate; governing flags: see command inventory `tokens`.

Takes no parameters.

### C232: Recover provider batch prompt/output samples

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard fetch-io; governing flags: see command inventory `fetch-io`.

Takes no parameters.

### C233: Link recovered samples to transcript conversations

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard fetch-io --link-only; governing flags: see command inventory `fetch-io`.

Takes no parameters.

### C234: Inspect suspicious ledger charges

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard quarantine; governing flags: see command inventory `quarantine`.

Takes no parameters.

### C235: Quarantine a selected impossible ledger charge

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard quarantine --row ID; governing flags: see command inventory `quarantine`.

Takes no parameters.

### C236: Plan a whole job set under budget and urgency

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard submit-jobs FILE; governing flags: see command inventory `submit-jobs`.

Takes no parameters.

### C237: Execute a whole job set under the same planner

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard run-jobs JOBS_JSONL [--budget USD] [--urgency auto|realtime|batch] [--quality] [--execute]; spendguard run-jobs --collect PENDING_JSONL.

Takes no parameters.

### C238: Collect submitted whole-job batch results

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard submit-jobs --collect PENDING_JSONL; governing flags: see command inventory `submit-jobs`.

Takes no parameters.

### C239: Read serving and green release alignment

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard version.

Takes no parameters.

### C240: Attest release convergence across consumers

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard release --check; governing flags: see command inventory `release`.

Takes no parameters.

### C241: Read canonical actionable catalogue and provider-qualified token prices

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C242: Run ONE metered VISION call (image(s) + prompt) through spendguard's governed adapter. The subscription lanes are text-only CLIs, so vision rides the metered API (executor='api'), pixel-priced and bounded by a deadline. Estimate-first: with NO budget_usd it returns ONLY the estimate ($0); pass budget_usd to run (refused over it). A dynamic-key MAP schema is refused before any spend (strict mode empties it to {} — use an ARRAY of {key,value}); paid results are cached by request content, so an identical re-request is $0.

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C243: The headline for THIS account: REAL $ out the door (subscription base + Claude Code overage + API credits) shown SEPARATELY from est-value (plan-covered Claude Code usage, what it's worth at API rates). The two axes are never summed. $0, read-only.

Promote unchanged; read_cc_spend_overview + guard.saved_since/decisions_summary preserving separate real$/estimated-value/savings axes.

Takes no parameters.

### C244: Cost of ONE conversation by its transcript id: plan-covered est-value + observed overage upper bound (the two axes kept separate). Get ids from spendguard_top_conversations. $0, read-only.

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C245: Open conversations that are expensive to keep alive — large re-read context billing every turn — with the $/turn cost, what compacting would save, and the ready-to-paste effective /compact command. Surfaces WHEN compacting a conversation is a good trade. $0, read-only.

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard claude-code context [--limit N]; spendguard claude-code compact.

Takes no parameters.

### C246: Estimate, budget-gate the estimate, and cache one governed image request

Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | str | yes | required | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| prompt | str | yes | required | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| images | list[str] {"alternatives": [], "enum": [], "items": {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, "properties": [], "python_annotation": "list[str]", "type": "array"} | yes | required | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| schema | dict \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "object"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "dict \| None", "type": "any"} | no | null | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| system | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| budget_usd | float \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "number"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "float \| None", "type": "any"} | no | null | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| max_out_tokens | int \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "integer"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "int \| None", "type": "any"} | no | null | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| no_cache | bool | no | false | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| timeout_s | int \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "integer"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "int \| None", "type": "any"} | no | null | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |

### C247: Constrain recommendation count, quality bar and one-call estimate budget

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C248: Choose whether endpoint health generates cached agentic remediation

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. Audited CLI invocation: spendguard reliability --run [--remediate] [--notify].

Takes no parameters.

### C249: Choose metered realtime model for urgent horizon overflow

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C250: Supply caller-owned prompts instead of sampling recorded corpus

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C251: Execute measured best-value model and effort selection on a real call

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C252: Execute a governed single text call with explicit caller controls

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C253: Constrain actual executor and forbid substitutions or billed fallbacks

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C254: Set generation controls and inspect whether each applied

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. JSON returns preserve Decimal as exact strings, dates and paths as strings, sets as deterministic arrays, and non-string-key mappings as entries containing structured key/value pairs.

Takes no parameters.

### C255: Inspect actual executor provider model and substitution provenance

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions. JSON returns preserve Decimal as exact strings, dates and paths as strings, sets as deterministic arrays, and non-string-key mappings as entries containing structured key/value pairs.

Takes no parameters.

### C256: Generate gated realtime embeddings with resumable checkpoint

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C257: Submit capped embedding Batch API work

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C258: Compare embedding quality and cost on same texts

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C259: Submit Anthropic native Messages Batch

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C260: Collect all OpenAI chat Batch results with explicit pending/error states

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C261: Collect all Anthropic native Messages Batch results

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C262: Issue typed deadline-bounded vendor call

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C263: Build provider-native strict JSON schema request

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C264: Set scoped generation defaults

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C265: Compute model-safe input budget and measured call deadline

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C266: Install and require runtime SDK gate; register interceptor

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C267: Attach contextual attribution and feedback to direct calls

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C268: Guard programmatic job estimates tests realtime and compute

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C269: Persist item-level completeness and bounded retry state

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C270: Guard resume identity and escalation/cancellation policy

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C271: Read canonical provider-qualified model token rates

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C272: Calculate exact token cost including cache read and cache creation

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C273: Normalize catalog model aliases for programmatic integration

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C274: Use full governed bulk routing input controls

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C275: Use OpenAI Batch schema effort sharding metadata and preflight controls

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C276: Register custom provider and inspect resolved provider or base model

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C277: Interpret response truncation from finish reason and output cap

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C278: Record a quality outcome for a recorded call

Callable variants preserve each exact existing source signature, annotation (Any when absent), unevaluated default expression and full docstring. Source declarations are not inferred cross-surface semantic conversions.

Takes no parameters.

### C279: Assess versioned project readiness with fail-closed evidence and coverage

New readiness capability; CLI exit status is declared on its callable variant. API and MCP expose the structured assessment and printable selfcheck contracts.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| repo_root | str \| None | no | None | Repository checkout whose deterministic readiness gates and source fingerprint are assessed; omitted uses the current working directory. |
| environment_report | str \| None | no | None | Path to independently produced clean-environment verification evidence; absent evidence remains UNREVIEWED. |
| tolerate_unreviewed | tuple[str, ...] {"alternatives": [], "enum": [], "items": {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, "properties": [], "python_annotation": "tuple[str, ...]", "type": "array"} | no | () | Repeatable readiness check identifier whose UNREVIEWED result does not make the process exit nonzero; rendering is unchanged and FAIL is never tolerated. |
| json_output | bool | no | False | Print the complete structured readiness report instead of the human-readable checklist; does not alter readiness or exit status. |

### C280: Measure or check nightly ledger hot-path performance against a same-runner committed baseline

Use bench --record to commit p50/p95/p99 hot-read and record_event latency, data-flow throughput, provenance, and load-profile evidence for the current runner_class. Use bench --check to compare the same operations only with that runner_class baseline. --iterations selects retained samples after --warmups; --volume controls only the synthetic fallback when spend_events/calls metadata is empty. Latency ceilings are baseline multiplied by latency_multiplier; the throughput floor is baseline divided by throughput_divisor. A missing runner_class baseline or threshold breach fails closed. Timing is offline and never calls a model.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| record | bool | no | False | Measure and commit latency, throughput, provenance, and load-profile evidence for the current runner_class. |
| check | bool | no | False | Measure and fail when a metric breaches its same-runner_class baseline and configured relative tolerance. |
| iterations | int | no | LEDGER_PERFORMANCE_DEFAULT_ITERATIONS | Measured iterations per hot operation after warmup. |
| warmups | int | no | LEDGER_PERFORMANCE_DEFAULT_WARMUPS | Warmup iterations discarded before measurement. |
| volume | int | no | LEDGER_PERFORMANCE_DEFAULT_VOLUME | Synthetic fallback volume used only when spend_events and calls have no retrospective metadata. |

## Programmatic core contracts

<a id="core-a03c9815845a6ed20a964a37c103c6b63fe691be8ac9e6050aa38130f17c9db5"></a>

### `spendguard.adapters:call`

Source: `src/spendguard/adapters.py:931`

Run one one-shot prompt through the governed model/lane router. Returns a CallResult (never raises).

For a classification, gate, or doctrine verdict, pass a stable caller ``intent`` and explicit
``reasoning="best-value"``: measured quality evidence chooses the cheapest qualifying (model, effort), the matching
configured subscription lane is tried first, and its metered API twin is the fallback unless disabled. The caller
names the job intent rather than inventing a model id; configured lane ids come from ``advisor.lane_models`` and
are exposed by ``spendguard lanes --catalog``. Codex/Kimi use default-on warm daemons (Codex measured ~5s
warm; >75s is cold ``codex exec`` only). See README.md's routing cheat-sheet.

A CallResult IS a dict — every `r["text"]` / `r.get(...)` works unchanged — that ALSO exposes its keys as
ATTRIBUTES, so `r.text` / `getattr(r, "text")` returns the text. The bare-dict version had no `.text` attribute,
so `getattr(r, "text", None) or r` silently yielded the whole dict (a caller then parsed it as an empty answer).

`governed=True` (a kwarg carried via **aliases) runs THIS call inside the dispatch GOVERNOR — for a caller
fanning many concurrent calls (a bakeoff / review panel). It bounds per-lane/vendor in-flight, and on a
saturated $0 lane SHEDS to that lane's metered twin instead of a per-call metered spill (the shared
dispatch.admit brain, identical to vendor_call.call); a saturated metered vendor (or no_metered_fallback)
returns a TYPED deadline dict. `timeout_s` is the queue-wait bound (derived from measured latency if omitted).
Same dict return, same no_substitution/intent/metered_only semantics. A SINGLE call omits governed (no overhead).

`metered_only=True` FORCES the paid metered API and SKIPS the subscription lane entirely (the opt-in metered
HALF of the atomic lane→metered pair). Use it for a consistency-sensitive fan (a verdict-cached refuter) that
needs the concurrency-invariant metered path serial-equivalently — where riding the $0 lane CLI (its own
reasoning scale + warm-daemon concurrency) would drift the verdict. Default False = the lane serves first and a
miss falls back to the metered API (the cheap, faithful-for-capability default). A vision call (`images=`) is
metered by construction regardless.

`measurement=True` declares that WHICH MODEL answers IS the result — a bakeoff, a cross-vendor panel, a priced
A/B — and so IMPLIES no_substitution: the named model answers or the call errors, never a silent lane/bandit swap
to a different (often ~100x) model that would attribute the number to the wrong model. It is an EXPLICIT flag, not
inferred from the intent text (keyword-guessing would mis-pin an intent like 'measurement-conversion' and wrongly
block a permitted fallback). Unlike metered_only it does NOT force the metered API (a $0 lane that CAN serve the
named model is fine); it only forbids changing the model.

`files=[path, …]` is the INPUT twin of the output guard below: each path is assembled into the prompt as a
WHOLE, stamped, self-verified block by llm_files.attach_many (raises rather than send a partial file). The
caller hands PATHS, so it has no way to pre-truncate a file into the prompt — the same discipline the output
side applies to never reading a truncated reply as a short answer.

THE TOKEN CONTROLS LIVE HERE, IN THE ONE FUNCTION EVERY CALL ALREADY GOES THROUGH. They were built
twice over and wired nowhere: bulkgate.is_truncated ("a fact, not a guess") had no caller outside its
module, bulkgate.maxtokens learned the p99 output length per call-class and no judging script used it,
and vendor_call.input_limit had exactly one caller — a probe script. So every caller hand-picked a
number and then read the reply as if the number had been right.

The first attempt at fixing this put the logic in a SIBLING function (call_complete) and left this one
free to return a cut body. That is opt-in safety, which is the same failure one level up: an hour after
it shipped, 1 of 9 judging scripts used it, and a name-review re-run came back with 40 of 79 groups
"UNREVIEWED" that were really just truncated. A control the caller must remember to use is a control
that will not be there when it matters, so both now happen unconditionally, here.

INPUT AND OUTPUT ARE INDEPENDENT — bounded by DIFFERENT model numbers, neither constraining the other:
  INPUT   the prompt is measured against the model's INPUT window (pricing.max_input_tokens) BEFORE sending.
          Over it → an explicit error, not a request the vendor silently clips. This never touches max_tokens.
  OUTPUT  the reply budget floors at TOKEN_FLOOR and is clamped by the model's OUTPUT ceiling
          (pricing.max_output) — set from the OUTPUT axis alone, never lowered by how large the input was.
          A reply the provider marks as cut off is RETRIED at double the budget; if it still will not fit,
          text=None and truncated=True, so a truncated body can never be read as a short answer.

`sig` names the call-class so the OUTPUT budget comes from its measured p99 (never a literal nobody picked),
and so each reply feeds that measurement. `intent` is the caller-facing ALIAS for it (the job-type label
advise/best-value/attribution key on): pass either — most callers pass one; if both, `sig` is the finer
call-class and `intent` the job-type. `reasoning` (minimal|low|medium|high) sets reasoning effort for
gpt-5/o-series models; defaults to 'minimal' for them (default-medium reasoning eats the token budget →
empty output, and costs more — wrong for simple classify/extract calls).
`reasoning="best-value"` DELEGATES the choice: spendguard resolves the cheapest (model, effort) whose MEASURED
good_rate for this intent clears the bar (from advise.ranked — $0, no LLM), swaps `model` to it, and PINS it so
the bandit cannot re-swap. With no measured evidence for the intent it keeps the named model (honest); with
no_substitution it titrates EFFORT for the named model only. The chosen arm is stamped substituted_from/best_value
so the saving+decision are booked against the counterfactual (what the named model would have cost).

`temperature`/`top_p`/`seed` are DETERMINISM knobs honored ONLY on a measurement call (measurement=True) — refused
on a production call, where a silently-varying governed result is a wrong-result risk. They exist because without
determinism a priced A/B needs n replicates to separate the effect from run noise (MEASURED: the same arm on the
same 300 concepts with byte-identical prompts swung 7.7%↔31.8% — a 24-pt nondeterminism band against a 5.6-pt
between-arm signal, ~4x the effect, so two single-run comparisons ordered the arms oppositely and both looked
clean). A $0 subscription LANE CLI has no temperature/seed channel, so setting any of these FORCES the metered API
for that call (and pins the model) — honoring the knobs rather than silently ignoring them on a lane, which would
reproduce the very artefact. seed is OpenAI-only (anthropic has no seed); it is dropped with a notice on anthropic.
The knobs sent are stamped on the result as `gen_params` so a replicate is distinguishable from an effort change.

PARAMS a consumer commonly passes (all optional): `timeout_s` bounds the request — a client-side cancel that
actually STOPS the call (and its billing), not merely the wait; it applies to BOTH the lane and the API path.
`schema` (a JSON Schema) forces STRUCTURED output — a forced tool-call on anthropic, response_format
json_schema/json_object elsewhere. `no_metered_fallback=True` makes a lane MISS return an error instead of
paying the API ($0 by construction). `no_substitution=True` PINS THE VENDOR: it suppresses BOTH lane-bandit
load-balancing (proactive) and lane-failover substitution (reactive), so the requested model answers or errors —
never a silent swap. Use it whenever the VENDOR IS THE MEASUREMENT (a cross-vendor consensus panel, a 2-judge
adjudication): the bandit in optout mode can otherwise run one model in place of four while every result keeps
its requested label. `images=[path | data: URL, …]` sends a VISION request — each image is
loaded once, priced by the PIXEL rule (content_tokens, not the text tokenizer), and the call rides the
metered API (the subscription lanes are text-only CLIs, so a vision call skips them: executor='api').

`soft_deadline_s` requests cancel-on-proven-zero-billable behavior. Current adapter transports are blocking
(non-streaming SDK calls and CLI wrappers) and expose neither a first-billable-token event nor a live cancel
handle, so they fail safe: run to completion and report `stall_outcome='produced_tokens_ran'`. Transport authors
can use escalation_ladder.cancel_on_zero_billable when they add those two capabilities.

RETURNS a dict — NEVER raises — with the SAME keys on success and failure, so a caller never has to guess:
  text          the answer (str), or None on any failure / a reply truncated past its retry
  cost          $ for THIS call: 0.0 = served by a $0 subscription LANE, a positive number = metered API,
                None = refused/errored (nothing spent)
  executor      WHICH path served or attempted it — a lane name ('claude-code' | 'codex' | 'gemini' |
                'zai-coding') or 'api' (the metered provider). This is how a caller tells LANE-vs-API from
                the result alone, on success AND on error.
  in_tok/out_tok/latency/finish_reason  usage + why the reply ended (finish_reason 'length' = truncated)
  substituted_from  present when a lane miss was served by a DIFFERENT lane/model (provenance)
  on FAILURE (error is non-None): error (one-line message), error_type (the exception CLASS — e.g.
                APITimeoutError [deadline] vs APIConnectionError [transport] vs NotFoundError [bad model]),
                status_code (HTTP status if any), provider_error (the real response BODY, not the one-line
                str), cause (the underlying error behind a generic wrapper — 'Connection error.' ←
                'ConnectTimeout'), retry_after (seconds, if the provider sent one).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | Any | yes | required | Requested model; explicit best-value may substitute a quality-qualified measured model unless model substitution is pinned. |
| prompt | Any | yes | required | Source parameter prompt at src/spendguard/adapters.py:931; see complete callable contract. |
| max_tokens | Any | no | None | Compatibility argument: normal execution obtains the output ceiling from adapters.output_budget and ignores a caller-selected cap. Actual generated tokens determine billing; input context limits are separate. |
| system | Any | no | None | Source parameter system at src/spendguard/adapters.py:931; see complete callable contract. |
| reasoning | Any | no | None | Explicit 'best-value' selects measured model and reasoning effort for the intent when quality evidence qualifies. Inherited advisor.default_reasoning='best-value' preserves a supplied model and selects effort; no_substitution also pins the model. |
| schema | Any | no | None | Source parameter schema at src/spendguard/adapters.py:931; see complete callable contract. |
| timeout_s | Any | no | None | Source parameter timeout_s at src/spendguard/adapters.py:931; see complete callable contract. |
| sig | Any | no | None | Source parameter sig at src/spendguard/adapters.py:931; see complete callable contract. |
| intent | Any | no | None | Job label for ledger attribution, measured quality evidence, budget context and routing. |
| retries | Any | no | 2 | Source parameter retries at src/spendguard/adapters.py:931; see complete callable contract. |
| files | Any | no | None | Source parameter files at src/spendguard/adapters.py:931; see complete callable contract. |
| _no_guard | Any | no | False | Source parameter _no_guard at src/spendguard/adapters.py:931; see complete callable contract. |
| no_metered_fallback | Any | no | False | True forbids paid API fallback when a subscription lane fails or cannot serve the request. |
| images | Any | no | None | Source parameter images at src/spendguard/adapters.py:931; see complete callable contract. |
| no_substitution | Any | no | False | Pin the requested model while permitting its reasoning-effort selection. |
| measurement | Any | no | False | Pin model attribution for measurement. Deterministic measurement controls additionally force paid API execution. |
| metered_only | Any | no | False | True skips subscription lanes and selects paid API execution. |
| base_fallback | Any | no | None | Source parameter base_fallback at src/spendguard/adapters.py:931; see complete callable contract. |
| _probe | Any | no | False | Source parameter _probe at src/spendguard/adapters.py:931; see complete callable contract. |
| _route | Any | no | True | Source parameter _route at src/spendguard/adapters.py:931; see complete callable contract. |
| _internal_pin | Any | no | False | Source parameter _internal_pin at src/spendguard/adapters.py:931; see complete callable contract. |
| _coalesce_eligible | Any | no | True | Source parameter _coalesce_eligible at src/spendguard/adapters.py:931; see complete callable contract. |
| temperature | Any | no | None | Source parameter temperature at src/spendguard/adapters.py:931; see complete callable contract. |
| top_p | Any | no | None | Source parameter top_p at src/spendguard/adapters.py:931; see complete callable contract. |
| seed | Any | no | None | Source parameter seed at src/spendguard/adapters.py:931; see complete callable contract. |
| soft_deadline_s | Any | no | None | Source parameter soft_deadline_s at src/spendguard/adapters.py:931; see complete callable contract. |
| auth_over_quota | Any | no | False | Source parameter auth_over_quota at src/spendguard/adapters.py:931; see complete callable contract. |
| decomposition | Any | no | None | Source parameter decomposition at src/spendguard/adapters.py:931; see complete callable contract. |
| aliases | Any | no | omitted; implementation decides | Source parameter aliases at src/spendguard/adapters.py:931; see complete callable contract. |

Returns: `Any`

<a id="core-7cd63734d7e1d279db772c3c0ceedc9250715358d971ca640fb258b59a4f9d9d"></a>

### `spendguard.adapters:deadline_for`

Source: `src/spendguard/adapters.py:2242`

PUBLIC deadline advisor — how many seconds a call should be ALLOWED to take, sized from MEASURED latency
(never a hardcoded number). Returns (seconds, basis): `seconds` is a proposed deadline you pass as timeout_s;
`basis` names where it came from — 'measured:class(n=…)' / 'measured:model(n=…)' / 'caller' / 'lane-floor' /
'unknown'. `seconds` is None with basis 'unknown' when there is not yet enough measurement (>= 5 obs) — answer
that with your own default_s rather than have a guess invented for you. Clamped to [DEADLINE_FLOOR_S,
DEADLINE_CEIL_S] (30s..1800s) and floored to a lane's minimum when a $0 lane serves the vendor.

This is the ONE public door onto the internal sizing (vendor_call.time_budget), so a consumer never imports
that internal: pass the model (a bare id or 'provider:model'), the job `intent`, and the input size in chars;
the vendor and call-class are derived here. Read-only, $0 — no model call. NOTE: adapters.call and crossllm.ask
already APPLY this sizing transitively — use this only to SIZE a deadline you pass yourself (e.g. a Batch submit).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | Any | yes | required | Source parameter model at src/spendguard/adapters.py:2242; see complete callable contract. |
| intent | Any | no | None | Source parameter intent at src/spendguard/adapters.py:2242; see complete callable contract. |
| in_chars | Any | no | None | Source parameter in_chars at src/spendguard/adapters.py:2242; see complete callable contract. |
| default_s | Any | no | None | Source parameter default_s at src/spendguard/adapters.py:2242; see complete callable contract. |

Returns: `Any`

<a id="core-ab52ba0149c314f1b9f89cdee406e2f6b82d561b9ac7aef3f363d9ff09bdda4f"></a>

### `spendguard.adapters:embed`

Source: `src/spendguard/adapters.py:1900`

The first-class REALTIME embedding surface — embed a list of texts on an OpenAI-COMPATIBLE provider (openai
+ gemini today; both speak /embeddings), gated + priced + metered exactly like adapters.call (the gate already
intercepts .embeddings.create). `model` defaults to the OpenAI default (config advisor.embed_model, else
text-embedding-3-small); pass a gemini id (e.g. 'gemini-embedding-001') to embed there, so the SAME corpus can
be embedded on both for an A/B.

ROBUST BULK (the CHUNK-never-single-shot rule, all four parts):
  • CHUNKED into requests of at most `max_batch` inputs;
  • DURABLE BY DEFAULT — a MULTI-chunk run auto-checkpoints to a jsonl under HOME's marked checkpoint dir (a single request is atomic;
    more than one is crash-losable), each chunk APPENDED (keyed by sha256(text) — CONTENT, not position) BEFORE
    the next runs, so a crash RESUMES and never re-pays, and a changed list re-embeds only new texts;
  • per-request TIMEOUT (default _EMBED_TIMEOUT_S) so a hung call can't wedge the run;
  • per-input SIZE GUARD + per-chunk ISOLATION — an oversized text is marked failed for that item (never sent to
    400 its chunk), and one chunk's failure marks its items and KEEPS GOING (the rest still embed + checkpoint).

Returns a dict — NEVER raises (except a deliberate spend stop, which PROPAGATES) — {vectors, model, dims, n,
checkpoint, failed, error}. `vectors` is ALIGNED to inputs (None where an item failed) and `failed` lists
{i, reason}; it is never a SILENTLY short list — a None + a failed[] entry, or an explicit error. Re-run to
retry only the failed items (the checkpoint already holds the rest).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| texts | Any | yes | required | Source parameter texts at src/spendguard/adapters.py:1900; see complete callable contract. |
| model | Any | no | None | Source parameter model at src/spendguard/adapters.py:1900; see complete callable contract. |
| dimensions | Any | no | None | Source parameter dimensions at src/spendguard/adapters.py:1900; see complete callable contract. |
| max_batch | Any | no | None | Source parameter max_batch at src/spendguard/adapters.py:1900; see complete callable contract. |
| timeout_s | Any | no | None | Source parameter timeout_s at src/spendguard/adapters.py:1900; see complete callable contract. |
| checkpoint | Any | no | None | Source parameter checkpoint at src/spendguard/adapters.py:1900; see complete callable contract. |

Returns: `Any`

<a id="core-35acdd39e41504e3005a2c4fe685c4f5fe166831bde1aa417403174590f0a648"></a>

### `spendguard.adapters:embed_batch`

Source: `src/spendguard/adapters.py:2125`

The first-class BATCH embedding surface — the ASYNC Batch API (~50% cheaper) for a LARGE corpus, gated
estimate-first + capped by the SAME chokepoint every batch submission passes (submit.guarded_submit). Builds a
/v1/embeddings JSONL (one input per line, custom_id 'emb-<i>' = input index) at a durable path, estimates +
cap-checks it ($0, no paid call), then submits. Returns {batch_id, jsonl, requests, error}; submit=False
writes + estimates only. Collect results later with callio.guarded_collect(batch_id) — vectors map by custom_id.

Batch availability is a CONFIG CAPABILITY on the provider spec (`batch`), never a hardcoded id check: OpenAI
declares it (its async Batch API). A provider that does not (gemini's OpenAI-compat endpoint exposes no
/batches — its async batch is a separate native API) returns a clear error pointing to embed() (chunked
realtime) until its native batch path is wired — a LOUD boundary, never a silent metered fallback.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| texts | Any | yes | required | Source parameter texts at src/spendguard/adapters.py:2125; see complete callable contract. |
| model | Any | no | None | Source parameter model at src/spendguard/adapters.py:2125; see complete callable contract. |
| dimensions | Any | no | None | Source parameter dimensions at src/spendguard/adapters.py:2125; see complete callable contract. |
| jsonl_path | Any | no | None | Source parameter jsonl_path at src/spendguard/adapters.py:2125; see complete callable contract. |
| cap_dollars | Any | no | None | Source parameter cap_dollars at src/spendguard/adapters.py:2125; see complete callable contract. |
| submit | Any | no | True | Source parameter submit at src/spendguard/adapters.py:2125; see complete callable contract. |
| preflight | Any | no | True | Source parameter preflight at src/spendguard/adapters.py:2125; see complete callable contract. |

Returns: `Any`

<a id="core-d8f59f72792b19ec7997b313d2a6f886df33af39c9ef29bc876f57debd59a7b6"></a>

### `spendguard.adapters:embed_compare`

Source: `src/spendguard/adapters.py:2174`

A/B two or more embedding models on the SAME texts — the cheap way to decide openai vs gemini (vs voyage)
BEFORE committing a corpus + a Qdrant collection to one. Embeds a SAMPLE of `texts` on each model (default: the
OpenAI default + gemini-embedding-001), then reports per-model {dims, rate_per_1m, n_failed} and — PAIRWISE — how
much the models AGREE on neighbourhood STRUCTURE: the Pearson correlation of their off-diagonal cosine-similarity
matrices (≈1.0 = the models call the same texts near/far; low = they disagree on what is similar). A quality
signal that needs NO labelled retrieval set. Makes REAL (gated) embedding calls on the sample — small, not free.

Returns {sample_n, models: [{model, dims, rate_per_1m, n_failed, error}],
         agreement: [{a, b, structure_corr, pairs}]}.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| texts | Any | yes | required | Source parameter texts at src/spendguard/adapters.py:2174; see complete callable contract. |
| models | Any | no | None | Source parameter models at src/spendguard/adapters.py:2174; see complete callable contract. |
| sample | Any | no | None | Source parameter sample at src/spendguard/adapters.py:2174; see complete callable contract. |

Returns: `Any`

<a id="core-61668d561b03727d4e3e35fe99cec36525d4a671fdbc2d38ab790fb532f308eb"></a>

### `spendguard.adapters:gc_embed_checkpoints`

Source: `src/spendguard/adapters.py:2039`

Bound the `embed_<model>_<hash>.jsonl` resume checkpoints that embed() writes in its marked dir, plus legacy
top-level HOME checkpoints. Measured
2026-10-08: 1,016 files / 19.65 GiB had accumulated in ~/.spendguard, 1,014 of them inside one month — embed()
created a checkpoint per multi-chunk run and NOTHING ever pruned one.

Why a RETENTION BOUND and not delete-on-success: a checkpoint is keyed by sha256(text), so re-running the same
corpus is served entirely from it and re-pays NOTHING. Deleting on clean completion would hand that saving back
(and break the resume guarantee tests/test_embed_surface.py asserts). So a checkpoint stays useful for a window
and is pruned once it is COLD — recent ones are never touched.

TWO bounds, both applied (age first, then total size):
  • AGE — older than `max_age_days` (safety.embed_checkpoint_max_age_days, default 7) is cold → prune;
  • SIZE — if what SURVIVES the age bound still exceeds `max_total_gb`
    (safety.embed_checkpoint_max_gb, default 5), prune OLDEST-FIRST until under the cap, so one month of heavy
    embedding cannot fill the disk even when every file is recent.

Unlike codex_exec.gc_shell_snapshots (opt-in, because it deletes in CODEX's dir), this prunes spendguard's OWN
temp files, so embed() calls it on create. DRY-RUN by default; deletes only with apply=True. `keep_path` is
never pruned — the in-flight run's own checkpoint, which both bounds must ignore.

`embed_batch_*.jsonl` is EXCLUDED: that is a submitted batch's request envelope, still needed to collect its
results — a different artefact that happens to share the prefix. Honest reporting (never masks a failure as a
clean sweep): a listing failure sets `error`; a per-file stat/unlink failure is COUNTED in `skipped`. Returns
{dir, examined, stale, over_cap, bytes, deleted, skipped, apply, cutoff_days, max_total_gb, error}.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| max_age_days | Any | no | None | Bound the `embed_<model>_<hash>.jsonl` resume checkpoints that embed() writes in its marked dir, plus legacy<br>top-level HOME checkpoints. Measured<br>2026-10-08: 1,016 files / 19.65 GiB had accumulated in ~/.spendguard, 1,014 of them inside one month — embed()<br>created a checkpoint per multi-chunk run and NOTHING ever pruned one.<br><br>Why a RETENTION BOUND and not delete-on-success: a checkpoint is keyed by sha256(text), so re-running the same<br>corpus is served entirely from it and re-pays NOTHING. Deleting on clean completion would hand that saving back<br>(and break the resume guarantee tests/test_embed_surface.py asserts). So a checkpoint stays useful for a window<br>and is pruned once it is COLD — recent ones are never touched.<br><br>TWO bounds, both applied (age first, then total size):<br>  • AGE — older than `max_age_days` (safety.embed_checkpoint_max_age_days, default 7) is cold → prune;<br>  • SIZE — if what SURVIVES the age bound still exceeds `max_total_gb`<br>    (safety.embed_checkpoint_max_gb, default 5), prune OLDEST-FIRST until under the cap, so one month of heavy<br>    embedding cannot fill the disk even when every file is recent.<br><br>Unlike codex_exec.gc_shell_snapshots (opt-in, because it deletes in CODEX's dir), this prunes spendguard's OWN<br>temp files, so embed() calls it on create. DRY-RUN by default; deletes only with apply=True. `keep_path` is<br>never pruned — the in-flight run's own checkpoint, which both bounds must ignore.<br><br>`embed_batch_*.jsonl` is EXCLUDED: that is a submitted batch's request envelope, still needed to collect its<br>results — a different artefact that happens to share the prefix. Honest reporting (never masks a failure as a<br>clean sweep): a listing failure sets `error`; a per-file stat/unlink failure is COUNTED in `skipped`. Returns<br>{dir, examined, stale, over_cap, bytes, deleted, skipped, apply, cutoff_days, max_total_gb, error}. |
| max_total_gb | Any | no | None | Bound the `embed_<model>_<hash>.jsonl` resume checkpoints that embed() writes in its marked dir, plus legacy<br>top-level HOME checkpoints. Measured<br>2026-10-08: 1,016 files / 19.65 GiB had accumulated in ~/.spendguard, 1,014 of them inside one month — embed()<br>created a checkpoint per multi-chunk run and NOTHING ever pruned one.<br><br>Why a RETENTION BOUND and not delete-on-success: a checkpoint is keyed by sha256(text), so re-running the same<br>corpus is served entirely from it and re-pays NOTHING. Deleting on clean completion would hand that saving back<br>(and break the resume guarantee tests/test_embed_surface.py asserts). So a checkpoint stays useful for a window<br>and is pruned once it is COLD — recent ones are never touched.<br><br>TWO bounds, both applied (age first, then total size):<br>  • AGE — older than `max_age_days` (safety.embed_checkpoint_max_age_days, default 7) is cold → prune;<br>  • SIZE — if what SURVIVES the age bound still exceeds `max_total_gb`<br>    (safety.embed_checkpoint_max_gb, default 5), prune OLDEST-FIRST until under the cap, so one month of heavy<br>    embedding cannot fill the disk even when every file is recent.<br><br>Unlike codex_exec.gc_shell_snapshots (opt-in, because it deletes in CODEX's dir), this prunes spendguard's OWN<br>temp files, so embed() calls it on create. DRY-RUN by default; deletes only with apply=True. `keep_path` is<br>never pruned — the in-flight run's own checkpoint, which both bounds must ignore.<br><br>`embed_batch_*.jsonl` is EXCLUDED: that is a submitted batch's request envelope, still needed to collect its<br>results — a different artefact that happens to share the prefix. Honest reporting (never masks a failure as a<br>clean sweep): a listing failure sets `error`; a per-file stat/unlink failure is COUNTED in `skipped`. Returns<br>{dir, examined, stale, over_cap, bytes, deleted, skipped, apply, cutoff_days, max_total_gb, error}. |
| apply | Any | no | False | Bound the `embed_<model>_<hash>.jsonl` resume checkpoints that embed() writes in its marked dir, plus legacy<br>top-level HOME checkpoints. Measured<br>2026-10-08: 1,016 files / 19.65 GiB had accumulated in ~/.spendguard, 1,014 of them inside one month — embed()<br>created a checkpoint per multi-chunk run and NOTHING ever pruned one.<br><br>Why a RETENTION BOUND and not delete-on-success: a checkpoint is keyed by sha256(text), so re-running the same<br>corpus is served entirely from it and re-pays NOTHING. Deleting on clean completion would hand that saving back<br>(and break the resume guarantee tests/test_embed_surface.py asserts). So a checkpoint stays useful for a window<br>and is pruned once it is COLD — recent ones are never touched.<br><br>TWO bounds, both applied (age first, then total size):<br>  • AGE — older than `max_age_days` (safety.embed_checkpoint_max_age_days, default 7) is cold → prune;<br>  • SIZE — if what SURVIVES the age bound still exceeds `max_total_gb`<br>    (safety.embed_checkpoint_max_gb, default 5), prune OLDEST-FIRST until under the cap, so one month of heavy<br>    embedding cannot fill the disk even when every file is recent.<br><br>Unlike codex_exec.gc_shell_snapshots (opt-in, because it deletes in CODEX's dir), this prunes spendguard's OWN<br>temp files, so embed() calls it on create. DRY-RUN by default; deletes only with apply=True. `keep_path` is<br>never pruned — the in-flight run's own checkpoint, which both bounds must ignore.<br><br>`embed_batch_*.jsonl` is EXCLUDED: that is a submitted batch's request envelope, still needed to collect its<br>results — a different artefact that happens to share the prefix. Honest reporting (never masks a failure as a<br>clean sweep): a listing failure sets `error`; a per-file stat/unlink failure is COUNTED in `skipped`. Returns<br>{dir, examined, stale, over_cap, bytes, deleted, skipped, apply, cutoff_days, max_total_gb, error}. |
| keep_path | Any | no | None | Bound the `embed_<model>_<hash>.jsonl` resume checkpoints that embed() writes in its marked dir, plus legacy<br>top-level HOME checkpoints. Measured<br>2026-10-08: 1,016 files / 19.65 GiB had accumulated in ~/.spendguard, 1,014 of them inside one month — embed()<br>created a checkpoint per multi-chunk run and NOTHING ever pruned one.<br><br>Why a RETENTION BOUND and not delete-on-success: a checkpoint is keyed by sha256(text), so re-running the same<br>corpus is served entirely from it and re-pays NOTHING. Deleting on clean completion would hand that saving back<br>(and break the resume guarantee tests/test_embed_surface.py asserts). So a checkpoint stays useful for a window<br>and is pruned once it is COLD — recent ones are never touched.<br><br>TWO bounds, both applied (age first, then total size):<br>  • AGE — older than `max_age_days` (safety.embed_checkpoint_max_age_days, default 7) is cold → prune;<br>  • SIZE — if what SURVIVES the age bound still exceeds `max_total_gb`<br>    (safety.embed_checkpoint_max_gb, default 5), prune OLDEST-FIRST until under the cap, so one month of heavy<br>    embedding cannot fill the disk even when every file is recent.<br><br>Unlike codex_exec.gc_shell_snapshots (opt-in, because it deletes in CODEX's dir), this prunes spendguard's OWN<br>temp files, so embed() calls it on create. DRY-RUN by default; deletes only with apply=True. `keep_path` is<br>never pruned — the in-flight run's own checkpoint, which both bounds must ignore.<br><br>`embed_batch_*.jsonl` is EXCLUDED: that is a submitted batch's request envelope, still needed to collect its<br>results — a different artefact that happens to share the prefix. Honest reporting (never masks a failure as a<br>clean sweep): a listing failure sets `error`; a per-file stat/unlink failure is COUNTED in `skipped`. Returns<br>{dir, examined, stale, over_cap, bytes, deleted, skipped, apply, cutoff_days, max_total_gb, error}. |
| include_legacy_top_level | Any | no | True | Bound the `embed_<model>_<hash>.jsonl` resume checkpoints that embed() writes in its marked dir, plus legacy<br>top-level HOME checkpoints. Measured<br>2026-10-08: 1,016 files / 19.65 GiB had accumulated in ~/.spendguard, 1,014 of them inside one month — embed()<br>created a checkpoint per multi-chunk run and NOTHING ever pruned one.<br><br>Why a RETENTION BOUND and not delete-on-success: a checkpoint is keyed by sha256(text), so re-running the same<br>corpus is served entirely from it and re-pays NOTHING. Deleting on clean completion would hand that saving back<br>(and break the resume guarantee tests/test_embed_surface.py asserts). So a checkpoint stays useful for a window<br>and is pruned once it is COLD — recent ones are never touched.<br><br>TWO bounds, both applied (age first, then total size):<br>  • AGE — older than `max_age_days` (safety.embed_checkpoint_max_age_days, default 7) is cold → prune;<br>  • SIZE — if what SURVIVES the age bound still exceeds `max_total_gb`<br>    (safety.embed_checkpoint_max_gb, default 5), prune OLDEST-FIRST until under the cap, so one month of heavy<br>    embedding cannot fill the disk even when every file is recent.<br><br>Unlike codex_exec.gc_shell_snapshots (opt-in, because it deletes in CODEX's dir), this prunes spendguard's OWN<br>temp files, so embed() calls it on create. DRY-RUN by default; deletes only with apply=True. `keep_path` is<br>never pruned — the in-flight run's own checkpoint, which both bounds must ignore.<br><br>`embed_batch_*.jsonl` is EXCLUDED: that is a submitted batch's request envelope, still needed to collect its<br>results — a different artefact that happens to share the prefix. Honest reporting (never masks a failure as a<br>clean sweep): a listing failure sets `error`; a per-file stat/unlink failure is COUNTED in `skipped`. Returns<br>{dir, examined, stale, over_cap, bytes, deleted, skipped, apply, cutoff_days, max_total_gb, error}. |

Returns: `Any`

<a id="core-7e40d0f70a93d5394ede11017981141a79cb69e3608f4d5598400031d2242a22"></a>

### `spendguard.adapters:generation`

Source: `src/spendguard/adapters.py:663`

Scope determinism knobs over EVERY nested adapters.call — the 'set it once for a bakeoff / a whole chat session'
surface, broader than a per-call arg. A DELIBERATE operator opt-in: calls inside honor the knobs WITHOUT needing
per-call measurement=True (unlike a stray per-call kwarg, which is refused), FORCE the metered API (a $0 lane has
no temperature/seed channel), and stamp gen_params on every result. Precedence: an explicit per-call knob still
wins; this sits above the `generation` config global. Nests (merges over an outer scope) and restores on exit.

    with adapters.generation(temperature=0, seed=20261008):
        bakeoff("vocab-ab", candidates=[...], run=True)   # every arm's call is deterministic + metered

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| temperature | Any | no | None | Source parameter temperature at src/spendguard/adapters.py:663; see complete callable contract. |
| top_p | Any | no | None | Source parameter top_p at src/spendguard/adapters.py:663; see complete callable contract. |
| seed | Any | no | None | Source parameter seed at src/spendguard/adapters.py:663; see complete callable contract. |

Returns: `Any`

<a id="core-69050de5faba0211df71f4256c1c82a7bf505ecd01a5bb08cc04773449e66fc5"></a>

### `spendguard.adapters:generation_support_details`

Source: `src/spendguard/adapters.py:600`

The subset of GEN_KNOBS (temperature, top_p, seed) that `provider`/`model`'s METERED API actually honors — the
SSOT that maps the caller's ONE standardized determinism surface onto each vendor's real capability, so the same
`adapters.call(..., measurement=True, temperature=0, seed=…)` is correct across providers and a knob the vendor
cannot take is RECORDED as dropped, never a silent 400 and never silently ignored. A FACT about the vendor API
(like schema_capability), not a judgement:
  • `seed` is an OpenAI (the real 'openai' provider) parameter; anthropic and the other OpenAI-compatible vendors
    (gemini/zai/moonshot/deepseek) have no seed field, so seed is unsupported there.
  • `temperature`/`top_p` are sampling knobs a REASONING model rejects or constrains (OpenAI 400s 'temperature
    does not support 0 with this model'; anthropic ties temperature to the thinking budget), so they are dropped
    for a reasoning model (reasons_by_default) and determinism there rests on seed where the vendor has it.
Returns ``(supported, unavailable_reason)``; the caller records both any dropped knobs and why the installed
SDK contract could not be inspected.  The reason is None when the contract was inspectable.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| provider | Any | yes | required | Source parameter provider at src/spendguard/adapters.py:600; see complete callable contract. |
| model | Any | yes | required | Source parameter model at src/spendguard/adapters.py:600; see complete callable contract. |

Returns: `Any`

<a id="core-37856c37c23162559aa68a492c4db1558c9fa7f8895a36acfb4ddda45b99c442"></a>

### `spendguard.adapters:json_schema_request`

Source: `src/spendguard/adapters.py:513`

Per-vendor kwargs that make the VENDOR enforce the shape, instead of a prompt asking politely.

  anthropic → a forced tool call: the model must emit tool_use conforming to input_schema
  openai    → response_format json_schema with strict=True
  others (OpenAI-compatible: GLM, Kimi, DeepSeek…) → response_format json_object, which guarantees
              parseable JSON but NOT the shape — so the local validator still has to run. Claiming
              otherwise would be assuming a capability we have not measured on those endpoints.

Enforcement guarantees the KEY exists. It does NOT guarantee the value means anything — `line_start: 0`
satisfies every strict schema ever written. That is what `nonempty` in output_contract is for.

TWO DIALECTS THAT LOOK IDENTICAL. output_contract's schema is OURS: it carries `nonempty`, which is not
JSON Schema and which no provider has ever heard of. Handing it to a provider verbatim is the same class
of error as reading a context window as an output ceiling — two things of the same shape, meaning
different things. Measured: gpt-5.5 rejected the contract outright (400, "'additionalProperties' is
required to be supplied and to be false"), while kimi-k3 and glm-5.2 accepted the request and invented a
shape, because the compat path told them "return JSON" and never told them WHICH json.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| kind | Any | yes | required | Source parameter kind at src/spendguard/adapters.py:513; see complete callable contract. |
| schema | Any | yes | required | Source parameter schema at src/spendguard/adapters.py:513; see complete callable contract. |
| name | Any | no | 'result' | Source parameter name at src/spendguard/adapters.py:513; see complete callable contract. |

Returns: `Any`

<a id="core-2964e7d7b3db7338eb0f4d4319d4750e3925f3d2d1ffde6e8d18881679ccf1a4"></a>

### `spendguard.adapters:panel_providers`

Source: `src/spendguard/adapters.py:2286`

The set of vendors that ACTUALLY answered a group of calls meant to be a cross-vendor panel — so a caller can
assert diversity FROM THE RESULTS (`len(panel_providers(rs)) == n`), never assume it. If the bandit collapsed the
panel this set is smaller than the number of successful calls, even though every result is labelled with a
different requested model. Errored results are excluded (a vendor that failed did not answer).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| results | Any | yes | required | Source parameter results at src/spendguard/adapters.py:2286; see complete callable contract. |

Returns: `Any`

<a id="core-0fcfc826d0da102e10c7b5ee25c322302cb4a4efcf17280371a4196275c8750c"></a>

### `spendguard.adapters:provider_base_model`

Source: `src/spendguard/adapters.py:86`

The RELIABLE BASE model for `provider` — the tier-3 last-resort fallback when a chosen model's $0 lane AND its
metered API both fail. Resolution: advisor.provider_base_model[provider] (an operator OVERRIDE) wins, else the
catalog's designated base (model_catalog.provider_base — the SSOT default). None if neither → tier-3 is a no-op for
that provider and the chain ends at metered. Never a hardcoded id here (the config/catalog own it), so a stale id
can't be baked in; same-provider by construction, so a consensus panel keeps its vendor identity.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| provider | Any | yes | required | Source parameter provider at src/spendguard/adapters.py:86; see complete callable contract. |

Returns: `Any`

<a id="core-b1e49d4dd77e13f8b8c082eaaf9a0a6bbf79f2afb88b83736f3521281e779aca"></a>

### `spendguard.adapters:provider_for`

Source: `src/spendguard/adapters.py:67`

Resolve provider from a model id. Accepts explicit 'provider:model' too. When several providers' prefixes
match, the LONGEST (most specific) prefix wins — DETERMINISTIC, independent of PROVIDERS insertion order, so a
plugin whose prefix overlaps a built-in can't resolve by dict order and silently bill the wrong vendor.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | Any | yes | required | Source parameter model at src/spendguard/adapters.py:67; see complete callable contract. |

Returns: `Any`

<a id="core-801cff9bffeebfdc73456e17a8e6031b400b92ce8fdbeff9626c693203fa28a3"></a>

### `spendguard.adapters:register_provider`

Source: `src/spendguard/adapters.py:59`

Add/override a provider. kind: 'openai' (OpenAI-compatible) or 'anthropic'.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| name | Any | yes | required | Source parameter name at src/spendguard/adapters.py:59; see complete callable contract. |
| base_url | Any | yes | required | Source parameter base_url at src/spendguard/adapters.py:59; see complete callable contract. |
| key_env | Any | yes | required | Source parameter key_env at src/spendguard/adapters.py:59; see complete callable contract. |
| prefixes | Any | yes | required | Source parameter prefixes at src/spendguard/adapters.py:59; see complete callable contract. |
| kind | Any | no | 'openai' | Source parameter kind at src/spendguard/adapters.py:59; see complete callable contract. |
| sdk_import | Any | no | None | Source parameter sdk_import at src/spendguard/adapters.py:59; see complete callable contract. |

Returns: `Any`

<a id="core-f5abf0761ded424f5fe42d04883afdb26c31aec8804c7ac86f4a0fdcfb3ca9cb"></a>

### `spendguard.adapters:safe_input_chars`

Source: `src/spendguard/adapters.py:107`

Max INPUT characters that safely FIT the context window (leaving room for the reply + the prompt scaffold) — so
a caller that SEGMENTS its own input (e.g. 7thsense's document comprehender) sizes to the REAL, catalog-sourced
window instead of a hardcoded guess. When `intent` is given, the MIN context window across the configured candidate
models (advisor.tiers) is used, so ANY model best-value might pick still fits. Sourced from
model_catalog.context_window (the METERED window); a LANE's smaller practical input capacity is a separate, LEARNED
axis (resource_state size_ceiling) with the tier-3 fallback behind it — so this is the upper SAFETY bound, not a
lane-usage target. Returns None when NO context is known (the caller keeps its own conservative default — never a
guessed number). chars_per_token is deliberately conservative (3.0; English prose is ~3.6+), so the estimate never
overshoots a real window.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | Any | yes | required | Source parameter model at src/spendguard/adapters.py:107; see complete callable contract. |
| intent | Any | no | None | Source parameter intent at src/spendguard/adapters.py:107; see complete callable contract. |
| reply_tokens | Any | no | 8000 | Source parameter reply_tokens at src/spendguard/adapters.py:107; see complete callable contract. |
| prompt_tokens | Any | no | 1500 | Source parameter prompt_tokens at src/spendguard/adapters.py:107; see complete callable contract. |
| chars_per_token | Any | no | 3.0 | Source parameter chars_per_token at src/spendguard/adapters.py:107; see complete callable contract. |

Returns: `Any`

<a id="core-e48066cc549119da9d2e09d9f49ad1450f36f38c97b556ea13b77ab01059e80a"></a>

### `spendguard.adapters:schema_capability`

Source: `src/spendguard/adapters.py:584`

How strongly `provider`'s METERED path can hold output to a JSON schema: SCHEMA_STRICT (anthropic/openai — the
vendor enforces the exact shape) or SCHEMA_JSON_OBJECT (every other OpenAI-compatible vendor — JSON guaranteed, the
shape is not). Derived from _schema_kind so it cannot disagree with the request json_schema_request actually builds.
A LANE is prompt-only regardless (a CLI completion carries no response_format/tool param), so a lane NEVER has
strict capability even for a strict-capable vendor — which is exactly why a strict schema on a prompt-only lane
must route to that vendor's metered path (the capability-aware auto-route).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| provider | Any | yes | required | Source parameter provider at src/spendguard/adapters.py:584; see complete callable contract. |

Returns: `Any`

<a id="core-eea380d84c003c970235e014651306a4d1f127792921bcee4f469522ce5e4761"></a>

### `spendguard.adapters:served_by`

Source: `src/spendguard/adapters.py:2270`

The vendor that ACTUALLY answered — read from the RESULT, never from what was requested. When the bandit
swapped the model this is the SUBSTITUTE's vendor, not the one you asked for. A cross-vendor panel MUST key on
this, never on the requested model: the lane bandit (bandit_mode=optout) can run one model 'in place of' four,
and every result still carries its REQUESTED label — so keying on the request reads a collapse as four agreeing
vendors (measured: gpt-5.5 answering as anthropic/gemini/zai/moonshot, panel printed all-ok). Uses the recorded
answering model (the honesty invariant: recorded model == who answered), falling back to the executor/lane.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| result | Any | yes | required | Source parameter result at src/spendguard/adapters.py:2270; see complete callable contract. |

Returns: `Any`

<a id="core-c28fbbc6d9e7b81fbf3073b3c41732395486919179eccbd63aa1ed11fb87c955"></a>

### `spendguard.adapters:vision`

Source: `src/spendguard/adapters.py:1719`

Run one VISION prompt (image(s) + text) against a vision-capable model — the explicit, discoverable entry
for what `call(images=…)` does, so a vision request is never accidentally sent as a text call.

WHY A NAMED ENTRY. Vision has one non-obvious rule: the subscription LANES are text-only print-mode CLIs
(agy/codex/claude-code) with NO image channel, so a vision call must ride the metered API. `call` already does
this — an `images=` argument skips the lane by construction (executor='api') and prices each image by PIXELS
(content_tokens), not the text tokenizer. But that lived as one kwarg on a long signature, easy to miss: a
caller who did not find it embedded the image elsewhere, the request rode a text lane, and the CLI cold-400'd
or returned an empty/garbled reply. This entry makes the capability impossible to miss and REFUSES to silently
degrade — `images` is required and must be non-empty (a vision call with no image is a bug, not a text call).
Each image is a file PATH or a data: URL. NB: `no_substitution` does NOT route a call to the API — only
`images` skips the lane; that mix-up (no_substitution as a vision fix) is why this entry exists.

SCHEMA. Pass `schema` for structured output, but on OpenAI models keep it strict-expressible: a DYNAMIC-KEY MAP
(labels:{<id>:<label>}) is collapsed to {} by strict mode and comes back empty — use an ARRAY of {key,value}
objects (results:[{id,label}]). `call` refuses such a map with a typed SchemaNotStrictExpressible error rather
than returning the empty object, so the failure is loud, not a plausible blank.

Returns the same result dict as `call` (executor='api'); raises ONLY the empty-images guard.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | Any | yes | required | Source parameter model at src/spendguard/adapters.py:1719; see complete callable contract. |
| prompt | Any | yes | required | Source parameter prompt at src/spendguard/adapters.py:1719; see complete callable contract. |
| images | Any | yes | required | Source parameter images at src/spendguard/adapters.py:1719; see complete callable contract. |
| schema | Any | no | None | Source parameter schema at src/spendguard/adapters.py:1719; see complete callable contract. |
| system | Any | no | None | Source parameter system at src/spendguard/adapters.py:1719; see complete callable contract. |
| sig | Any | no | None | Source parameter sig at src/spendguard/adapters.py:1719; see complete callable contract. |
| reasoning | Any | no | None | Source parameter reasoning at src/spendguard/adapters.py:1719; see complete callable contract. |
| max_tokens | Any | no | None | Source parameter max_tokens at src/spendguard/adapters.py:1719; see complete callable contract. |
| timeout_s | Any | no | None | Source parameter timeout_s at src/spendguard/adapters.py:1719; see complete callable contract. |
| retries | Any | no | 2 | Source parameter retries at src/spendguard/adapters.py:1719; see complete callable contract. |
| no_metered_fallback | Any | no | False | Source parameter no_metered_fallback at src/spendguard/adapters.py:1719; see complete callable contract. |
| no_substitution | Any | no | False | Source parameter no_substitution at src/spendguard/adapters.py:1719; see complete callable contract. |

Returns: `Any`

<a id="core-94c5f0267e76eb0720077029f2a8140fea908e5a4d7cb211a78a40af49b06c7e"></a>

### `spendguard.adapters:was_substituted`

Source: `src/spendguard/adapters.py:2264`

True if a DIFFERENT model answered this call than was requested — a lane-bandit or confirmed-substitute swap.
The provenance is `result['substituted_from']` (the model you asked for); `result['model']` is who answered.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| result | Any | yes | required | Source parameter result at src/spendguard/adapters.py:2264; see complete callable contract. |

Returns: `Any`

<a id="core-b5bfe0fbc9438ce6c08f2548bac4f8d1c63171c01fe17ba50a498055cfc1f0a6"></a>

### `spendguard.advise:ranked`

Source: `src/spendguard/advise.py:102`

The structured cost×quality ranking per (vendor:model) for an intent — the ONE computation behind both
the `advise` CLI printer and the MCP `advise`/`recommend` tools, so the ranking can never drift between the
surfaces (the #0b duplication trap). Best-first by $/good-result where any quality is labeled, else by $/M
output (cost only). Each row: id ('vendor:model'), model (bare), provider, jobs, cost, out_tok, per_m_out,
good_rate (None if unlabeled), per_good (None if no good result). Returns
{scope, as_of, labeled, metric, pick, models:[...]}.

`by_effort=True` returns one row per (vendor:model, EFFORT) — each row also carries `effort` — so the
best-value selector can pick the cheapest effort that holds quality. `id` stays 'vendor:model' in BOTH modes
(so plan-matching and the CLI are unchanged); the effort rides its own field, never fused into the id.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | no | None | The structured cost×quality ranking per (vendor:model) for an intent — the ONE computation behind both<br>the `advise` CLI printer and the MCP `advise`/`recommend` tools, so the ranking can never drift between the<br>surfaces (the #0b duplication trap). Best-first by $/good-result where any quality is labeled, else by $/M<br>output (cost only). Each row: id ('vendor:model'), model (bare), provider, jobs, cost, out_tok, per_m_out,<br>good_rate (None if unlabeled), per_good (None if no good result). Returns<br>{scope, as_of, labeled, metric, pick, models:[...]}.<br><br>`by_effort=True` returns one row per (vendor:model, EFFORT) — each row also carries `effort` — so the<br>best-value selector can pick the cheapest effort that holds quality. `id` stays 'vendor:model' in BOTH modes<br>(so plan-matching and the CLI are unchanged); the effort rides its own field, never fused into the id. |
| as_of | Any | no | None | The structured cost×quality ranking per (vendor:model) for an intent — the ONE computation behind both<br>the `advise` CLI printer and the MCP `advise`/`recommend` tools, so the ranking can never drift between the<br>surfaces (the #0b duplication trap). Best-first by $/good-result where any quality is labeled, else by $/M<br>output (cost only). Each row: id ('vendor:model'), model (bare), provider, jobs, cost, out_tok, per_m_out,<br>good_rate (None if unlabeled), per_good (None if no good result). Returns<br>{scope, as_of, labeled, metric, pick, models:[...]}.<br><br>`by_effort=True` returns one row per (vendor:model, EFFORT) — each row also carries `effort` — so the<br>best-value selector can pick the cheapest effort that holds quality. `id` stays 'vendor:model' in BOTH modes<br>(so plan-matching and the CLI are unchanged); the effort rides its own field, never fused into the id. |
| by_effort | Any | no | False | The structured cost×quality ranking per (vendor:model) for an intent — the ONE computation behind both<br>the `advise` CLI printer and the MCP `advise`/`recommend` tools, so the ranking can never drift between the<br>surfaces (the #0b duplication trap). Best-first by $/good-result where any quality is labeled, else by $/M<br>output (cost only). Each row: id ('vendor:model'), model (bare), provider, jobs, cost, out_tok, per_m_out,<br>good_rate (None if unlabeled), per_good (None if no good result). Returns<br>{scope, as_of, labeled, metric, pick, models:[...]}.<br><br>`by_effort=True` returns one row per (vendor:model, EFFORT) — each row also carries `effort` — so the<br>best-value selector can pick the cheapest effort that holds quality. `id` stays 'vendor:model' in BOTH modes<br>(so plan-matching and the CLI are unchanged); the effort rides its own field, never fused into the id. |

Returns: `Any`

<a id="core-c40a37e5989fa4c4746161ddb687ebc7ecf5681c614f5696ce3c8c5aea11f4d9"></a>

### `spendguard.advisor:mine`

Source: `src/spendguard/advisor.py:172`

Estimate, or explicitly run, evidence synthesis and persist resulting insights.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| run | Any | no | False | Execute the governed synthesis call; false returns its estimate only. |
| intent | Any | no | None | Restrict measured evidence to this intent. |

Returns: `Any`

<a id="core-e67f73c0e429d4ccef8d0923aa934cdc75a98501502e8331734f603875aa7358"></a>

### `spendguard.advisor:optimize`

Source: `src/spendguard/advisor.py:256`

Estimate, or explicitly run, measured-evidence optimization advice for an intent and plan.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | no | None | Job-type intent used to select measured evidence and insights. |
| plan | Any | no | None | Proposed execution plan included in the optimization prompt. |
| run | Any | no | False | Execute the governed advisor call; false returns its estimate only. |

Returns: `Any`

<a id="core-a17c133ed591f37bb800091483a2a96a14fe2e3013f48724b37a39353cadb617"></a>

### `spendguard.advisor:recommend_models`

Source: `src/spendguard/advisor.py:294`

Agentic TOP-K model recommendation for an intent, on the cost×quality frontier with an intent-set quality
BAR. The structured (schema-forced) cousin of optimize: the reasoner infers how much precision the job needs
(unless `quality_bar` is given), then ranks the CHEAPEST models that MEET that bar first. It ranks only
models that have evidence for the intent — a bakeoff is what fills in untried ones. Estimate-FIRST: run=False
returns the zero-spend estimate + candidate set; run=True makes ONE meta-caged realtime call (intent
spendguard:recommend). Returns a structured dict (quality_bar + top[] enriched with the measured $/good).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | no | None | Agentic TOP-K model recommendation for an intent, on the cost×quality frontier with an intent-set quality<br>BAR. The structured (schema-forced) cousin of optimize: the reasoner infers how much precision the job needs<br>(unless `quality_bar` is given), then ranks the CHEAPEST models that MEET that bar first. It ranks only<br>models that have evidence for the intent — a bakeoff is what fills in untried ones. Estimate-FIRST: run=False<br>returns the zero-spend estimate + candidate set; run=True makes ONE meta-caged realtime call (intent<br>spendguard:recommend). Returns a structured dict (quality_bar + top[] enriched with the measured $/good). |
| k | Any | no | 5 | Agentic TOP-K model recommendation for an intent, on the cost×quality frontier with an intent-set quality<br>BAR. The structured (schema-forced) cousin of optimize: the reasoner infers how much precision the job needs<br>(unless `quality_bar` is given), then ranks the CHEAPEST models that MEET that bar first. It ranks only<br>models that have evidence for the intent — a bakeoff is what fills in untried ones. Estimate-FIRST: run=False<br>returns the zero-spend estimate + candidate set; run=True makes ONE meta-caged realtime call (intent<br>spendguard:recommend). Returns a structured dict (quality_bar + top[] enriched with the measured $/good). |
| quality_bar | Any | no | None | Agentic TOP-K model recommendation for an intent, on the cost×quality frontier with an intent-set quality<br>BAR. The structured (schema-forced) cousin of optimize: the reasoner infers how much precision the job needs<br>(unless `quality_bar` is given), then ranks the CHEAPEST models that MEET that bar first. It ranks only<br>models that have evidence for the intent — a bakeoff is what fills in untried ones. Estimate-FIRST: run=False<br>returns the zero-spend estimate + candidate set; run=True makes ONE meta-caged realtime call (intent<br>spendguard:recommend). Returns a structured dict (quality_bar + top[] enriched with the measured $/good). |
| run | Any | no | False | Agentic TOP-K model recommendation for an intent, on the cost×quality frontier with an intent-set quality<br>BAR. The structured (schema-forced) cousin of optimize: the reasoner infers how much precision the job needs<br>(unless `quality_bar` is given), then ranks the CHEAPEST models that MEET that bar first. It ranks only<br>models that have evidence for the intent — a bakeoff is what fills in untried ones. Estimate-FIRST: run=False<br>returns the zero-spend estimate + candidate set; run=True makes ONE meta-caged realtime call (intent<br>spendguard:recommend). Returns a structured dict (quality_bar + top[] enriched with the measured $/good). |

Returns: `Any`

<a id="core-d9e1b853512a82afa78b40431bcd147dd11ca22c435da133f66b929f044bec20"></a>

### `spendguard.advisor:reconstruct_advisor_history`

Source: `src/spendguard/advisor.py:114`

Judge a bounded sample of the recovered call_io corpus for quality → makes good% / $/good real.
Estimate-only unless run=True. Realtime judge (synchronous, small, caged by intent spendguard:*).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| run | Any | no | False | Judge a bounded sample of the recovered call_io corpus for quality → makes good% / $/good real.<br>Estimate-only unless run=True. Realtime judge (synchronous, small, caged by intent spendguard:*). |
| per | Any | no | 15 | Judge a bounded sample of the recovered call_io corpus for quality → makes good% / $/good real.<br>Estimate-only unless run=True. Realtime judge (synchronous, small, caged by intent spendguard:*). |
| limit | Any | no | None | Judge a bounded sample of the recovered call_io corpus for quality → makes good% / $/good real.<br>Estimate-only unless run=True. Realtime judge (synchronous, small, caged by intent spendguard:*). |

Returns: `Any`

<a id="core-f77c7da4899f31a23ea0dc3cff1e702a0414db79bbc295d14a244803469d2998"></a>

### `spendguard.agent_spawn_gate:evaluate_pretooluse`

Source: `src/spendguard/agent_spawn_gate.py:91`

Return Claude Code PreToolUse JSON for one Agent/Task spawn payload.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| payload | Any | yes | required | Return Claude Code PreToolUse JSON for one Agent/Task spawn payload. |

Returns: `Any`

<a id="core-32a35d127dfac3137d94292ff042230753e4b5f62945c92053279ea0abea54b1"></a>

### `spendguard.agent_spawn_gate:install_agent_gate`

Source: `src/spendguard/agent_spawn_gate.py:173`

Merge or remove only spendguard's Agent|Task PreToolUse hook; back up before every changed write.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| settings_path | Any | no | None | Merge or remove only spendguard's Agent\|Task PreToolUse hook; back up before every changed write. |
| uninstall | Any | no | False | Merge or remove only spendguard's Agent\|Task PreToolUse hook; back up before every changed write. |
| executable | Any | no | None | Merge or remove only spendguard's Agent\|Task PreToolUse hook; back up before every changed write. |

Returns: `Any`

<a id="core-8712d8c9d2166d496e41346685492f107557eb2d7793d41a82aca1b60f18a7c3"></a>

### `spendguard.audit:audit_repository_price_literals`

Source: `src/spendguard/audit.py:161 (audit.main)`

Audit repository spend integration with optional deep adjudication Extract scan orchestration over module SCRIPTS/EXCLUDE_DIRS; preserve skipped coverage, optional deep(scanned,PRICING,run), ci-only failures. Audited CLI invocation: spendguard audit --ci --deep --run.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| ci | bool | no | false | Extract scan orchestration over module SCRIPTS/EXCLUDE_DIRS; preserve skipped coverage, optional deep(scanned,PRICING,run), ci-only failures. |
| deep | bool | no | false | Extract scan orchestration over module SCRIPTS/EXCLUDE_DIRS; preserve skipped coverage, optional deep(scanned,PRICING,run), ci-only failures. |
| run | bool | no | false | Extract scan orchestration over module SCRIPTS/EXCLUDE_DIRS; preserve skipped coverage, optional deep(scanned,PRICING,run), ci-only failures. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-e7b3e0ec183e79e320a0a194baf0c3dcec9b6f6799c982378f2f52acb27940a9"></a>

### `spendguard.backfill:backfill`

Source: `src/spendguard/backfill.py:73`

Ingest the real batch ledgers into `calls` + the learning graph. Returns (rows, dollars).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent_map | Any | no | None | Ingest the real batch ledgers into `calls` + the learning graph. Returns (rows, dollars). |
| providers | Any | no | ('openai', 'anthropic') | Ingest the real batch ledgers into `calls` + the learning graph. Returns (rows, dollars). |

Returns: `Any`

<a id="core-0a386423d2156252798bb6307054e9cf35ca1df1e68efd1d5fd5817741f4b3e8"></a>

### `spendguard.bakeoff:bakeoff`

Source: `src/spendguard/bakeoff.py:189`

Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)
record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to
test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False
returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds
`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a
measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to
the reading it re-runs (a lineage).

`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each
candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.
Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,
model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The
per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this
broad explorer's.

`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):
it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`
(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied
criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a
ceiling. Returns a structured dict.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | yes | required | Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)<br>record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to<br>test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False<br>returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds<br>`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a<br>measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to<br>the reading it re-runs (a lineage).<br><br>`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each<br>candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.<br>Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,<br>model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The<br>per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this<br>broad explorer's.<br><br>`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):<br>it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`<br>(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied<br>criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a<br>ceiling. Returns a structured dict. |
| candidates | Any | no | None | Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)<br>record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to<br>test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False<br>returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds<br>`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a<br>measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to<br>the reading it re-runs (a lineage).<br><br>`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each<br>candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.<br>Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,<br>model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The<br>per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this<br>broad explorer's.<br><br>`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):<br>it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`<br>(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied<br>criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a<br>ceiling. Returns a structured dict. |
| prompts | Any | no | None | Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)<br>record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to<br>test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False<br>returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds<br>`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a<br>measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to<br>the reading it re-runs (a lineage).<br><br>`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each<br>candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.<br>Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,<br>model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The<br>per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this<br>broad explorer's.<br><br>`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):<br>it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`<br>(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied<br>criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a<br>ceiling. Returns a structured dict. |
| sample_n | Any | no | 5 | Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)<br>record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to<br>test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False<br>returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds<br>`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a<br>measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to<br>the reading it re-runs (a lineage).<br><br>`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each<br>candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.<br>Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,<br>model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The<br>per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this<br>broad explorer's.<br><br>`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):<br>it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`<br>(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied<br>criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a<br>ceiling. Returns a structured dict. |
| run | Any | no | False | Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)<br>record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to<br>test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False<br>returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds<br>`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a<br>measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to<br>the reading it re-runs (a lineage).<br><br>`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each<br>candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.<br>Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,<br>model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The<br>per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this<br>broad explorer's.<br><br>`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):<br>it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`<br>(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied<br>criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a<br>ceiling. Returns a structured dict. |
| budget_usd | Any | no | None | Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)<br>record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to<br>test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False<br>returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds<br>`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a<br>measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to<br>the reading it re-runs (a lineage).<br><br>`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each<br>candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.<br>Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,<br>model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The<br>per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this<br>broad explorer's.<br><br>`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):<br>it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`<br>(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied<br>criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a<br>ceiling. Returns a structured dict. |
| judge_model | Any | no | None | Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)<br>record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to<br>test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False<br>returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds<br>`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a<br>measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to<br>the reading it re-runs (a lineage).<br><br>`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each<br>candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.<br>Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,<br>model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The<br>per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this<br>broad explorer's.<br><br>`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):<br>it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`<br>(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied<br>criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a<br>ceiling. Returns a structured dict. |
| parent_reading | Any | no | None | Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)<br>record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to<br>test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False<br>returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds<br>`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a<br>measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to<br>the reading it re-runs (a lineage).<br><br>`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each<br>candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.<br>Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,<br>model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The<br>per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this<br>broad explorer's.<br><br>`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):<br>it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`<br>(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied<br>criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a<br>ceiling. Returns a structured dict. |
| efforts | Any | no | None | Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)<br>record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to<br>test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False<br>returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds<br>`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a<br>measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to<br>the reading it re-runs (a lineage).<br><br>`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each<br>candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.<br>Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,<br>model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The<br>per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this<br>broad explorer's.<br><br>`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):<br>it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`<br>(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied<br>criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a<br>ceiling. Returns a structured dict. |
| requirement_aware | Any | no | False | Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)<br>record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to<br>test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False<br>returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds<br>`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a<br>measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to<br>the reading it re-runs (a lineage).<br><br>`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each<br>candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.<br>Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,<br>model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The<br>per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this<br>broad explorer's.<br><br>`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):<br>it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`<br>(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied<br>criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a<br>ceiling. Returns a structured dict. |
| adjudicator_model | Any | no | None | Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)<br>record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to<br>test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False<br>returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds<br>`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a<br>measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to<br>the reading it re-runs (a lineage).<br><br>`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each<br>candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.<br>Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,<br>model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The<br>per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this<br>broad explorer's.<br><br>`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):<br>it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`<br>(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied<br>criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a<br>ceiling. Returns a structured dict. |
| temperature | Any | no | None | Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)<br>record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to<br>test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False<br>returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds<br>`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a<br>measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to<br>the reading it re-runs (a lineage).<br><br>`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each<br>candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.<br>Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,<br>model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The<br>per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this<br>broad explorer's.<br><br>`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):<br>it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`<br>(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied<br>criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a<br>ceiling. Returns a structured dict. |
| top_p | Any | no | None | Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)<br>record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to<br>test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False<br>returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds<br>`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a<br>measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to<br>the reading it re-runs (a lineage).<br><br>`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each<br>candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.<br>Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,<br>model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The<br>per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this<br>broad explorer's.<br><br>`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):<br>it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`<br>(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied<br>criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a<br>ceiling. Returns a structured dict. |
| seed | Any | no | None | Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)<br>record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to<br>test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False<br>returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds<br>`budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a<br>measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to<br>the reading it re-runs (a lineage).<br><br>`efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each<br>candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.<br>Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,<br>model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The<br>per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this<br>broad explorer's.<br><br>`requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):<br>it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`<br>(default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied<br>criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a<br>ceiling. Returns a structured dict. |

Returns: `Any`

<a id="core-72a6b6e21315b4fe9774ac9699ce45b256f22111f798380ccb8c40cb171d95da"></a>

### `spendguard.balances:all_balances`

Source: `src/spendguard/balances.py:286`

Per-vendor balance for EVERY provider we can price: the API-endpoint vendors (deepseek/moonshot — real
balance) PLUS every config-declared vendor with no endpoint (openai/anthropic/gemini — declared-minus-spend).
One 'how much is available' view across all providers, from cache (or a live refresh of the API ones).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| refresh | Any | no | False | Source parameter refresh at src/spendguard/balances.py:286; see complete callable contract. |

Returns: `Any`

<a id="core-73ce6edb6b10c3c98a01f1444af743e0732d056374df12f30baf70f22ca0e098"></a>

### `spendguard.balances:refresh_balances`

Source: `src/spendguard/balances.py:240`

Re-fetch + re-read every keyed vendor's balance and write the cache atomically. Returns the per-vendor
result list. $0 fetches; the agentic read runs only for a vendor whose raw balance changed.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| vendors | Any | no | None | Source parameter vendors at src/spendguard/balances.py:240; see complete callable contract. |

Returns: `Any`

<a id="core-be53ce2abbc8feb169b28c9cfc604e5cef3d13d962cf106aa4f4e6b62b60a219"></a>

### `spendguard.batch_tracker:status`

Source: `src/spendguard/batch_tracker.py:628`

{open, failing, settled, failed, total} counts of tracked batch jobs — observability for the CLI / receipt. $0.
Returns {} on a non-stop DB error (a stop/lock propagates).

Takes no parameters.

Returns: `Any`

<a id="core-b6cb85c431fbc089694468330259d25681e6f54390f4101e99d9b71e2f7e9f45"></a>

### `spendguard.bootstrap:bootstrap`

Source: `src/spendguard/bootstrap.py:23`

Recover free historical evidence, then estimate or run separately guarded reasoning steps.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| repo | Any | no | None | Repository used for intent reconstruction; omission uses the current directory. |
| transcripts | Any | no | None | Transcript directory passed to conversation indexing and synthesis. |
| run | Any | no | False | Execute governed reasoning steps after free recovery; false estimates those steps only. |
| cap | Any | no | 50 | Maximum provider-history batches passed to call I/O recovery. |

Returns: `Any`

<a id="core-cd9e70ee7c4814e749cc50e5700383f4070a51f29048fd960f86c8b0417c2e13"></a>

### `spendguard.brief:brief`

Source: `src/spendguard/brief.py:79`

Build a task brief from intent history and defaults, with optional governed refinement.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| task | Any | yes | required | Task text used for intent matching and the resulting brief. |
| intent | Any | no | None | Explicit job-type intent; omission invokes existing intent matching. |
| run_llm | Any | no | False | Run the optional governed LLM refinement after constructing the brief. |

Returns: `Any`

<a id="core-7c40f8353d3f358d9d6d9a6450bb4e8b660c1e5c7f773457428908418693869b"></a>

### `spendguard.budget:by_key`

Source: `src/spendguard/budget.py:1336`

{(provider, key_fp): {'cost': $, 'calls': n}} of gate-recorded workload — per-KEY spend (which
workspace/project key did this money flow through?). Excludes meta and every reconcile marker row
(mirror/backfill rows carry no key). key_fp '' groups as '(none)' — rows recorded before key stamping
existed, or where no key env was resolvable. LOCAL-ONLY view; fingerprints never leave the machine.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Source parameter since at src/spendguard/budget.py:1336; see complete callable contract. |

Returns: `Any`

<a id="core-3e8b82031ed5205f9685a5d0e4b5fdd69e918060a897a412fd566d552dc3bd3b"></a>

### `spendguard.budget:quarantine_charge`

Source: `src/spendguard/budget.py:855`

Void ONE spend_events row as an impossible estimate — status='void', so it drops out of every total
while staying fully auditable (the amount is untouched; the change is logged through the hash chain by
update()).

Target by `row` (the spend_events id — exact) or by `ts` (ts_utc). Up to six rows can share one second, so
a ts matching more than one RAISES rather than voiding all of them: silently voiding five innocent rows to
quarantine one bad one would be a worse version of the bug this repairs. Returns the number voided.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| ts | Any | no | None | Source parameter ts at src/spendguard/budget.py:855; see complete callable contract. |
| reason | Any | no | '' | Source parameter reason at src/spendguard/budget.py:855; see complete callable contract. |
| row | Any | no | None | Source parameter row at src/spendguard/budget.py:855; see complete callable contract. |

Returns: `Any`

<a id="core-12507e4a027ddb6af31bc106bc1a7107de12de4d0c4b3dc7f761f7e22cbaf633"></a>

### `spendguard.budget:reattribute_providers`

Source: `src/spendguard/budget.py:428`

Find (and optionally correct) charges whose `provider` disagrees with the model registry.

WHY THIS IS A FUNCTION AND NOT A ONE-OFF SCRIPT. The gate inferred a charge's vendor with
`"anthropic" if model.startswith("claude") else "openai"`, so every OpenAI-COMPATIBLE vendor was
recorded as OpenAI: measured, 695 rows and $30.26 of Moonshot and z.ai spend sat on the OpenAI line.
`saas reconcile` compares the ledger to provider billing PER PROVIDER, so one line was over-attributed
by exactly what the others were missing, and the leak verdict drawn from them was wrong twice over.
The inference is fixed, but a ledger that cannot repair its own history is not an accounting record —
and the same class of drift will happen again the next time a vendor is added.

DRY BY DEFAULT. Returns what it WOULD change; `apply=True` writes. Every correction is journalled to
spend_audit with the old and new value, because a silent restatement is indistinguishable from the
error it corrects — and in a forensic tool the correction has to be as visible as the mistake.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| apply | Any | no | False | Source parameter apply at src/spendguard/budget.py:428; see complete callable contract. |
| actor | Any | no | 'reattribute_providers' | Source parameter actor at src/spendguard/budget.py:428; see complete callable contract. |

Returns: `Any`

<a id="core-a32b2761c6af674017384df0af14a08e52ca728b74a689d3c087ffd1e6dd0da6"></a>

### `spendguard.budget:reprice_unpriced`

Source: `src/spendguard/budget.py:943`

Retroactively PRICE 'unpriced' spend_events rows that are NOW priceable (a rate resolves for their exact
(provider, model)). The tokens were always real; only the $ was unknown at gate time — the model had no price
yet, or its provider was ambiguous. This gives each its catalog-rate cost through the ledger's OWN correction
path: update() revises an OPEN row in place (cost_basis 'unpriced'→'estimate', sets the money column + rates,
so it drops out of the 'cannot price' view AND folds into the total); a row in a LOCKED period gets an adjust()
DELTA instead (the $ folds in; the immutable original keeps its marker, so a closed period is never edited).
A row whose price STILL can't resolve is LEFT unpriced — honest, never a guessed number.

DRY-RUN by default: returns a plan [{id, provider, model, in_tok, out_tok, cost, method}] (method ∈
update|adjust|skip). `apply=True` commits through update()/adjust() — both audited + revision-bumped, never raw
SQL. Filter to one `model` (e.g. 'glm-5.2') or sweep every unpriced model.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | Any | no | None | Retroactively PRICE 'unpriced' spend_events rows that are NOW priceable (a rate resolves for their exact<br>(provider, model)). The tokens were always real; only the $ was unknown at gate time — the model had no price<br>yet, or its provider was ambiguous. This gives each its catalog-rate cost through the ledger's OWN correction<br>path: update() revises an OPEN row in place (cost_basis 'unpriced'→'estimate', sets the money column + rates,<br>so it drops out of the 'cannot price' view AND folds into the total); a row in a LOCKED period gets an adjust()<br>DELTA instead (the $ folds in; the immutable original keeps its marker, so a closed period is never edited).<br>A row whose price STILL can't resolve is LEFT unpriced — honest, never a guessed number.<br><br>DRY-RUN by default: returns a plan [{id, provider, model, in_tok, out_tok, cost, method}] (method ∈<br>update\|adjust\|skip). `apply=True` commits through update()/adjust() — both audited + revision-bumped, never raw<br>SQL. Filter to one `model` (e.g. 'glm-5.2') or sweep every unpriced model. |
| apply | Any | no | False | Retroactively PRICE 'unpriced' spend_events rows that are NOW priceable (a rate resolves for their exact<br>(provider, model)). The tokens were always real; only the $ was unknown at gate time — the model had no price<br>yet, or its provider was ambiguous. This gives each its catalog-rate cost through the ledger's OWN correction<br>path: update() revises an OPEN row in place (cost_basis 'unpriced'→'estimate', sets the money column + rates,<br>so it drops out of the 'cannot price' view AND folds into the total); a row in a LOCKED period gets an adjust()<br>DELTA instead (the $ folds in; the immutable original keeps its marker, so a closed period is never edited).<br>A row whose price STILL can't resolve is LEFT unpriced — honest, never a guessed number.<br><br>DRY-RUN by default: returns a plan [{id, provider, model, in_tok, out_tok, cost, method}] (method ∈<br>update\|adjust\|skip). `apply=True` commits through update()/adjust() — both audited + revision-bumped, never raw<br>SQL. Filter to one `model` (e.g. 'glm-5.2') or sweep every unpriced model. |
| actor | Any | no | 'reprice' | Retroactively PRICE 'unpriced' spend_events rows that are NOW priceable (a rate resolves for their exact<br>(provider, model)). The tokens were always real; only the $ was unknown at gate time — the model had no price<br>yet, or its provider was ambiguous. This gives each its catalog-rate cost through the ledger's OWN correction<br>path: update() revises an OPEN row in place (cost_basis 'unpriced'→'estimate', sets the money column + rates,<br>so it drops out of the 'cannot price' view AND folds into the total); a row in a LOCKED period gets an adjust()<br>DELTA instead (the $ folds in; the immutable original keeps its marker, so a closed period is never edited).<br>A row whose price STILL can't resolve is LEFT unpriced — honest, never a guessed number.<br><br>DRY-RUN by default: returns a plan [{id, provider, model, in_tok, out_tok, cost, method}] (method ∈<br>update\|adjust\|skip). `apply=True` commits through update()/adjust() — both audited + revision-bumped, never raw<br>SQL. Filter to one `model` (e.g. 'glm-5.2') or sweep every unpriced model. |

Returns: `Any`

<a id="core-29d7d8dee922197176e01c2f6ae85f9faef9b53bd9733d95b4d77e1bae448ddf"></a>

### `spendguard.budget:spent_since`

Source: `src/spendguard/budget.py:787`

Gate-recorded workload $ since `day` — the LLM cap's number, now read from spend_events via the single
`_COUNTABLE` filter (batch+realtime, excluding meta / reconciliation / voided-impossible / reconstructed).
`deadline_s` optionally bounds this aggregate by a relative wall-clock duration; expiry raises
LedgerQueryDeadline rather than returning an incomplete spend value.
Optionally SCOPE to a `project` (repo) and/or `conv` (conversation) — the receipt uses this to show what is
relevant to the current repo/conversation, not a global sum.

Repointed onto spend_events (the money-of-record). This is LLM-only by construction: GPU/remote spend has
its OWN cap (resources.compute_exceeded) and is no longer lumped into the LLM total as the old
countable_charges view did (charges carried no remote rows, so the live number is unchanged).

CLI DELIBERATE variant restriction: In-process budget aggregate; command consumers use existing ledger-read aggregate operations.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| day | Any | yes | required | Inclusive earliest workload spend timestamp or day. |
| project | Any | no | None | Optional repository project scope. |
| conv | Any | no | None | Optional conversation scope. |
| deadline_s | Any | no | config.LEDGER_QUERY_DEADLINE_S | Relative aggregate deadline; expiry raises LedgerQueryDeadline. |
| read_only | Any | no | False | Force the bounded snapshot even for an initialized owner; consumers snapshot automatically. |

Returns: `Any`

<a id="core-5abbb770d9b0ed78ddc275faefaab001dc08d7bfc372b69907b34ecc25bb0ecf"></a>

### `spendguard.budget:suspect_batches`

Source: `src/spendguard/budget.py:841`

Batch spend since `since`, with the token counts, so an operator can SEE the arithmetic behind a number
they doubt. `row` is the spend_events id — the exact handle for `spendguard quarantine --row`. Batch rows
come from in_category('batch') (the batch money column populated — the schema's own definition), and the
tokens live on the row (in_tok/out_tok) so there's no `calls` join; `actor` carries the caller.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | yes | required | Source parameter since at src/spendguard/budget.py:841; see complete callable contract. |

Returns: `Any`

<a id="core-433da6e40256bb6ce6a7321a3a95a78d5556bd9c1aed3a57b266d74cbe11cef2"></a>

### `spendguard.cacheaudit:audit_cache_opportunities`

Source: `src/spendguard/cacheaudit.py:155`

Report realized cache hits, intent-prefix opportunities and repository prompt scripts.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| repo | Any | no | None | Repository scanned for prompt scripts; omission uses the current directory. |

Returns: `Any`

<a id="core-cacc7c885585b768e262aa1bd88d9a8af5cb64d1b3f6746df3556463c59b43ef"></a>

### `spendguard.cachetest:cache_test`

Source: `src/spendguard/cachetest.py:98`

Estimate, or explicitly execute, a cold/warm prompt-cache experiment.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| system | Any | yes | required | Repeated system prompt; the existing minimum token check applies. |
| users | Any | yes | required | User prompts for the experiment; None selects the existing sample prompts and an explicit empty list stays empty. |
| model | Any | no | None | Priced model to test; omission uses the configured advisor judge. |
| run | Any | no | False | Execute cold and warm calls; false reports the estimate only. |

Returns: `Any`

<a id="core-d03047976b8a014f07d77d9ea3c437d97afd0d408220a83418e0bdf5d633c246"></a>

### `spendguard.calibrate:backtest`

Source: `src/spendguard/calibrate.py:504`

Held-out replay on the REAL corpus: per (label, model, transport) cell, train on the earliest
70% (time order), predict each held-out row's $ two ways, score median abs % error (MAPE).
  naive      = row's own in_tok (PERFECT input knowledge — generous to naive) × flat realtime
               rates, output = the train max (the cap a careful engineer would set).
  calibrated = estimate(…, as_of=train end) with the SAME inputs.
The deck is stacked FOR naive on input; the learned win must come from output-fill, transport
and residual — exactly the claimed error sources. Ship gate: calibrated beats naive overall.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| as_json | Any | no | False | Source parameter as_json at src/spendguard/calibrate.py:504; see complete callable contract. |
| min_cell | Any | no | MIN_CELL_BACKTEST | Source parameter min_cell at src/spendguard/calibrate.py:504; see complete callable contract. |

Returns: `Any`

<a id="core-e84bfce560d1b757ab84e26e5ba75422b20d5ac3fa545a7790d3cd07e0f7a581"></a>

### `spendguard.calibrate:fetch_shared`

Source: `src/spendguard/calibrate.py:641`

Pull the org-aggregated calibration (n-weighted across members) and cache it — estimate() then
shrinks local stats toward the ORG's experience instead of the naive assumption.

Takes no parameters.

Returns: `Any`

<a id="core-f8dccbb7bf58302418c4907a6a169e5bfaa19622a5d59fac1d4f4b1e6b69f7ab"></a>

### `spendguard.calibrate:pair`

Source: `src/spendguard/calibrate.py:455`

Join predictions to captured actuals once their window has closed. Exact join first
(calls.chain == job_id, any age); else label+model calls inside [pred_ts, pred_ts+horizon].
A closed window with no actuals is marked expired (actual_calls=0) — UNKNOWN stays visible,
it never reads as $0 spent. Idempotent. Returns {paired, expired, pending}.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| now | Any | no | None | Source parameter now at src/spendguard/calibrate.py:455; see complete callable contract. |

Returns: `Any`

<a id="core-472d9c47e3796a9c3c592a3bc5452700891aefeb6aa3e10c6a9f2593d6af26c4"></a>

### `spendguard.calibrate:predict_cost`

Source: `src/spendguard/calibrate.py:352`

Predict a planned job's $ from OUR history. est_in_tokens / est_out_max are PER REQUEST
(what the caller knows: rendered-prompt tokens and the max_tokens it will set); n = requests.
Returns {p50_usd, p90_usd, level, n_obs, basis, naive_usd, per_request:{in_p50, out_p50}, …}.
p90 compounds each quantity's p90 (conservative upper band). Zero spend — sqlite + pricing.py.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| label | Any | yes | required | Source parameter label at src/spendguard/calibrate.py:352; see complete callable contract. |
| n | Any | no | 1 | Source parameter n at src/spendguard/calibrate.py:352; see complete callable contract. |
| model | Any | no | None | Source parameter model at src/spendguard/calibrate.py:352; see complete callable contract. |
| transport | Any | no | 'batch' | Source parameter transport at src/spendguard/calibrate.py:352; see complete callable contract. |
| est_in_tokens | Any | no | None | Source parameter est_in_tokens at src/spendguard/calibrate.py:352; see complete callable contract. |
| est_out_max | Any | no | None | Source parameter est_out_max at src/spendguard/calibrate.py:352; see complete callable contract. |
| as_of | Any | no | None | Source parameter as_of at src/spendguard/calibrate.py:352; see complete callable contract. |

Returns: `Any`

<a id="core-98486b3d303a93551f31063bc3a903d45894fd819cf4b97516a2afe2a751f27f"></a>

### `spendguard.calibrate:push_shared`

Source: `src/spendguard/calibrate.py:634`

Package this install's calibration statistics and push them to the org server (visibility-gated,
de-id'd labels, statistics only). Everyone contributes; everyone's prior improves.

Takes no parameters.

Returns: `Any`

<a id="core-58f1db2228460c48a6f5ce5814264a2223f46f6f0549a1659fd72bed4438a619"></a>

### `spendguard.calibrate:show`

Source: `src/spendguard/calibrate.py:679`

Print the calibration state — what has been learned, at what confidence.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| label | Any | no | None | Source parameter label at src/spendguard/calibrate.py:679; see complete callable contract. |

Returns: `Any`

<a id="core-31e3e87d5d0c535161cf8afad6c64bd151823006b9de9d4203ecfcc005584877"></a>

### `spendguard.callio:collect_chat_tasks`

Source: `src/spendguard/callio.py:670`

The SETTLE twin of submit.submit_chat_tasks — pull the FULL result set of chat batches back and key it by
custom_id, closing the loop (submit → wait → collect) so submitted results never sit uncollected. A THIN wrapper
over batch_status (readiness) + guarded_collect (the streaming, $0, custom_id-paired full pull) — it never
re-implements the download; it materialises the generator into a settled dict the caller can join to its tasks.

Returns {results, failed, anomalies, not_ready, collected, batches}:
  · results   {custom_id: text}   — every succeeded row, keyed by the custom_id submit_chat_tasks assigned
                                      (a dict task's own id, or 'task-<i>' positional WITHIN the submitted set);
  · failed    {custom_id: error}  — a per-request failure, surfaced by id (never a silent drop);
  · anomalies [ {error, raw} ]    — an output row with a missing/unparseable custom_id (nothing to key it by);
  · not_ready [batch_id]          — with require_ready, a batch whose output file isn't ready yet is SKIPPED
                                      here (poll again later): the async 24h window is never blocked on;
  · collected int, batches int.
$0 (a finished batch already billed at submit; this only STREAMS the output file). OpenAI-only (the chat Batch API
is). record_io=True also captures each row into the (intent, model) quality corpus (idempotent on batch+custom_id).
For a very large pull where even the results dict is too big to hold, use guarded_collect directly (it streams).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| batch_ids | Any | yes | required | Source parameter batch_ids at src/spendguard/callio.py:670; see complete callable contract. |
| intent | Any | yes | required | Source parameter intent at src/spendguard/callio.py:670; see complete callable contract. |
| model | Any | yes | required | Source parameter model at src/spendguard/callio.py:670; see complete callable contract. |
| require_ready | Any | no | True | Source parameter require_ready at src/spendguard/callio.py:670; see complete callable contract. |
| record_io | Any | no | False | Source parameter record_io at src/spendguard/callio.py:670; see complete callable contract. |

Returns: `Any`

<a id="core-ac1d68b3ff1225ef8e5c9ba0b345f249d5a866a3fca16b7240c95dbab8e72b6e"></a>

### `spendguard.callio:collect_message_batch`

Source: `src/spendguard/callio.py:740`

SETTLE twin of submit.submit_message_batch — pull the FULL result set of Anthropic Message Batch(es) back and key
it by custom_id, closing submit→wait→collect so a submitted batch never sits uncollected. The Anthropic analogue of
collect_chat_tasks (OpenAI). Returns {results, failed, anomalies, not_ready, collected, batches, usage}:
  · results   {custom_id: text}   — every SUCCEEDED result, keyed by the custom_id submit assigned. For a
                                      forced-tool/schema batch the structured tool_use.input is returned as JSON;
                                      otherwise the message's text blocks joined.
  · failed    {custom_id: error}  — a per-request errored/canceled/expired result, surfaced by id (never dropped);
  · anomalies [ {error, raw} ]    — a result with no custom_id (nothing to key it by) — surfaced, never a silent drop;
  · not_ready [batch_id]          — a batch not ENDED yet (poll again later) — the 29-day window is never blocked on;
  · collected int, batches int;
  · usage {in_tok, out_tok, cache_read, cache_creation} — the REAL billed tokens summed from each result's own
                                      usage (BOTH axes — this is the input half fetch_anthropic does not capture), so
                                      the caller sees actual consumption. ($ truth still trues up at reconcile via
                                      provider billing; this records real usage, it does not write a second $ row.)
$0 (a finished batch already billed at submit; this STREAMS the results). Anthropic-only (the Message Batches API is).
record_io=True also captures each succeeded row into the (intent, model) quality corpus with REAL in_tok+out_tok
(idempotent on batch+custom_id, so re-collecting never double-counts).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| batch_ids | Any | yes | required | Source parameter batch_ids at src/spendguard/callio.py:740; see complete callable contract. |
| intent | Any | yes | required | Source parameter intent at src/spendguard/callio.py:740; see complete callable contract. |
| model | Any | yes | required | Source parameter model at src/spendguard/callio.py:740; see complete callable contract. |
| require_ready | Any | no | True | Source parameter require_ready at src/spendguard/callio.py:740; see complete callable contract. |
| record_io | Any | no | False | Source parameter record_io at src/spendguard/callio.py:740; see complete callable contract. |

Returns: `Any`

<a id="core-b09ec82c7d4e35675d6110f99587223c10127ce7fb94d874e8de8a83953177e3"></a>

### `spendguard.callio:fetch_history`

Source: `src/spendguard/callio.py:481`

Fill call_io from provider batch I/O, bounded to `cap` per (intent, model). Zero token cost.
Skips a batch entirely once its (intent, model) quota is met — so total downloads stay bounded.

Runs inside budget.reading_history(): downloading a batch INPUT file hands the gate bytes that look
exactly like a batch about to be submitted, and it duly estimated them as new spend — one read-only pass
wrote $359.63 of charges for work that had already been billed months earlier, and the daily cap then
blocked every real call. "Zero token cost" in this docstring was true and still produced a bill.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| cap | Any | no | DEFAULT_CAP | Source parameter cap at src/spendguard/callio.py:481; see complete callable contract. |
| sample_n | Any | no | None | Source parameter sample_n at src/spendguard/callio.py:481; see complete callable contract. |
| limit_batches | Any | no | None | Source parameter limit_batches at src/spendguard/callio.py:481; see complete callable contract. |

Returns: `Any`

<a id="core-2cca63d5198d65f5b17141dde17b7944d7ee0a7ed97b2e12b56ec541690804b6"></a>

### `spendguard.callio:link_conversations`

Source: `src/spendguard/callio.py:321`

Stamp each recovered per-request row with the conversation that spawned its batch + the RICH pre/post
context (~`turns` message turns before AND after the batch mention — the why/outcome, not a one-liner). The
call→conversation join is the batch id. Re-links (overwrites) so old one-line snippets upgrade. Returns rows.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| tdir | Any | no | None | Source parameter tdir at src/spendguard/callio.py:321; see complete callable contract. |
| turns | Any | no | 10 | Source parameter turns at src/spendguard/callio.py:321; see complete callable contract. |

Returns: `Any`

<a id="core-b9b23e491c9e01b062cb1215bef1787a3bd8aeb423939f584a03e01c2148c97f"></a>

### `spendguard.callio:status_rows`

Source: `src/spendguard/callio.py:826`

Per (intent, model): total samples, REPLAYABLE (truncated=0 — the rows bakeoff/effort-titrate actually
sample), LIVE (source='live_io'), and JUDGED. Optionally kept to intents whose name CONTAINS any of `intents`
(substring, mechanical — same filter shape as the matrices). $0, read-only: the corpus-fill view to consult
BEFORE estimating a sweep, so you can see which intents have enough replay bodies yet.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intents | Any | no | None | Per (intent, model): total samples, REPLAYABLE (truncated=0 — the rows bakeoff/effort-titrate actually<br>sample), LIVE (source='live_io'), and JUDGED. Optionally kept to intents whose name CONTAINS any of `intents`<br>(substring, mechanical — same filter shape as the matrices). $0, read-only: the corpus-fill view to consult<br>BEFORE estimating a sweep, so you can see which intents have enough replay bodies yet. |

Returns: `Any`

<a id="core-ff8fcc845d5b8f6400dab80e066583b4e7e801effe6da5281f6de5f1c1c916d6"></a>

### `spendguard.calls:cost_summary`

Source: `src/spendguard/calls.py:968`

Per (intent, model): calls, $ total, %good, and cost-per-good-result.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | no | None | Per (intent, model): calls, $ total, %good, and cost-per-good-result. |

Returns: `Any`

<a id="core-338ee42e079697612ca024dd53e80591dc88fba822e615dfb983a69793a6e9d5"></a>

### `spendguard.capability_readiness:assess_capability_readiness`

Source: `src/spendguard/capability_readiness.py`

Run every declared readiness gate and return revision, coverage, ready and item evidence. Missing or unavailable evidence is UNREVIEWED; ready requires every item PASS.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| repo_root | str \| None | no | None | Repository checkout whose deterministic readiness gates and source fingerprint are assessed; omitted uses the current working directory. |
| environment_report | str \| None | no | None | Path to independently produced clean-environment verification evidence; absent evidence remains UNREVIEWED. |

Returns: `dict`

<a id="core-af3c9425d056c61cb4885c201af073c5d2d56a68f9bd8f7356a7276f2f9c647a"></a>

### `spendguard.capability_readiness:run_capability_selfcheck`

Source: `src/spendguard/capability_readiness.py`

Print every readiness outcome and coverage. Return zero only when every item passes or an UNREVIEWED item is explicitly named by tolerate_unreviewed; FAIL, other UNREVIEWED items, and malformed manifests return nonzero.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| repo_root | str \| None | no | None | Repository checkout whose deterministic readiness gates and source fingerprint are assessed; omitted uses the current working directory. |
| environment_report | str \| None | no | None | Path to independently produced clean-environment verification evidence; absent evidence remains UNREVIEWED. |
| tolerate_unreviewed | tuple[str, ...] {"alternatives": [], "enum": [], "items": {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, "properties": [], "python_annotation": "tuple[str, ...]", "type": "array"} | no | () | Repeatable readiness check identifier whose UNREVIEWED result does not make the process exit nonzero; rendering is unchanged and FAIL is never tolerated. |
| json_output | bool | no | False | Print the complete structured readiness report instead of the human-readable checklist; does not alter readiness or exit status. |

Returns: `int`

<a id="core-d1f1430b4b40ae43360370c6cb3729c5405c102e811d6aa21c14cc1e9a7a96a6"></a>

### `spendguard.cascade:cascade`

Source: `src/spendguard/cascade.py:38`

Route prompt down the ladder (cheapest→strongest). Returns dict with the serving model + costs.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| prompt | Any | yes | required | Route prompt down the ladder (cheapest→strongest). Returns dict with the serving model + costs. |
| ladder | Any | yes | required | Route prompt down the ladder (cheapest→strongest). Returns dict with the serving model + costs. |
| verify | Any | no | None | Route prompt down the ladder (cheapest→strongest). Returns dict with the serving model + costs. CLI DELIBERATE parameter restriction: A Python verifier callable cannot be transported as JSON. MCP DELIBERATE parameter restriction: A Python verifier callable cannot be transported as JSON. |
| intent | Any | no | None | Route prompt down the ladder (cheapest→strongest). Returns dict with the serving model + costs. |
| _caller | Any | no | None | Route prompt down the ladder (cheapest→strongest). Returns dict with the serving model + costs. CLI DELIBERATE parameter restriction: A Python execution callback cannot be transported as JSON. MCP DELIBERATE parameter restriction: A Python execution callback cannot be transported as JSON. |

Returns: `Any`

<a id="core-3e64d2cfd9c646d6b699458744435a73e0de3cf1a1c4f6ba0bda82092561e82d"></a>

### `spendguard.catalog:pull_live_catalog`

Source: `src/spendguard/catalog.py:30`

Fetch the live model-id list for every KEYED provider (or the given subset) via vendor_call.list_models and
write the cache atomically. Returns (n_providers_ok, n_ids, errors_by_provider). Fail-soft PER provider: one
provider's fetch error is recorded and the rest still cache — a partial catalog is better than none, and the
error is surfaced, never silent.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| providers | Any | no | None | Source parameter providers at src/spendguard/catalog.py:30; see complete callable contract. |
| timeout_s | Any | no | 20 | Source parameter timeout_s at src/spendguard/catalog.py:30; see complete callable contract. |

Returns: `Any`

<a id="core-b54ba257b4ca32b9421b050fd32f842bf0b561892e8ffcebe98a3c3d5b485558"></a>

### `spendguard.chat:accept_chat_org_taxonomy`

Source: `src/spendguard/chat.py:1072 (chat.main)`

Accept the organization attribution taxonomy Promote unchanged; enable, clear pending request, pull taxonomy; deferred pull errors still returns0. Audited CLI invocation: spendguard chat accept; governing flags: see command inventory `chat`.

Takes no parameters.

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-68a183d1490cca16f5b2f1762864dd6cbfcf7d690b8a0f96c7e1f5887b11e260"></a>

### `spendguard.chat:classify_chat_allocations`

Source: `src/spendguard/chat.py:861`

Agentically assign (org, project) to conversations from their content. Caged (spendguard:categorize),
estimate-first. Only classifies UNCLASSIFIED conversations unless --reclassify.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| run | Any | no | False | Source parameter run at src/spendguard/chat.py:861; see complete callable contract. |
| days | Any | no | None | Source parameter days at src/spendguard/chat.py:861; see complete callable contract. |
| recls | Any | no | False | Source parameter recls at src/spendguard/chat.py:861; see complete callable contract. |
| batch_size | Any | no | 25 | Source parameter batch_size at src/spendguard/chat.py:861; see complete callable contract. |

Returns: `Any`

<a id="core-556cc1a8d2f8911bc043ddae16e2670631bb813f78ff4b9623c2b91d84ec2710"></a>

### `spendguard.chat:diagnose_connection`

Source: `src/spendguard/chat.py:997`

Diagnose fresh cookie decryption and chat API connectivity, returning a nonzero status on failure.

Takes no parameters.

Returns: `Any`

<a id="core-eb5f6e6f6c49d2a7bc4fd91350abbba198cc78d1e5e3828d87d56886d470a1c9"></a>

### `spendguard.chat:discover_taxonomy`

Source: `src/spendguard/chat.py:750`

AGENTICALLY propose the org→project taxonomy from the conversation corpus (caged, estimate-first). Writes the
proposal to config chat.taxonomy (review/edit it), so classification doesn't depend on hand-written config.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| run | Any | no | False | Source parameter run at src/spendguard/chat.py:750; see complete callable contract. |
| days | Any | no | None | Source parameter days at src/spendguard/chat.py:750; see complete callable contract. |
| sample | Any | no | None | Source parameter sample at src/spendguard/chat.py:750; see complete callable contract. |
| apply | Any | no | True | Source parameter apply at src/spendguard/chat.py:750; see complete callable contract. |

Returns: `Any`

<a id="core-0d3b02df6e948747be2ec9928fbb5fef640a8a4fac7b3cc73e7f9f4b2807235a"></a>

### `spendguard.chat:loop`

Source: `src/spendguard/chat.py:939`

ONE iteration of the attribution loop — the engine behind both user-self-serve and org-requested activation:
  1. fetch new conversations (incremental watermark)
  2. classify the unclassified (caged; estimate-only unless run)
  3. PERIODIC taxonomy review (every chat.discover_days): if chat.auto_taxonomy → discover+apply+reallocate;
     else just flag that a review is due (no surprise caged spend)
  4. sync the rollup to the org (org-routed, channel=claude-ai, billed=false)
Caged steps degrade gracefully (over meta budget → deferred, loop continues). Returns a result dict; prints
unless quiet. Cron-friendly: `chat loop --run` (or folded into `saas sync --if-due`).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| run | Any | no | False | Source parameter run at src/spendguard/chat.py:939; see complete callable contract. |
| force_discover | Any | no | False | Source parameter force_discover at src/spendguard/chat.py:939; see complete callable contract. |
| quiet | Any | no | False | Source parameter quiet at src/spendguard/chat.py:939; see complete callable contract. |

Returns: `Any`

<a id="core-da50b836306356f0afb3ceae461f9643fcf91e60c5366dca494a7c398d29bcb6"></a>

### `spendguard.chat:publish_chat_org_taxonomy`

Source: `src/spendguard/chat.py:1072 (chat.main)`

Push the local attribution taxonomy to the organization Extract push-taxonomy branch; saas.push_taxonomy, print prefixed result, return0. Audited CLI invocation: spendguard chat push-taxonomy; governing flags: see command inventory `chat`.

Takes no parameters.

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-eabbf4460903fce45385927ac898fb5a7d8527e22ad3ebd569dae50163cb0bfb"></a>

### `spendguard.chat:set_chat_consent`

Source: `src/spendguard/chat.py:1072 (chat.main)`

Consent to browser-chat attribution ingestion Promote unchanged; bind on=True; config.save_config and opt-in message. Audited CLI invocation: spendguard chat enable; governing flags: see command inventory `chat`.

Takes no parameters.

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-00f0659dfbccc9874fe9370841518330f062c5cf5238dd22a4166fd93c9f6952"></a>

### `spendguard.chat:show_by_project`

Source: `src/spendguard/chat.py:499`

PROJECT-first pivot — projects are orthogonal, so flip the lens: each project, with the org/team scopes that
flow INTO it. Same data as `show`, pivoted (proves the orthogonal dimension is bidirectional in the UI).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| days | Any | no | None | Source parameter days at src/spendguard/chat.py:499; see complete callable contract. |

Returns: `Any`

<a id="core-ce9c57855b3d9b03d0b3286466070ecb6ec87ec259e4df65ad636be25c7ba62c"></a>

### `spendguard.chat:show_chat_adapter_status`

Source: `src/spendguard/chat.py:1072 (chat.main)`

Read browser-chat attribution consent and source state Promote unchanged; pending org request/local conversation cache/taxonomy state, best-effort SaaS state. Audited CLI invocation: spendguard chat status; governing flags: see command inventory `chat`.

Takes no parameters.

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-1c74a075f6e7dd7a8cf1a2bf3c90295cb59a5e6440ce83beb146de12bc8b165e"></a>

### `spendguard.chat:show_chat_report`

Source: `src/spendguard/chat.py:451`

Print chat subscription usage value by organization, team and project, optionally refreshing first.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| days | Any | no | None | Trailing day window; omission reports all recorded days. |
| refresh | Any | no | True | Refresh live chat conversations before reporting; failures are printed and cached data remains usable. |

Returns: `Any`

<a id="core-7168401015ccf497937c0cda76c566ba395f0e34655a8034d626303a49df45ad"></a>

### `spendguard.chat:show_chat_work`

Source: `src/spendguard/chat.py:602`

claude.ai chat WORK DONE — per-conversation rows (title + auto-summary + value) bucketed by period (day rows
fold into the period; a multi-day conversation contributes to each day it was active).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| by | Any | no | 'week' | Source parameter by at src/spendguard/chat.py:602; see complete callable contract. |
| days | Any | no | None | Source parameter days at src/spendguard/chat.py:602; see complete callable contract. |

Returns: `Any`

<a id="core-380f107d8ba37dcb03f45c076a554751f9f7f7909849806e43f430bf553fb39b"></a>

### `spendguard.chat:story`

Source: `src/spendguard/chat.py:662`

Caged synth over the period's chat conversations → narrative STORY + private WORK INSIGHTS. Estimate-first.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| by | Any | no | 'week' | Source parameter by at src/spendguard/chat.py:662; see complete callable contract. |
| days | Any | no | 7 | Source parameter days at src/spendguard/chat.py:662; see complete callable contract. |
| run | Any | no | False | Source parameter run at src/spendguard/chat.py:662; see complete callable contract. |

Returns: `Any`

<a id="core-2c89d5fdec60f47c99adb513a0c4ce721f925f6e2b5ac80ea1b4719e34b29d0e"></a>

### `spendguard.chat:sync`

Source: `src/spendguard/chat.py:548`

Push claude.ai chat value (channel=claude-ai) → the server. OPT-IN: requires chat.enabled. Refreshes first.
Routes by agentic org: only conversations whose org matches THIS connection's org (or are unclassified) push here.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| dry | Any | no | False | Source parameter dry at src/spendguard/chat.py:548; see complete callable contract. |

Returns: `Any`

<a id="core-29fbd504a96239601a42d5978a7868b66a95ea5b8ec7f84d70d8ef2468a3d18a"></a>

### `spendguard.claudecode:attribute_overage`

Source: `src/spendguard/claudecode.py:751`

RECONCILE the real Claude Code overage (ingested invoices, source='anthropic-invoice' cc-overage) to the
CONVERSATIONS that caused it: distribute each MONTH's real overage across the conversations that were in an
observed overage window that month, PROPORTIONAL to their observable overage est-value. The invoice gives the
TOTAL truth per month; the observable window detector gives the per-conversation SHAPE. Months with real overage
but no observed window can't be attributed (surfaced, not hidden). Read-only, $0.

Declared result transport: `overage_attribution_with_evidence`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| top | Any | no | 12 | Source parameter top at src/spendguard/claudecode.py:751; see complete callable contract. |

Returns: `Any`

<a id="core-1ede0e708fc12da7d3a919663754a76f4f1d2071ee80e3fe7ac9ba625821f93b"></a>

### `spendguard.claudecode:classify`

Source: `src/spendguard/claudecode.py:1414`

Classify Claude Code sessions into org→team×project via the SHARED classifier + taxonomy (NOT the cwd repo
name — that's an umbrella). Caged, estimate-first. Stored per session in state.cls; reused by day_totals/sync.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| run | Any | no | False | Source parameter run at src/spendguard/claudecode.py:1414; see complete callable contract. |
| days | Any | no | None | Source parameter days at src/spendguard/claudecode.py:1414; see complete callable contract. |
| recls | Any | no | False | Source parameter recls at src/spendguard/claudecode.py:1414; see complete callable contract. |

Returns: `Any`

<a id="core-e0c6bc027509f51ee52b069465806e4e4b3d48ef243a1d36501954d0e78ca2e1"></a>

### `spendguard.claudecode:compaction_candidates`

Source: `src/spendguard/claudecode.py:886`

Conversations sustaining a large re-read context — the ones expensive to keep OPEN. A conversation qualifies
when its most-recent `min_turns` turns ALL have context (in+cache_read+cache_write) >= `min_context`. For each:
current context, mean, recurring re-read $/turn, and (only when k is MEASURED) the $/turn a compaction would save.
Config: advisor.compaction_context_tokens, advisor.compaction_min_turns. The threshold only PRE-FILTERS which
sessions to surface; whether the retained context is still worth its cost is an agentic call, not made here.
Returns (candidates sorted by recurring $/turn desc, (k, k_n), scan-stats {examined, flagged, too_few_turns,
below_threshold} — so the scan is transparent and never silently truncates).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| min_context | Any | no | None | Source parameter min_context at src/spendguard/claudecode.py:886; see complete callable contract. |
| min_turns | Any | no | None | Source parameter min_turns at src/spendguard/claudecode.py:886; see complete callable contract. |
| min_day | Any | no | None | Source parameter min_day at src/spendguard/claudecode.py:886; see complete callable contract. |

Returns: `Any`

<a id="core-2cac4c9e07619f9487de1afe65ba935829d7064f6e620c12c1f35d83c39c2b0e"></a>

### `spendguard.claudecode:context_trajectory`

Source: `src/spendguard/claudecode.py:870`

Per-conversation context stats from the ledger: {turns, current, max, mean, recurring_read_usd_per_turn,
model} where context = in_tok + cache_read + cache_write per turn. `current` = the last turn's context (what each
further turn re-reads); recurring_read_usd_per_turn = last turn's cache_read × the model's cache-read rate — the $
this open session spends EVERY turn just re-reading its retained context. None where unpriced/empty.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| conv_id | Any | yes | required | Source parameter conv_id at src/spendguard/claudecode.py:870; see complete callable contract. |

Returns: `Any`

<a id="core-1d9cae7d9cf964ad9905db9e0c3fc933e42e84897649f17cde3eae78fc08cbaf"></a>

### `spendguard.claudecode:ingest_events`

Source: `src/spendguard/claudecode.py:176`

Write each Claude Code assistant TURN into the local spend_events ledger, so the app's OWN turns become
queryable next to gate/batch/GPU spend. The ledger had 0 claude-code rows — precisely why token burn AFTER the
weekly plan cap was exhausted could not be tracked down: it overflowed to real API $ that no gate call ever saw.
Each turn is booked kind="est_chat" → est_chat_usd (plan-covered VALUE, billed=False, kept OUT of real $);
Phase 2 reconciles the slice past the weekly cap into a separate realtime/billed stream (never summed with this).

Idempotent: a STABLE dedup_key ("cc:<message.id>") makes the ledger book each assistant API response ONCE — across
resume/branch/compaction replays (same id in several files) AND across re-runs (record_event returns early on an
existing id). Incremental via a per-session watermark SEPARATE from update()'s, so the two miners never advance
each other's cursor. CHUNK-safe: each session is isolated in its own try, so one malformed transcript cannot wedge
the run. Pure parse + arithmetic — $0, no LLM, no network.

days  — only ingest turns on/after today-`days` (bounds a first measured run to recent activity).
limit — only the `limit` most-recently-modified transcripts (bounds the SCAN, not just the write).
reset — delete prior source="claude-code" rows + clear the watermark first (dev re-ingest).
dry   — count the turns + est-value that WOULD be written, write nothing (measure before a large backfill).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| days | Any | no | None | Source parameter days at src/spendguard/claudecode.py:176; see complete callable contract. |
| limit | Any | no | None | Source parameter limit at src/spendguard/claudecode.py:176; see complete callable contract. |
| reset | Any | no | False | Source parameter reset at src/spendguard/claudecode.py:176; see complete callable contract. |
| dry | Any | no | False | Source parameter dry at src/spendguard/claudecode.py:176; see complete callable contract. |

Returns: `Any`

<a id="core-b139ba8238cdfbb4ce69c12b5708ed577a5c958f15ae97b0cc15cb9cdcaf3cef"></a>

### `spendguard.claudecode:ingest_invoices`

Source: `src/spendguard/claudecode.py:662`

Ingest the REAL Anthropic charges (a by-card CSV export with columns Card, Reimburse, Source, Invoice ID,
Date, Type, Amount) into spend_events as the GROUND-TRUTH billed stream — the actual money out the door that the
observable est-value only approximates. Three streams, kept DISTINCT (never summed blindly):
  • claude.ai base SUBSCRIPTION (the recurring $200/mo Max plan — the first $200 claude.ai charge each month) →
    subscription_usd (the flat plan fee).
  • claude.ai OVERAGE (everything else on claude.ai: the $45/$90/$250 bundles + $10-25 top-ups) → realtime_usd,
    source='anthropic-invoice': the REAL Claude Code overage, the truth the window detector only estimates.
  • Console/API credit GRANTS → realtime_usd, source='anthropic-invoice-api': these fund spendguard's OWN gated
    API calls (already tracked as USAGE), so they are the PURCHASE side — reconcile against, don't sum with, that.
Idempotent: dedup_key='inv:<invoice_id>' (delete+rebook). $0, pure parse — no LLM, no network.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| csv_path | Any | no | None | Source parameter csv_path at src/spendguard/claudecode.py:662; see complete callable contract. |
| dry | Any | no | False | Source parameter dry at src/spendguard/claudecode.py:662; see complete callable contract. |

Returns: `Any`

<a id="core-2c80db0db80ae022b2f5d3601d32afc3fa84143b54943348bd8cc84419321f58"></a>

### `spendguard.claudecode:overage_tally`

Source: `src/spendguard/claudecode.py:604`

The FACTUAL running tally of subscription-lane OVERAGE (real past-cap $), per PROVIDER — the authoritative
invoice TRUTH plus any un-invoiced observable ESTIMATE (overflow still billed=1), never double-counted (an
invoiced month's overflow is billed=0 by _supersede_overflow_for_invoiced_months). GENERAL across lanes —
anthropic (claude-code), openai (codex), zai, … — by the same provider match the reconciliation uses.
Read-only, $0. Returns {provider: {invoiced_usd, estimate_usd, total_usd, months_invoiced, months_estimate}}.

Takes no parameters.

Returns: `Any`

<a id="core-f87029799039d1f3b7cb2938512faa85a523981b0c9c6a9126956cf7d772b7f8"></a>

### `spendguard.claudecode:reconcile_billing_state`

Source: `src/spendguard/claudecode.py:462`

CALCULATE billing_state by SEGMENTING each timeline into subscription vs overage WINDOWS, from the PROVIDER's
OWN observable signals — never a guessed cap, never the admin API (spendguard's core identity). Est-value is the
correct frame ONLY inside a subscription window; once past the weekly cap and running on credits, those turns are
REAL PAID tokens (the API). Windows:
  • SUBSCRIPTION [reset → weekly-cap hit): turns are est_chat (plan-covered, billed=0).
  • OVERAGE [weekly-cap hit → next reset): turns are REAL PAID — booked realtime/billed=1 (source=
    'claude-code-overflow') at the FULL est-value of those turns = an UNRECONCILED UPPER BOUND, pending
    reconciliation DOWN to the real auto-recharge invoices.
  • BLOCKED (out_of_credits inside an overage window): rejected, $0 — surfaced as friction, never billed.
Boundaries OBSERVED: B = seven_day cap-hit; the weekly reset grid = the provider's `resetsAt` + 7d. Plan-covered
est-value = total est_chat − overage (est-value is subscription-windows-only). The two axes are NEVER summed.
Idempotent (delete+rebook). Pure parse + arithmetic, $0.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| dry | Any | no | False | Source parameter dry at src/spendguard/claudecode.py:462; see complete callable contract. |

Returns: `Any`

<a id="core-c9aa6d1a112a80dc1e2c2b12805268fd38e33878d961bb0a3a7944c40e9e54d8"></a>

### `spendguard.claudecode:show_claudecode_report`

Source: `src/spendguard/claudecode.py:1328`

Refresh Claude Code history and print subscription usage value separately from billed dollars.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| days | Any | no | None | Trailing day window; omission reports all-time usage. |

Returns: `Any`

<a id="core-22c0bebff0fd97ac4effe995d15e3617d285c680cdf7116dbe839115c8e8669b"></a>

### `spendguard.claudecode:show_claudecode_work`

Source: `src/spendguard/claudecode.py:1564`

Conversation-derived WORK DONE — per-session rows (what was asked + cost) bucketed by day/week/month/quarter.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| by | Any | no | 'week' | Source parameter by at src/spendguard/claudecode.py:1564; see complete callable contract. |
| days | Any | no | None | Source parameter days at src/spendguard/claudecode.py:1564; see complete callable contract. |

Returns: `Any`

<a id="core-1b105145c5500fa758a2650831566d24a2ca468dd845512bd06cd1efb95573d7"></a>

### `spendguard.claudecode:story`

Source: `src/spendguard/claudecode.py:1607`

Caged synth over the period's work rows → a narrative STORY + private WORK INSIGHTS (findings/decisions/
gotchas/next — distinct from cost/LLM-usage learnings). Estimate-first; the LLM call is caged under caps.meta.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| by | Any | no | 'week' | Source parameter by at src/spendguard/claudecode.py:1607; see complete callable contract. |
| days | Any | no | 7 | Source parameter days at src/spendguard/claudecode.py:1607; see complete callable contract. |
| run | Any | no | False | Source parameter run at src/spendguard/claudecode.py:1607; see complete callable contract. |

Returns: `Any`

<a id="core-1f59ee55a88ef04ad79ef1cb56c81e98e416b4863e8cb8c6504dcdabef30501b"></a>

### `spendguard.claudecode:sync`

Source: `src/spendguard/claudecode.py:1477`

Push Claude Code spend (channel=claude-code) → the server. Honors visibility + contributor; ORG-ROUTED by the
session's classified org (only rows whose org matches THIS connection — or are unclassified — push here).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| dry | Any | no | False | Source parameter dry at src/spendguard/claudecode.py:1477; see complete callable contract. |

Returns: `Any`

<a id="core-9f365f15bd99e84a5f14e19ce8ae19e93b6b107ac30db2042dc0c3c02c352101"></a>

### `spendguard.claudecode:update`

Source: `src/spendguard/claudecode.py:105`

Read NEW transcript lines into the local accumulator (spend + work per project/model/day). Pure-ish: mutates
+ returns state; no network. Returns (state, summary-of-this-pass).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| st | Any | no | None | Source parameter st at src/spendguard/claudecode.py:105; see complete callable contract. |

Returns: `Any`

<a id="core-29db913bfc41ec2562dc18efb968d68668abb2867310880dd6413e4b16a58ad0"></a>

### `spendguard.cli:check_protected_lane_leaks`

Source: `src/spendguard/cli.py:345 (cli.lane_leak_check_cli)`

Verify protected executor work did not leak to other providers Extract parsed-namespace body; executor/provider both-or-neither; omitted targets=config.shed_lanes; preserve terminal precedence LEAK/PROBE_FAILED/INCONCLUSIVE/CLEAN and optional bounded log. Audited CLI invocation: spendguard lane-leak-check --bounded-log --hours --ledger-path --min-other-calls --protect-executor --protect-provider.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| protect_executor | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract parsed-namespace body; executor/provider both-or-neither; omitted targets=config.shed_lanes; preserve terminal precedence LEAK/PROBE_FAILED/INCONCLUSIVE/CLEAN and optional bounded log. |
| protect_provider | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract parsed-namespace body; executor/provider both-or-neither; omitted targets=config.shed_lanes; preserve terminal precedence LEAK/PROBE_FAILED/INCONCLUSIVE/CLEAN and optional bounded log. |
| hours | float \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "number"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "float \| None", "type": "any"} | no | null | Extract parsed-namespace body; executor/provider both-or-neither; omitted targets=config.shed_lanes; preserve terminal precedence LEAK/PROBE_FAILED/INCONCLUSIVE/CLEAN and optional bounded log. |
| min_other_calls | int \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "integer"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "int \| None", "type": "any"} | no | null | Extract parsed-namespace body; executor/provider both-or-neither; omitted targets=config.shed_lanes; preserve terminal precedence LEAK/PROBE_FAILED/INCONCLUSIVE/CLEAN and optional bounded log. |
| ledger_path | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract parsed-namespace body; executor/provider both-or-neither; omitted targets=config.shed_lanes; preserve terminal precedence LEAK/PROBE_FAILED/INCONCLUSIVE/CLEAN and optional bounded log. |
| bounded_log | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract parsed-namespace body; executor/provider both-or-neither; omitted targets=config.shed_lanes; preserve terminal precedence LEAK/PROBE_FAILED/INCONCLUSIVE/CLEAN and optional bounded log. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-a350a430c63f60cbf0ddae9db08b70771c58070731dfae38f71415809bbed5ff"></a>

### `spendguard.close:build_close`

Source: `src/spendguard/close.py:22`

{month, providers: [{provider, usd, days}], total_usd, current_month, forecast?} from local
provider truth. For the OPEN month with ≥5 observed days, forecast = MTD + remaining calendar days ×
the observed daily median (p50) / 90th-percentile (p90) — a RUN-RATE projection, labeled as such
(it extrapolates the month's own daily distribution; it does not model planned jobs).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| month | Any | yes | required | Source parameter month at src/spendguard/close.py:22; see complete callable contract. |

Returns: `Any`

<a id="core-61b64bd3142812e6d1c7cd900e7c9cce0d613d092b3b21c46109bec455b7dedb"></a>

### `spendguard.close:month_window`

Source: `src/spendguard/close.py:14`

(start, end) ISO dates for YYYY-MM.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| month | Any | yes | required | Source parameter month at src/spendguard/close.py:14; see complete callable contract. |

Returns: `Any`

<a id="core-918699d20ffd15e30ea3a5cb7ff89a957a927370f557b853cfdeadf30e61a8dd"></a>

### `spendguard.close:to_csv`

Source: `src/spendguard/close.py:73`

Serialize a monthly provider-truth close statement as CSV text.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| stmt | Any | yes | required | Close statement containing month, total_usd and provider rows with provider, usd and days. |

Returns: `Any`

<a id="core-0bb0c552b8f2f94df1a4986a4ec42f09f0e0a57bdd66c293b6c823e541b22719"></a>

### `spendguard.codex:classify`

Source: `src/spendguard/codex.py:177`

Classify Codex sessions into org→team×project via the SHARED classifier + taxonomy (the cwd is a PRIOR the
LLM confirms/overrides per session, incl. cross-org). Caged, estimate-first. Stored in state.cls; reused by
day_totals/sync. Parity with claudecode.classify (same convergence: re-do unclassified or 0-confidence).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| run | Any | no | False | Source parameter run at src/spendguard/codex.py:177; see complete callable contract. |
| days | Any | no | None | Source parameter days at src/spendguard/codex.py:177; see complete callable contract. |
| recls | Any | no | False | Source parameter recls at src/spendguard/codex.py:177; see complete callable contract. |

Returns: `Any`

<a id="core-1ef4b1b007645d4d16061893217cf94c01bda586a3d5d61ff121d971aa775d6b"></a>

### `spendguard.codex:show_codex_report`

Source: `src/spendguard/codex.py:260`

Local Codex USAGE-VALUE report by project, and stamp the est-value windows so `spendguard receipt` / the
in-chat footer sum claude-code + claude-ai + codex.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| days | Any | no | None | Source parameter days at src/spendguard/codex.py:260; see complete callable contract. |

Returns: `Any`

<a id="core-700262b3620f05c8617829c1e88cef6d3f433270d02219aadc6c9554dd550a5d"></a>

### `spendguard.codex:sync`

Source: `src/spendguard/codex.py:235`

Push Codex spend (channel=codex, billed=false) → the server, ORG-ROUTED by each session's classified org.
Mirrors claudecode.sync.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| dry | Any | no | False | Source parameter dry at src/spendguard/codex.py:235; see complete callable contract. |

Returns: `Any`

<a id="core-117d123a9aee976586207b966d4b814e1a1e032faeaac2a22e5acb7de8f6caad"></a>

### `spendguard.codex:update`

Source: `src/spendguard/codex.py:131`

Re-digest only the session files whose mtime changed (the incremental watermark), and PRUNE cached digests
whose session file no longer exists. Returns (state, n_changed).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| st | Any | no | None | Source parameter st at src/spendguard/codex.py:131; see complete callable contract. |

Returns: `Any`

<a id="core-286e9aa4c23193ed1be23d025baf4e7d90aef24d7ed72b431a280f6c906d7a31"></a>

### `spendguard.codex_exec:gc_shell_snapshots`

Source: `src/spendguard/codex_exec.py:469`

Prune codex CLI's shell-snapshot residue. codex writes ONE .sh per `codex exec` into
<codex_home>/shell_snapshots, and spendguard's codex lane drives that heavily — measured 2026-09-15, 7.2GB /
38,739 files. A snapshot restores an interactive shell's environment; spendguard's one-shot `codex exec` calls
never reuse theirs, and no active session touches one older than a day, so pruning by AGE is safe. DRY-RUN by
default (reports what it WOULD remove); deletes only with apply=True; NEVER touches a recent (possibly in-use)
snapshot. It deletes in codex's OWN dir, so it is opt-in: the caller (the CLI `spendguard codex-gc`, dry-run
first) or a scheduled job runs it — spendguard never prunes it silently on the hot path.

Honest reporting (never masks a failure as a clean sweep): a directory listing that fails sets `error`; a
per-file stat/unlink failure is COUNTED in `skipped`, so `deleted` vs `skipped` is truthful. Returns a summary
dict {dir, examined, stale, bytes, deleted, skipped, apply, cutoff_days, error}.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| max_age_days | Any | no | 7 | Prune codex CLI's shell-snapshot residue. codex writes ONE .sh per `codex exec` into<br><codex_home>/shell_snapshots, and spendguard's codex lane drives that heavily — measured 2026-09-15, 7.2GB /<br>38,739 files. A snapshot restores an interactive shell's environment; spendguard's one-shot `codex exec` calls<br>never reuse theirs, and no active session touches one older than a day, so pruning by AGE is safe. DRY-RUN by<br>default (reports what it WOULD remove); deletes only with apply=True; NEVER touches a recent (possibly in-use)<br>snapshot. It deletes in codex's OWN dir, so it is opt-in: the caller (the CLI `spendguard codex-gc`, dry-run<br>first) or a scheduled job runs it — spendguard never prunes it silently on the hot path.<br><br>Honest reporting (never masks a failure as a clean sweep): a directory listing that fails sets `error`; a<br>per-file stat/unlink failure is COUNTED in `skipped`, so `deleted` vs `skipped` is truthful. Returns a summary<br>dict {dir, examined, stale, bytes, deleted, skipped, apply, cutoff_days, error}. |
| apply | Any | no | False | Prune codex CLI's shell-snapshot residue. codex writes ONE .sh per `codex exec` into<br><codex_home>/shell_snapshots, and spendguard's codex lane drives that heavily — measured 2026-09-15, 7.2GB /<br>38,739 files. A snapshot restores an interactive shell's environment; spendguard's one-shot `codex exec` calls<br>never reuse theirs, and no active session touches one older than a day, so pruning by AGE is safe. DRY-RUN by<br>default (reports what it WOULD remove); deletes only with apply=True; NEVER touches a recent (possibly in-use)<br>snapshot. It deletes in codex's OWN dir, so it is opt-in: the caller (the CLI `spendguard codex-gc`, dry-run<br>first) or a scheduled job runs it — spendguard never prunes it silently on the hot path.<br><br>Honest reporting (never masks a failure as a clean sweep): a directory listing that fails sets `error`; a<br>per-file stat/unlink failure is COUNTED in `skipped`, so `deleted` vs `skipped` is truthful. Returns a summary<br>dict {dir, examined, stale, bytes, deleted, skipped, apply, cutoff_days, error}. |

Returns: `Any`

<a id="core-9f90499f2fa86d109c568d05e7bbe904a25005b4d5562095ae0cbe99f1a2cb08"></a>

### `spendguard.compaction:agentic_advise`

Source: `src/spendguard/compaction.py:165`

AGENTIC compaction advisor: read what the conversation is DOING and decide (a) whether NOW is a good moment to
compact (a natural seam vs mid-implementation) and (b) a compact_command TAILORED to what THIS conversation must
preserve. The judgement is the LLM's (not a threshold). Runs UNDER spendguard's own gate, caged (intent
spendguard:compact-advise), estimate-first. Returns the verdict dict on run; prints the estimate + returns None
otherwise. Never raises out to a hook.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| transcript_path | Any | yes | required | Source parameter transcript_path at src/spendguard/compaction.py:165; see complete callable contract. |
| run | Any | no | False | Source parameter run at src/spendguard/compaction.py:165; see complete callable contract. |
| model | Any | no | None | Source parameter model at src/spendguard/compaction.py:165; see complete callable contract. |

Returns: `Any`

<a id="core-7d538e5d056753beb062583208e79f285f934d311981f976c815677b75db1c19"></a>

### `spendguard.compaction:compact_snippet`

Source: `src/spendguard/compaction.py:91`

The ready-to-paste EFFECTIVE '/compact <instructions>' command (preserve goal/decisions/paths, collapse
exploration) — from the config-pathed file (advisor.compact_snippet_file → the review's compact_snippet.txt),
else a bundled fallback. This is what makes a MANUAL /compact lossless when typed explicitly; the PreCompact hook
injects the SAME intent automatically on every compaction, so even a bare /compact is guided once the hook is on.

Takes no parameters.

Returns: `Any`

<a id="core-599ff4f027637b2ed47e9adb0625f40c13c44e39ca007aa586f8bf2406705a21"></a>

### `spendguard.comprehend:comprehend_corpus`

Source: `src/spendguard/comprehend.py:152`

Fan the corpus across the $0 lanes and aggregate per file. Default (run=False) is a ZERO-SPEND estimate.

`intent` is the job-type label every call records under (required — attribution is the core mission).
`lanes` optionally confines the fan to an explicit lane subset (e.g. ["codex", "gemini"] to keep it off a
protected lane); None lets bulk_delegate use every eligible idle lane, which already excludes a lane that is
out of quota. `checkpoint` is the resumable results jsonl — when None on a run, a STABLE per-intent path is
used, so the fan is durable/resumable by default. `run=True` FAILS CLOSED if the interpreter is not gated,
and refuses if the estimate exceeds `budget_usd`.

`items` is an explicit list of literal file identities (no glob expansion), alternative to `patterns`.
Every item is read whole or contained in contiguous chunks; missing identities are reported as unreadable.
`vendors` confines the existing lane set by its registered provider identity; an empty intersection refuses.
`panel` reviews every item/chunk with every selected vendor's configured lane model, using the existing
provider-pinned bulk matrix. Results retain separate `reviews` per vendor, and the estimate prices all votes.
`intent` remains required at runtime when patterns are omitted for the items input surface.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| patterns | Any | no | None | Fan the corpus across the $0 lanes and aggregate per file. Default (run=False) is a ZERO-SPEND estimate.<br><br>`intent` is the job-type label every call records under (required — attribution is the core mission).<br>`lanes` optionally confines the fan to an explicit lane subset (e.g. ["codex", "gemini"] to keep it off a<br>protected lane); None lets bulk_delegate use every eligible idle lane, which already excludes a lane that is<br>out of quota. `checkpoint` is the resumable results jsonl — when None on a run, a STABLE per-intent path is<br>used, so the fan is durable/resumable by default. `run=True` FAILS CLOSED if the interpreter is not gated,<br>and refuses if the estimate exceeds `budget_usd`.<br><br>`items` is an explicit list of literal file identities (no glob expansion), alternative to `patterns`.<br>Every item is read whole or contained in contiguous chunks; missing identities are reported as unreadable.<br>`vendors` confines the existing lane set by its registered provider identity; an empty intersection refuses.<br>`panel` reviews every item/chunk with every selected vendor's configured lane model, using the existing<br>provider-pinned bulk matrix. Results retain separate `reviews` per vendor, and the estimate prices all votes.<br>`intent` remains required at runtime when patterns are omitted for the items input surface. |
| intent | Any | no | None | Fan the corpus across the $0 lanes and aggregate per file. Default (run=False) is a ZERO-SPEND estimate.<br><br>`intent` is the job-type label every call records under (required — attribution is the core mission).<br>`lanes` optionally confines the fan to an explicit lane subset (e.g. ["codex", "gemini"] to keep it off a<br>protected lane); None lets bulk_delegate use every eligible idle lane, which already excludes a lane that is<br>out of quota. `checkpoint` is the resumable results jsonl — when None on a run, a STABLE per-intent path is<br>used, so the fan is durable/resumable by default. `run=True` FAILS CLOSED if the interpreter is not gated,<br>and refuses if the estimate exceeds `budget_usd`.<br><br>`items` is an explicit list of literal file identities (no glob expansion), alternative to `patterns`.<br>Every item is read whole or contained in contiguous chunks; missing identities are reported as unreadable.<br>`vendors` confines the existing lane set by its registered provider identity; an empty intersection refuses.<br>`panel` reviews every item/chunk with every selected vendor's configured lane model, using the existing<br>provider-pinned bulk matrix. Results retain separate `reviews` per vendor, and the estimate prices all votes.<br>`intent` remains required at runtime when patterns are omitted for the items input surface. |
| question | Any | no | None | Fan the corpus across the $0 lanes and aggregate per file. Default (run=False) is a ZERO-SPEND estimate.<br><br>`intent` is the job-type label every call records under (required — attribution is the core mission).<br>`lanes` optionally confines the fan to an explicit lane subset (e.g. ["codex", "gemini"] to keep it off a<br>protected lane); None lets bulk_delegate use every eligible idle lane, which already excludes a lane that is<br>out of quota. `checkpoint` is the resumable results jsonl — when None on a run, a STABLE per-intent path is<br>used, so the fan is durable/resumable by default. `run=True` FAILS CLOSED if the interpreter is not gated,<br>and refuses if the estimate exceeds `budget_usd`.<br><br>`items` is an explicit list of literal file identities (no glob expansion), alternative to `patterns`.<br>Every item is read whole or contained in contiguous chunks; missing identities are reported as unreadable.<br>`vendors` confines the existing lane set by its registered provider identity; an empty intersection refuses.<br>`panel` reviews every item/chunk with every selected vendor's configured lane model, using the existing<br>provider-pinned bulk matrix. Results retain separate `reviews` per vendor, and the estimate prices all votes.<br>`intent` remains required at runtime when patterns are omitted for the items input surface. |
| model | Any | no | None | Fan the corpus across the $0 lanes and aggregate per file. Default (run=False) is a ZERO-SPEND estimate.<br><br>`intent` is the job-type label every call records under (required — attribution is the core mission).<br>`lanes` optionally confines the fan to an explicit lane subset (e.g. ["codex", "gemini"] to keep it off a<br>protected lane); None lets bulk_delegate use every eligible idle lane, which already excludes a lane that is<br>out of quota. `checkpoint` is the resumable results jsonl — when None on a run, a STABLE per-intent path is<br>used, so the fan is durable/resumable by default. `run=True` FAILS CLOSED if the interpreter is not gated,<br>and refuses if the estimate exceeds `budget_usd`.<br><br>`items` is an explicit list of literal file identities (no glob expansion), alternative to `patterns`.<br>Every item is read whole or contained in contiguous chunks; missing identities are reported as unreadable.<br>`vendors` confines the existing lane set by its registered provider identity; an empty intersection refuses.<br>`panel` reviews every item/chunk with every selected vendor's configured lane model, using the existing<br>provider-pinned bulk matrix. Results retain separate `reviews` per vendor, and the estimate prices all votes.<br>`intent` remains required at runtime when patterns are omitted for the items input surface. |
| run | Any | no | False | Fan the corpus across the $0 lanes and aggregate per file. Default (run=False) is a ZERO-SPEND estimate.<br><br>`intent` is the job-type label every call records under (required — attribution is the core mission).<br>`lanes` optionally confines the fan to an explicit lane subset (e.g. ["codex", "gemini"] to keep it off a<br>protected lane); None lets bulk_delegate use every eligible idle lane, which already excludes a lane that is<br>out of quota. `checkpoint` is the resumable results jsonl — when None on a run, a STABLE per-intent path is<br>used, so the fan is durable/resumable by default. `run=True` FAILS CLOSED if the interpreter is not gated,<br>and refuses if the estimate exceeds `budget_usd`.<br><br>`items` is an explicit list of literal file identities (no glob expansion), alternative to `patterns`.<br>Every item is read whole or contained in contiguous chunks; missing identities are reported as unreadable.<br>`vendors` confines the existing lane set by its registered provider identity; an empty intersection refuses.<br>`panel` reviews every item/chunk with every selected vendor's configured lane model, using the existing<br>provider-pinned bulk matrix. Results retain separate `reviews` per vendor, and the estimate prices all votes.<br>`intent` remains required at runtime when patterns are omitted for the items input surface. |
| budget_usd | Any | no | None | Fan the corpus across the $0 lanes and aggregate per file. Default (run=False) is a ZERO-SPEND estimate.<br><br>`intent` is the job-type label every call records under (required — attribution is the core mission).<br>`lanes` optionally confines the fan to an explicit lane subset (e.g. ["codex", "gemini"] to keep it off a<br>protected lane); None lets bulk_delegate use every eligible idle lane, which already excludes a lane that is<br>out of quota. `checkpoint` is the resumable results jsonl — when None on a run, a STABLE per-intent path is<br>used, so the fan is durable/resumable by default. `run=True` FAILS CLOSED if the interpreter is not gated,<br>and refuses if the estimate exceeds `budget_usd`.<br><br>`items` is an explicit list of literal file identities (no glob expansion), alternative to `patterns`.<br>Every item is read whole or contained in contiguous chunks; missing identities are reported as unreadable.<br>`vendors` confines the existing lane set by its registered provider identity; an empty intersection refuses.<br>`panel` reviews every item/chunk with every selected vendor's configured lane model, using the existing<br>provider-pinned bulk matrix. Results retain separate `reviews` per vendor, and the estimate prices all votes.<br>`intent` remains required at runtime when patterns are omitted for the items input surface. |
| lanes | Any | no | None | Fan the corpus across the $0 lanes and aggregate per file. Default (run=False) is a ZERO-SPEND estimate.<br><br>`intent` is the job-type label every call records under (required — attribution is the core mission).<br>`lanes` optionally confines the fan to an explicit lane subset (e.g. ["codex", "gemini"] to keep it off a<br>protected lane); None lets bulk_delegate use every eligible idle lane, which already excludes a lane that is<br>out of quota. `checkpoint` is the resumable results jsonl — when None on a run, a STABLE per-intent path is<br>used, so the fan is durable/resumable by default. `run=True` FAILS CLOSED if the interpreter is not gated,<br>and refuses if the estimate exceeds `budget_usd`.<br><br>`items` is an explicit list of literal file identities (no glob expansion), alternative to `patterns`.<br>Every item is read whole or contained in contiguous chunks; missing identities are reported as unreadable.<br>`vendors` confines the existing lane set by its registered provider identity; an empty intersection refuses.<br>`panel` reviews every item/chunk with every selected vendor's configured lane model, using the existing<br>provider-pinned bulk matrix. Results retain separate `reviews` per vendor, and the estimate prices all votes.<br>`intent` remains required at runtime when patterns are omitted for the items input surface. |
| max_chars | Any | no | _DEFAULT_MAX_CHARS | Fan the corpus across the $0 lanes and aggregate per file. Default (run=False) is a ZERO-SPEND estimate.<br><br>`intent` is the job-type label every call records under (required — attribution is the core mission).<br>`lanes` optionally confines the fan to an explicit lane subset (e.g. ["codex", "gemini"] to keep it off a<br>protected lane); None lets bulk_delegate use every eligible idle lane, which already excludes a lane that is<br>out of quota. `checkpoint` is the resumable results jsonl — when None on a run, a STABLE per-intent path is<br>used, so the fan is durable/resumable by default. `run=True` FAILS CLOSED if the interpreter is not gated,<br>and refuses if the estimate exceeds `budget_usd`.<br><br>`items` is an explicit list of literal file identities (no glob expansion), alternative to `patterns`.<br>Every item is read whole or contained in contiguous chunks; missing identities are reported as unreadable.<br>`vendors` confines the existing lane set by its registered provider identity; an empty intersection refuses.<br>`panel` reviews every item/chunk with every selected vendor's configured lane model, using the existing<br>provider-pinned bulk matrix. Results retain separate `reviews` per vendor, and the estimate prices all votes.<br>`intent` remains required at runtime when patterns are omitted for the items input surface. |
| checkpoint | Any | no | None | Fan the corpus across the $0 lanes and aggregate per file. Default (run=False) is a ZERO-SPEND estimate.<br><br>`intent` is the job-type label every call records under (required — attribution is the core mission).<br>`lanes` optionally confines the fan to an explicit lane subset (e.g. ["codex", "gemini"] to keep it off a<br>protected lane); None lets bulk_delegate use every eligible idle lane, which already excludes a lane that is<br>out of quota. `checkpoint` is the resumable results jsonl — when None on a run, a STABLE per-intent path is<br>used, so the fan is durable/resumable by default. `run=True` FAILS CLOSED if the interpreter is not gated,<br>and refuses if the estimate exceeds `budget_usd`.<br><br>`items` is an explicit list of literal file identities (no glob expansion), alternative to `patterns`.<br>Every item is read whole or contained in contiguous chunks; missing identities are reported as unreadable.<br>`vendors` confines the existing lane set by its registered provider identity; an empty intersection refuses.<br>`panel` reviews every item/chunk with every selected vendor's configured lane model, using the existing<br>provider-pinned bulk matrix. Results retain separate `reviews` per vendor, and the estimate prices all votes.<br>`intent` remains required at runtime when patterns are omitted for the items input surface. |
| refuse_billed | Any | no | False | Fan the corpus across the $0 lanes and aggregate per file. Default (run=False) is a ZERO-SPEND estimate.<br><br>`intent` is the job-type label every call records under (required — attribution is the core mission).<br>`lanes` optionally confines the fan to an explicit lane subset (e.g. ["codex", "gemini"] to keep it off a<br>protected lane); None lets bulk_delegate use every eligible idle lane, which already excludes a lane that is<br>out of quota. `checkpoint` is the resumable results jsonl — when None on a run, a STABLE per-intent path is<br>used, so the fan is durable/resumable by default. `run=True` FAILS CLOSED if the interpreter is not gated,<br>and refuses if the estimate exceeds `budget_usd`.<br><br>`items` is an explicit list of literal file identities (no glob expansion), alternative to `patterns`.<br>Every item is read whole or contained in contiguous chunks; missing identities are reported as unreadable.<br>`vendors` confines the existing lane set by its registered provider identity; an empty intersection refuses.<br>`panel` reviews every item/chunk with every selected vendor's configured lane model, using the existing<br>provider-pinned bulk matrix. Results retain separate `reviews` per vendor, and the estimate prices all votes.<br>`intent` remains required at runtime when patterns are omitted for the items input surface. |
| items | array {"alternatives": [], "enum": [], "items": {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, "properties": [], "python_annotation": null, "type": "array"} | no | None | Literal file/id list, alternative to patterns; whole evidence, no glob expansion. |
| vendors | array {"alternatives": [], "enum": [], "items": {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, "properties": [], "python_annotation": null, "type": "array"} | no | None | Constrain lanes to these registered provider identities. |
| panel | boolean | no | False | Fan every item/chunk across every selected vendor's configured lane model. |

Returns: `Any`

<a id="core-ee933086ba7445ad6825aa20577ae73b6a6d9e69fc1554ab7778ff5519ae8f08"></a>

### `spendguard.consumer_audit:audit_consumer_workspaces`

Source: `src/spendguard/consumer_audit.py:266`

Audit, narrowly arm safe venvs, re-probe, and return a deterministic proof record.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| deployed_sha | Any | yes | required | Audit, narrowly arm safe venvs, re-probe, and return a deterministic proof record. |
| roots | Any | no | None | Audit, narrowly arm safe venvs, re-probe, and return a deterministic proof record. |
| auto_arm | Any | no | True | Audit, narrowly arm safe venvs, re-probe, and return a deterministic proof record. |
| dry_run | Any | no | False | Audit, narrowly arm safe venvs, re-probe, and return a deterministic proof record. |
| settings | Any | no | None | Audit, narrowly arm safe venvs, re-probe, and return a deterministic proof record. CLI DELIBERATE parameter restriction: Custom settings contain live Path values; use the Python API for this optional object. MCP DELIBERATE parameter restriction: Custom settings contain live Path values; use the Python API for this optional object. |

Returns: `Any`

<a id="core-b6899eaf3192e427fae63d2b256c722cfbaa391c5bc743d47f714837417fec5b"></a>

### `spendguard.content_compression_backfill:compress_retained_content_database`

Source: `src/spendguard/content_compression_backfill.py:39`

Compress one bounded batch per table; rerun until every returned count is zero.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| database_path | Any | no | None | Compress one bounded batch per table; rerun until every returned count is zero. |
| batch_rows | Any | no | CONTENT_BACKFILL_BATCH_ROWS | Compress one bounded batch per table; rerun until every returned count is zero. |

Returns: `Any`

<a id="core-29c8df38b24cd50784657618f693bc4807cfd55bcb3a93465a88840d936e4a37"></a>

### `spendguard.conv:attribute_segments`

Source: `src/spendguard/conv.py:464`

AGENTIC: classify each subconversation into org→team×project via the shared LLM classifier (cwd as a PRIOR it
confirms/overrides), and RECORD each decision in the base sqlite so we never re-pay. Estimate-first (run=False →
estimate only). Scopes to SPEND-BEARING segments by default (the core-mission $ attribution), and re-classifies
only segments that are absent / not-llm-or-human / below confidence τ — the convergence loop's small step.
Returns the recorded store (run=True) or an estimate summary (run=False).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| tdir | Any | no | None | AGENTIC: classify each subconversation into org→team×project via the shared LLM classifier (cwd as a PRIOR it<br>confirms/overrides), and RECORD each decision in the base sqlite so we never re-pay. Estimate-first (run=False →<br>estimate only). Scopes to SPEND-BEARING segments by default (the core-mission $ attribution), and re-classifies<br>only segments that are absent / not-llm-or-human / below confidence τ — the convergence loop's small step.<br>Returns the recorded store (run=True) or an estimate summary (run=False). |
| run | Any | no | False | AGENTIC: classify each subconversation into org→team×project via the shared LLM classifier (cwd as a PRIOR it<br>confirms/overrides), and RECORD each decision in the base sqlite so we never re-pay. Estimate-first (run=False →<br>estimate only). Scopes to SPEND-BEARING segments by default (the core-mission $ attribution), and re-classifies<br>only segments that are absent / not-llm-or-human / below confidence τ — the convergence loop's small step.<br>Returns the recorded store (run=True) or an estimate summary (run=False). |
| recls | Any | no | False | AGENTIC: classify each subconversation into org→team×project via the shared LLM classifier (cwd as a PRIOR it<br>confirms/overrides), and RECORD each decision in the base sqlite so we never re-pay. Estimate-first (run=False →<br>estimate only). Scopes to SPEND-BEARING segments by default (the core-mission $ attribution), and re-classifies<br>only segments that are absent / not-llm-or-human / below confidence τ — the convergence loop's small step.<br>Returns the recorded store (run=True) or an estimate summary (run=False). |
| spend_only | Any | no | True | AGENTIC: classify each subconversation into org→team×project via the shared LLM classifier (cwd as a PRIOR it<br>confirms/overrides), and RECORD each decision in the base sqlite so we never re-pay. Estimate-first (run=False →<br>estimate only). Scopes to SPEND-BEARING segments by default (the core-mission $ attribution), and re-classifies<br>only segments that are absent / not-llm-or-human / below confidence τ — the convergence loop's small step.<br>Returns the recorded store (run=True) or an estimate summary (run=False). |
| tau | Any | no | _SEG_TAU | AGENTIC: classify each subconversation into org→team×project via the shared LLM classifier (cwd as a PRIOR it<br>confirms/overrides), and RECORD each decision in the base sqlite so we never re-pay. Estimate-first (run=False →<br>estimate only). Scopes to SPEND-BEARING segments by default (the core-mission $ attribution), and re-classifies<br>only segments that are absent / not-llm-or-human / below confidence τ — the convergence loop's small step.<br>Returns the recorded store (run=True) or an estimate summary (run=False). |

Returns: `Any`

<a id="core-dba9ce92a60a76a4aaae6aeecda30510bf724ee45c9afcd8a99a32e32881e6a5"></a>

### `spendguard.conv:attribute_usage`

Source: `src/spendguard/conv.py:153`

Proper accounting: match actual provider USAGE (per-batch cost) to PROJECTS via the AGENTIC per-subconversation
attribution — batch id → the segment that ran it → its LLM-classified project (cwd as a prior the LLM confirms).
NEVER a regex keyword guess. Reads the cached classification (run `spendguard accounting --run` to refresh it).
Returns {total, batches, linked, by_project:{proj:$}}. Free (reads the cache).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | '2026-06-01' | Proper accounting: match actual provider USAGE (per-batch cost) to PROJECTS via the AGENTIC per-subconversation<br>attribution — batch id → the segment that ran it → its LLM-classified project (cwd as a prior the LLM confirms).<br>NEVER a regex keyword guess. Reads the cached classification (run `spendguard accounting --run` to refresh it).<br>Returns {total, batches, linked, by_project:{proj:$}}. Free (reads the cache). |
| tdir | Any | no | None | Proper accounting: match actual provider USAGE (per-batch cost) to PROJECTS via the AGENTIC per-subconversation<br>attribution — batch id → the segment that ran it → its LLM-classified project (cwd as a prior the LLM confirms).<br>NEVER a regex keyword guess. Reads the cached classification (run `spendguard accounting --run` to refresh it).<br>Returns {total, batches, linked, by_project:{proj:$}}. Free (reads the cache). |

Returns: `Any`

<a id="core-375b25629b9594d6d293a155faa4f6ae0cbc653ab9bdb726fbc15824ab87cda8"></a>

### `spendguard.conv:index_cmd`

Source: `src/spendguard/conv.py:1107`

Index conversation events and optionally replace their persisted graph projection.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| tdir | Any | no | None | Transcript directory passed to the conversation index builder. |
| apply | Any | no | False | Persist conversation_event nodes and comments_on edges; false reports counts only. |
| rebuild | Any | no | False | Rebuild the transcript index instead of reusing cached entries. |

Returns: `Any`

<a id="core-596687b43ad44be8bcb8426454ea5dc8c1aabbb5dd9710370fb70f20b4d4e9ea"></a>

### `spendguard.conv:synth`

Source: `src/spendguard/conv.py:1166`

Estimate, or explicitly run, conversation evidence synthesis and persist its insights.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| tdir | Any | no | None | Transcript directory used to build the conversation event index. |
| run | Any | no | False | Execute the governed synthesis call; false reports its estimate only. |
| limit | Any | no | 40 | Maximum deduplicated conversation events supplied to synthesis. |

Returns: `Any`

<a id="core-a570c02e8bcb02050257f2641b80c6fd63fc53838f59964bf69f3906c95f8878"></a>

### `spendguard.coverage:audit_gating_coverage`

Source: `src/spendguard/coverage.py:117`

[{venv, llm:[providers], gated:bool, own:bool}] for every venv (under roots) that has an LLM SDK installed.
`own` = it's spendguard's own venv, which self-gates via `import spendguard; spendguard.require()` in its scripts
(gating it via sitecustomize would spam the receipt on every dev `python` — so that's intentional, not a gap).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| roots | Any | no | None | [{venv, llm:[providers], gated:bool, own:bool}] for every venv (under roots) that has an LLM SDK installed.<br>`own` = it's spendguard's own venv, which self-gates via `import spendguard; spendguard.require()` in its scripts<br>(gating it via sitecustomize would spam the receipt on every dev `python` — so that's intentional, not a gap). |

Returns: `Any`

<a id="core-2b2b2b602d41bb6497173212fa9c761d8af7bdca9bda4e2ad59ba556c6f0787e"></a>

### `spendguard.crossllm:ask`

Source: `src/spendguard/crossllm.py:272`

Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.

vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default
                 panel (config `ask.default_vendors`) — then just pick a count with `n`.
n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick
                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.
schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer
mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered
                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met
budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend
deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.
max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used
preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before
                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated
                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.

Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error
(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written
(ModelPreflightRefused) — both deliberate, both BEFORE any spend.

Declared result transport: `ask_result_as_dict`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| prompt | Any | yes | required | Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.<br><br>vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default<br>                 panel (config `ask.default_vendors`) — then just pick a count with `n`.<br>n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick<br>                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.<br>schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer<br>mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered<br>                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met<br>budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend<br>deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.<br>max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used<br>preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before<br>                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated<br>                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.<br><br>Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error<br>(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written<br>(ModelPreflightRefused) — both deliberate, both BEFORE any spend. |
| vendors | Any | no | None | Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.<br><br>vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default<br>                 panel (config `ask.default_vendors`) — then just pick a count with `n`.<br>n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick<br>                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.<br>schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer<br>mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered<br>                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met<br>budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend<br>deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.<br>max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used<br>preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before<br>                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated<br>                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.<br><br>Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error<br>(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written<br>(ModelPreflightRefused) — both deliberate, both BEFORE any spend. |
| n | Any | no | None | Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.<br><br>vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default<br>                 panel (config `ask.default_vendors`) — then just pick a count with `n`.<br>n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick<br>                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.<br>schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer<br>mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered<br>                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met<br>budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend<br>deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.<br>max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used<br>preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before<br>                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated<br>                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.<br><br>Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error<br>(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written<br>(ModelPreflightRefused) — both deliberate, both BEFORE any spend. |
| schema | Any | no | None | Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.<br><br>vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default<br>                 panel (config `ask.default_vendors`) — then just pick a count with `n`.<br>n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick<br>                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.<br>schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer<br>mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered<br>                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met<br>budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend<br>deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.<br>max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used<br>preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before<br>                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated<br>                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.<br><br>Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error<br>(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written<br>(ModelPreflightRefused) — both deliberate, both BEFORE any spend. |
| system | Any | no | None | Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.<br><br>vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default<br>                 panel (config `ask.default_vendors`) — then just pick a count with `n`.<br>n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick<br>                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.<br>schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer<br>mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered<br>                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met<br>budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend<br>deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.<br>max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used<br>preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before<br>                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated<br>                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.<br><br>Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error<br>(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written<br>(ModelPreflightRefused) — both deliberate, both BEFORE any spend. |
| purpose | Any | no | 'ask' | Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.<br><br>vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default<br>                 panel (config `ask.default_vendors`) — then just pick a count with `n`.<br>n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick<br>                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.<br>schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer<br>mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered<br>                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met<br>budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend<br>deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.<br>max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used<br>preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before<br>                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated<br>                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.<br><br>Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error<br>(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written<br>(ModelPreflightRefused) — both deliberate, both BEFORE any spend. |
| deadline_s | Any | no | None | Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.<br><br>vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default<br>                 panel (config `ask.default_vendors`) — then just pick a count with `n`.<br>n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick<br>                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.<br>schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer<br>mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered<br>                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met<br>budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend<br>deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.<br>max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used<br>preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before<br>                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated<br>                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.<br><br>Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error<br>(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written<br>(ModelPreflightRefused) — both deliberate, both BEFORE any spend. |
| budget_usd | Any | no | None | Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.<br><br>vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default<br>                 panel (config `ask.default_vendors`) — then just pick a count with `n`.<br>n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick<br>                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.<br>schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer<br>mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered<br>                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met<br>budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend<br>deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.<br>max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used<br>preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before<br>                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated<br>                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.<br><br>Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error<br>(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written<br>(ModelPreflightRefused) — both deliberate, both BEFORE any spend. |
| mode | Any | no | 'all' | Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.<br><br>vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default<br>                 panel (config `ask.default_vendors`) — then just pick a count with `n`.<br>n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick<br>                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.<br>schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer<br>mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered<br>                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met<br>budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend<br>deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.<br>max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used<br>preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before<br>                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated<br>                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.<br><br>Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error<br>(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written<br>(ModelPreflightRefused) — both deliberate, both BEFORE any spend. |
| require | Any | no | None | Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.<br><br>vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default<br>                 panel (config `ask.default_vendors`) — then just pick a count with `n`.<br>n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick<br>                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.<br>schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer<br>mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered<br>                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met<br>budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend<br>deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.<br>max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used<br>preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before<br>                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated<br>                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.<br><br>Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error<br>(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written<br>(ModelPreflightRefused) — both deliberate, both BEFORE any spend. |
| max_tokens | Any | no | None | Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.<br><br>vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default<br>                 panel (config `ask.default_vendors`) — then just pick a count with `n`.<br>n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick<br>                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.<br>schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer<br>mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered<br>                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met<br>budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend<br>deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.<br>max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used<br>preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before<br>                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated<br>                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.<br><br>Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error<br>(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written<br>(ModelPreflightRefused) — both deliberate, both BEFORE any spend. |
| est_output_tokens | Any | no | None | Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.<br><br>vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default<br>                 panel (config `ask.default_vendors`) — then just pick a count with `n`.<br>n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick<br>                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.<br>schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer<br>mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered<br>                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met<br>budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend<br>deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.<br>max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used<br>preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before<br>                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated<br>                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.<br><br>Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error<br>(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written<br>(ModelPreflightRefused) — both deliberate, both BEFORE any spend. |
| preflight | Any | no | True | Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.<br><br>vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default<br>                 panel (config `ask.default_vendors`) — then just pick a count with `n`.<br>n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick<br>                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.<br>schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer<br>mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered<br>                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met<br>budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend<br>deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.<br>max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used<br>preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before<br>                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated<br>                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.<br><br>Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error<br>(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written<br>(ModelPreflightRefused) — both deliberate, both BEFORE any spend. |
| refuse_billed | Any | no | False | Ask N models the same prompt; return an honest AskResult. The stable public entry point for cross-LLM work.<br><br>vendors        : ["vendor:model", ...] or [("vendor","model"), ...]. Omit to use the configured default<br>                 panel (config `ask.default_vendors`) — then just pick a count with `n`.<br>n              : HOW MANY of the vendors to actually use — the caller decides the panel size (2 for a quick<br>                 check, the full list for high stakes). The first `n` as ordered, so put must-haves first.<br>schema         : optional JSON Schema — an OK result that violates it becomes SCHEMA_VIOLATION, not an answer<br>mode           : "all"  → wait for every vendor (fan_out); complete == everyone answered<br>                 "first"→ return as soon as `require` (default 1) answer (first_ok); complete == need met<br>budget_usd     : if set, REFUSE (BudgetRefused) when the pre-flight metered estimate exceeds it — before spend<br>deadline_s     : per-vendor default; time_budget refines it per model. Defaults to a measured-safe value.<br>max_tokens     : omit it — the measured output cap (32K floor, clamped to the model max) is used<br>preflight      : HARD GATE (default on) — every named model must be callable AS WRITTEN (served + priced) before<br>                 any spend; a stale/unpriced/unknown id is refused with its fix (ModelPreflightRefused). Validated<br>                 once per vendor-set per process. Pass preflight=False only when the caller already preflighted.<br><br>Never raises for a vendor failure (those are honest kinds in the result); raises only for a caller error<br>(bad vendor spec / no vendors), a refused budget (BudgetRefused), or a model not callable as written<br>(ModelPreflightRefused) — both deliberate, both BEFORE any spend. |

Returns: `Any`

<a id="core-531c16b0caeca81416ad6014208e8847bad71c05ccaf71428cb7a17e57f4dc8a"></a>

### `spendguard.delegate_jobs:read_delegate_job`

Source: `src/spendguard/delegate_jobs.py:186`

Read job status; stale running jobs are reported failed without mutating history.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| job_id | Any | yes | required | Source parameter job_id at src/spendguard/delegate_jobs.py:186; see complete callable contract. |

Returns: `Any`

<a id="core-2cf61e6857bb3198179cbe8eee0415f30de52f1a88fff5df3f7ca826f8cbf66d"></a>

### `spendguard.delegate_jobs:read_delegate_job_result`

Source: `src/spendguard/delegate_jobs.py:210`

Return the complete authoritative terminal payload for one job.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| job_id | Any | yes | required | Source parameter job_id at src/spendguard/delegate_jobs.py:210; see complete callable contract. |

Returns: `Any`

<a id="core-68ddabbd5490ea060407af6d2e38f0366a724e8bcf5ad0394a71f0852c521d8d"></a>

### `spendguard.delegate_router:delegate_task`

Source: `src/spendguard/delegate_router.py:586`

Classify, estimate, and optionally execute one self-contained AGENTIC workspace-write subtask.

Default is zero-execution dry-run; execution requires a trusted absolute ``cwd`` and has a 1,800-second default
agentic budget. For a one-shot classification, gate, or doctrine verdict, instead use
``adapters.call(..., reasoning="best-value", intent=...)`` or ``spendguard ask``. Codex is ~5s warm; >75s is
cold ``codex exec`` only. See README.md's routing cheat-sheet.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| task | Any | yes | required | Source parameter task at src/spendguard/delegate_router.py:586; see complete callable contract. |
| files | Any | no | None | Source parameter files at src/spendguard/delegate_router.py:586; see complete callable contract. |
| intent | Any | no | None | Source parameter intent at src/spendguard/delegate_router.py:586; see complete callable contract. |
| provider | Any | no | 'auto' | Source parameter provider at src/spendguard/delegate_router.py:586; see complete callable contract. |
| execute | Any | no | False | Source parameter execute at src/spendguard/delegate_router.py:586; see complete callable contract. |
| timeout | Any | no | None | Source parameter timeout at src/spendguard/delegate_router.py:586; see complete callable contract. |
| cwd | Any | no | None | Source parameter cwd at src/spendguard/delegate_router.py:586; see complete callable contract. |
| warm | Any | no | False | Use the existing warm governed broker path. |

Returns: `Any`

<a id="core-27b0596e32b63a7c3e4de5028aa61d0e8ca760de16235afe09ba3988e43cc00e"></a>

### `spendguard.delegate_router:start_delegate_job`

Source: `src/spendguard/delegate_router.py:546`

Prepare and start one daemon delegate job, returning without awaiting execution.

CLI DELIBERATE variant restriction: The delegate command invokes this lifecycle starter with detached=True; a direct generated starter command is omitted to preserve governed handoff and result collection.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| task | Any | yes | required | Source parameter task at src/spendguard/delegate_router.py:546; see complete callable contract. |
| files | Any | no | None | Source parameter files at src/spendguard/delegate_router.py:546; see complete callable contract. |
| intent | Any | no | None | Source parameter intent at src/spendguard/delegate_router.py:546; see complete callable contract. |
| provider | Any | no | 'auto' | Source parameter provider at src/spendguard/delegate_router.py:546; see complete callable contract. |
| cwd | Any | no | None | Source parameter cwd at src/spendguard/delegate_router.py:546; see complete callable contract. |
| timeout | Any | no | None | Source parameter timeout at src/spendguard/delegate_router.py:546; see complete callable contract. |
| detached | Any | no | False | Launch the existing governed executor in an independent OS worker that survives caller exit. |
| warm | Any | no | False | Use the existing warm governed broker path. |

Returns: `Any`

<a id="core-23cfa6dd59c35987fb1c680583bd0660eb48994df194155ee711f3a5a92988f6"></a>

### `spendguard.delegate_router:wait_for_delegate_job_handoff`

Source: `src/spendguard/delegate_router.py:570`

Wait only through the configured inline window, then return result or a durable handle.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| job_id | Any | yes | required | Source parameter job_id at src/spendguard/delegate_router.py:570; see complete callable contract. |

Returns: `Any`

<a id="core-ca799ffcbf23d86f2e0e5171395b3d2252b3dce0e8e23ba688b8eb3064f69f32"></a>

### `spendguard.dispatch:admission_state`

Source: `src/spendguard/dispatch.py:1116`

The ONE $0 snapshot of the whole ADMISSION + QUEUE + reasoning-cut picture, so the three surfaces render the
SAME data and can never drift: the CLI (`spendguard dispatch`) and the MCP tool (spendguard_dispatch_state) both
call this. Assembles the live governor (per-key concurrency/rpm/tpm/in-flight/waiting), the SELF-CALIBRATED
per-vendor limits (learned from 429 + success headers), the durable QUEUE depth (pending/leased/done/failed +
PARKED backpressure), and the deadline-cancel counts (the invisible reasoning-cut waste). Each subsystem is pulled
behind its own try so one being unavailable degrades to a named note, never breaks the snapshot.

Takes no parameters.

Returns: `Any`

<a id="core-623c13035b1e27f089e5786a20cc25cc60c17c94812688cabe1893620b843e38"></a>

### `spendguard.distribution:distribute_fleet`

Source: `src/spendguard/distribution.py:730`

Idempotent scheduler/library entry. Any missing proof is a named straggler.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| check | Any | no | False | Idempotent scheduler/library entry. Any missing proof is a named straggler. |
| roots | Any | no | None | Idempotent scheduler/library entry. Any missing proof is a named straggler. |
| dry_run | Any | no | False | Idempotent scheduler/library entry. Any missing proof is a named straggler. |
| settings | Any | no | None | Idempotent scheduler/library entry. Any missing proof is a named straggler. |
| version | Any | no | None | Idempotent scheduler/library entry. Any missing proof is a named straggler. |

Returns: `Any`

<a id="core-27e9e0821dd621c864e00d5cd0ab25e6f6b463affc8511076ecf708a52d17b65"></a>

### `spendguard.effort_titration:titrate`

Source: `src/spendguard/effort_titration.py:342`

A/B the effort ladder for (intent, model), score each output, and (run=True) record the cheapest holding
effort as the effort:<intent> fact. `model` defaults to the intent's most-used model (its de-facto default —
the cheapest one to make honest); pass one to titrate a specific model. `efforts` overrides the ladder.
ESTIMATE-FIRST: run=False returns the plan + $ estimate and spends nothing; run=True executes (refusing if the
pilot estimate exceeds `budget_usd`). Incremental + RESUMABLE: expand a chunk at a time, agentic verdict after
each, stop when confident, up to `sample_n`, checkpointing after EVERY measured prompt.
`requirement_aware=True` scores each output with the two-tier REQUIREMENT judge (requirement_judge: screen model →
opus `adjudicator_model` on the unsure calls), against the prompt's own extracted criteria, instead of the generic
quality judge. Returns a dict.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | yes | required | A/B the effort ladder for (intent, model), score each output, and (run=True) record the cheapest holding<br>effort as the effort:<intent> fact. `model` defaults to the intent's most-used model (its de-facto default —<br>the cheapest one to make honest); pass one to titrate a specific model. `efforts` overrides the ladder.<br>ESTIMATE-FIRST: run=False returns the plan + $ estimate and spends nothing; run=True executes (refusing if the<br>pilot estimate exceeds `budget_usd`). Incremental + RESUMABLE: expand a chunk at a time, agentic verdict after<br>each, stop when confident, up to `sample_n`, checkpointing after EVERY measured prompt.<br>`requirement_aware=True` scores each output with the two-tier REQUIREMENT judge (requirement_judge: screen model →<br>opus `adjudicator_model` on the unsure calls), against the prompt's own extracted criteria, instead of the generic<br>quality judge. Returns a dict. |
| model | Any | no | None | A/B the effort ladder for (intent, model), score each output, and (run=True) record the cheapest holding<br>effort as the effort:<intent> fact. `model` defaults to the intent's most-used model (its de-facto default —<br>the cheapest one to make honest); pass one to titrate a specific model. `efforts` overrides the ladder.<br>ESTIMATE-FIRST: run=False returns the plan + $ estimate and spends nothing; run=True executes (refusing if the<br>pilot estimate exceeds `budget_usd`). Incremental + RESUMABLE: expand a chunk at a time, agentic verdict after<br>each, stop when confident, up to `sample_n`, checkpointing after EVERY measured prompt.<br>`requirement_aware=True` scores each output with the two-tier REQUIREMENT judge (requirement_judge: screen model →<br>opus `adjudicator_model` on the unsure calls), against the prompt's own extracted criteria, instead of the generic<br>quality judge. Returns a dict. |
| efforts | Any | no | None | A/B the effort ladder for (intent, model), score each output, and (run=True) record the cheapest holding<br>effort as the effort:<intent> fact. `model` defaults to the intent's most-used model (its de-facto default —<br>the cheapest one to make honest); pass one to titrate a specific model. `efforts` overrides the ladder.<br>ESTIMATE-FIRST: run=False returns the plan + $ estimate and spends nothing; run=True executes (refusing if the<br>pilot estimate exceeds `budget_usd`). Incremental + RESUMABLE: expand a chunk at a time, agentic verdict after<br>each, stop when confident, up to `sample_n`, checkpointing after EVERY measured prompt.<br>`requirement_aware=True` scores each output with the two-tier REQUIREMENT judge (requirement_judge: screen model →<br>opus `adjudicator_model` on the unsure calls), against the prompt's own extracted criteria, instead of the generic<br>quality judge. Returns a dict. |
| sample_n | Any | no | _MAX_SAMPLE | A/B the effort ladder for (intent, model), score each output, and (run=True) record the cheapest holding<br>effort as the effort:<intent> fact. `model` defaults to the intent's most-used model (its de-facto default —<br>the cheapest one to make honest); pass one to titrate a specific model. `efforts` overrides the ladder.<br>ESTIMATE-FIRST: run=False returns the plan + $ estimate and spends nothing; run=True executes (refusing if the<br>pilot estimate exceeds `budget_usd`). Incremental + RESUMABLE: expand a chunk at a time, agentic verdict after<br>each, stop when confident, up to `sample_n`, checkpointing after EVERY measured prompt.<br>`requirement_aware=True` scores each output with the two-tier REQUIREMENT judge (requirement_judge: screen model →<br>opus `adjudicator_model` on the unsure calls), against the prompt's own extracted criteria, instead of the generic<br>quality judge. Returns a dict. |
| run | Any | no | False | A/B the effort ladder for (intent, model), score each output, and (run=True) record the cheapest holding<br>effort as the effort:<intent> fact. `model` defaults to the intent's most-used model (its de-facto default —<br>the cheapest one to make honest); pass one to titrate a specific model. `efforts` overrides the ladder.<br>ESTIMATE-FIRST: run=False returns the plan + $ estimate and spends nothing; run=True executes (refusing if the<br>pilot estimate exceeds `budget_usd`). Incremental + RESUMABLE: expand a chunk at a time, agentic verdict after<br>each, stop when confident, up to `sample_n`, checkpointing after EVERY measured prompt.<br>`requirement_aware=True` scores each output with the two-tier REQUIREMENT judge (requirement_judge: screen model →<br>opus `adjudicator_model` on the unsure calls), against the prompt's own extracted criteria, instead of the generic<br>quality judge. Returns a dict. |
| budget_usd | Any | no | None | A/B the effort ladder for (intent, model), score each output, and (run=True) record the cheapest holding<br>effort as the effort:<intent> fact. `model` defaults to the intent's most-used model (its de-facto default —<br>the cheapest one to make honest); pass one to titrate a specific model. `efforts` overrides the ladder.<br>ESTIMATE-FIRST: run=False returns the plan + $ estimate and spends nothing; run=True executes (refusing if the<br>pilot estimate exceeds `budget_usd`). Incremental + RESUMABLE: expand a chunk at a time, agentic verdict after<br>each, stop when confident, up to `sample_n`, checkpointing after EVERY measured prompt.<br>`requirement_aware=True` scores each output with the two-tier REQUIREMENT judge (requirement_judge: screen model →<br>opus `adjudicator_model` on the unsure calls), against the prompt's own extracted criteria, instead of the generic<br>quality judge. Returns a dict. |
| judge_model | Any | no | None | A/B the effort ladder for (intent, model), score each output, and (run=True) record the cheapest holding<br>effort as the effort:<intent> fact. `model` defaults to the intent's most-used model (its de-facto default —<br>the cheapest one to make honest); pass one to titrate a specific model. `efforts` overrides the ladder.<br>ESTIMATE-FIRST: run=False returns the plan + $ estimate and spends nothing; run=True executes (refusing if the<br>pilot estimate exceeds `budget_usd`). Incremental + RESUMABLE: expand a chunk at a time, agentic verdict after<br>each, stop when confident, up to `sample_n`, checkpointing after EVERY measured prompt.<br>`requirement_aware=True` scores each output with the two-tier REQUIREMENT judge (requirement_judge: screen model →<br>opus `adjudicator_model` on the unsure calls), against the prompt's own extracted criteria, instead of the generic<br>quality judge. Returns a dict. |
| requirement_aware | Any | no | False | A/B the effort ladder for (intent, model), score each output, and (run=True) record the cheapest holding<br>effort as the effort:<intent> fact. `model` defaults to the intent's most-used model (its de-facto default —<br>the cheapest one to make honest); pass one to titrate a specific model. `efforts` overrides the ladder.<br>ESTIMATE-FIRST: run=False returns the plan + $ estimate and spends nothing; run=True executes (refusing if the<br>pilot estimate exceeds `budget_usd`). Incremental + RESUMABLE: expand a chunk at a time, agentic verdict after<br>each, stop when confident, up to `sample_n`, checkpointing after EVERY measured prompt.<br>`requirement_aware=True` scores each output with the two-tier REQUIREMENT judge (requirement_judge: screen model →<br>opus `adjudicator_model` on the unsure calls), against the prompt's own extracted criteria, instead of the generic<br>quality judge. Returns a dict. |
| adjudicator_model | Any | no | None | A/B the effort ladder for (intent, model), score each output, and (run=True) record the cheapest holding<br>effort as the effort:<intent> fact. `model` defaults to the intent's most-used model (its de-facto default —<br>the cheapest one to make honest); pass one to titrate a specific model. `efforts` overrides the ladder.<br>ESTIMATE-FIRST: run=False returns the plan + $ estimate and spends nothing; run=True executes (refusing if the<br>pilot estimate exceeds `budget_usd`). Incremental + RESUMABLE: expand a chunk at a time, agentic verdict after<br>each, stop when confident, up to `sample_n`, checkpointing after EVERY measured prompt.<br>`requirement_aware=True` scores each output with the two-tier REQUIREMENT judge (requirement_judge: screen model →<br>opus `adjudicator_model` on the unsure calls), against the prompt's own extracted criteria, instead of the generic<br>quality judge. Returns a dict. |

Returns: `Any`

<a id="core-3c196c985d979e312b156d47ff947499babeae52f187641a291c5e4cbc892b01"></a>

### `spendguard.estimate_divergence:enforce`

Source: `src/spendguard/estimate_divergence.py:91`

Judge every recorded estimate against its actual. Returns the verdicts; RAISES on an ungrounded one.

Call this wherever an estimate authorizes spending, and in CI. `raise_on_fail=False` is for reporting
surfaces that must render rather than throw — but the default is to raise, because the default failure
of this whole area has been observing without objecting.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| raise_on_fail | Any | no | True | Judge every recorded estimate against its actual. Returns the verdicts; RAISES on an ungrounded one.<br><br>Call this wherever an estimate authorizes spending, and in CI. `raise_on_fail=False` is for reporting<br>surfaces that must render rather than throw — but the default is to raise, because the default failure<br>of this whole area has been observing without objecting. |
| model | Any | no | None | Judge every recorded estimate against its actual. Returns the verdicts; RAISES on an ungrounded one.<br><br>Call this wherever an estimate authorizes spending, and in CI. `raise_on_fail=False` is for reporting<br>surfaces that must render rather than throw — but the default is to raise, because the default failure<br>of this whole area has been observing without objecting. |

Returns: `Any`

<a id="core-a14295255b80d68eec0d244a34c644c57953e253ad89ff2ae1c66a0b9b9f81fc"></a>

### `spendguard.estimate_literals:review_literal_token_estimates`

Source: `src/spendguard/estimate_literals.py:148 (estimate_literals.cmd)`

Find and adjudicate ungrounded token estimate literals Extract unruled_and_quotes scan, optional adjudicate_literal+verdict ledger save, rescan and terminal status. Audited CLI invocation: spendguard estimate-literals --judge.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| judge | bool | no | false | Extract unruled_and_quotes scan, optional adjudicate_literal+verdict ledger save, rescan and terminal status. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-4febdb316dd1d47a25609770d714af32c1449c1567a9b8d0f68836de1b3e2705"></a>

### `spendguard.execution_journal:recover_durable_execution`

Source: `src/spendguard/execution_journal.py:403`

Recover persisted execution state and payloads, repairing lost-process or terminal projections.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| execution_id | Any | yes | required | Durable execution identifier whose state and payloads are recovered. |
| root | Any | no | None | Execution journal root; omission uses the existing configured root. |

Returns: `Any`

<a id="core-d5a99048eb6eca05285e4dbc9a5499f3d6b2631509b259160d0e0c7d80bd48ae"></a>

### `spendguard.experiment:experiment`

Source: `src/spendguard/experiment.py:119`

A/B/n-test cheaper (model, instruction, effort) VARIANTS for `intent` against its stored call_io samples, and
record which hold quality — the programmatic experiment API.

intent: the job-type label whose sample corpus is tested. models / instructions: the variant axes (lists; None = a
default terse-output + cheaper-model set from _variants). n: samples per variant. mode: the equivalence tier
('auto' | 'embed' | 'rubric' | 'custom:<module.fn>'). reconsider=True also retests models previously marked
ineffective.

ESTIMATE-FIRST: run=False (the default) only estimates and prints — no workload spend (meta-capped effort/discovery
probes only). run=True makes REAL metered calls under the META cage (caps.meta), grades each output via
equivalence.grade, and KILLS a variant whose pilot fails the quality bar before the full sample (a losing arm is
never paid in full). SIDE EFFECTS when run: metered spend, console progress, and the ineffective-model denylist is
updated. Returns a summary of the per-variant results (cost / score / verdict). A per-call failure is COUNTED, never
silently swallowed; a deliberate spend stop or cap refusal propagates.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | yes | required | A/B/n-test cheaper (model, instruction, effort) VARIANTS for `intent` against its stored call_io samples, and<br>record which hold quality — the programmatic experiment API.<br><br>intent: the job-type label whose sample corpus is tested. models / instructions: the variant axes (lists; None = a<br>default terse-output + cheaper-model set from _variants). n: samples per variant. mode: the equivalence tier<br>('auto' \| 'embed' \| 'rubric' \| 'custom:<module.fn>'). reconsider=True also retests models previously marked<br>ineffective.<br><br>ESTIMATE-FIRST: run=False (the default) only estimates and prints — no workload spend (meta-capped effort/discovery<br>probes only). run=True makes REAL metered calls under the META cage (caps.meta), grades each output via<br>equivalence.grade, and KILLS a variant whose pilot fails the quality bar before the full sample (a losing arm is<br>never paid in full). SIDE EFFECTS when run: metered spend, console progress, and the ineffective-model denylist is<br>updated. Returns a summary of the per-variant results (cost / score / verdict). A per-call failure is COUNTED, never<br>silently swallowed; a deliberate spend stop or cap refusal propagates. |
| models | Any | no | None | A/B/n-test cheaper (model, instruction, effort) VARIANTS for `intent` against its stored call_io samples, and<br>record which hold quality — the programmatic experiment API.<br><br>intent: the job-type label whose sample corpus is tested. models / instructions: the variant axes (lists; None = a<br>default terse-output + cheaper-model set from _variants). n: samples per variant. mode: the equivalence tier<br>('auto' \| 'embed' \| 'rubric' \| 'custom:<module.fn>'). reconsider=True also retests models previously marked<br>ineffective.<br><br>ESTIMATE-FIRST: run=False (the default) only estimates and prints — no workload spend (meta-capped effort/discovery<br>probes only). run=True makes REAL metered calls under the META cage (caps.meta), grades each output via<br>equivalence.grade, and KILLS a variant whose pilot fails the quality bar before the full sample (a losing arm is<br>never paid in full). SIDE EFFECTS when run: metered spend, console progress, and the ineffective-model denylist is<br>updated. Returns a summary of the per-variant results (cost / score / verdict). A per-call failure is COUNTED, never<br>silently swallowed; a deliberate spend stop or cap refusal propagates. |
| instructions | Any | no | None | A/B/n-test cheaper (model, instruction, effort) VARIANTS for `intent` against its stored call_io samples, and<br>record which hold quality — the programmatic experiment API.<br><br>intent: the job-type label whose sample corpus is tested. models / instructions: the variant axes (lists; None = a<br>default terse-output + cheaper-model set from _variants). n: samples per variant. mode: the equivalence tier<br>('auto' \| 'embed' \| 'rubric' \| 'custom:<module.fn>'). reconsider=True also retests models previously marked<br>ineffective.<br><br>ESTIMATE-FIRST: run=False (the default) only estimates and prints — no workload spend (meta-capped effort/discovery<br>probes only). run=True makes REAL metered calls under the META cage (caps.meta), grades each output via<br>equivalence.grade, and KILLS a variant whose pilot fails the quality bar before the full sample (a losing arm is<br>never paid in full). SIDE EFFECTS when run: metered spend, console progress, and the ineffective-model denylist is<br>updated. Returns a summary of the per-variant results (cost / score / verdict). A per-call failure is COUNTED, never<br>silently swallowed; a deliberate spend stop or cap refusal propagates. |
| n | Any | no | 20 | A/B/n-test cheaper (model, instruction, effort) VARIANTS for `intent` against its stored call_io samples, and<br>record which hold quality — the programmatic experiment API.<br><br>intent: the job-type label whose sample corpus is tested. models / instructions: the variant axes (lists; None = a<br>default terse-output + cheaper-model set from _variants). n: samples per variant. mode: the equivalence tier<br>('auto' \| 'embed' \| 'rubric' \| 'custom:<module.fn>'). reconsider=True also retests models previously marked<br>ineffective.<br><br>ESTIMATE-FIRST: run=False (the default) only estimates and prints — no workload spend (meta-capped effort/discovery<br>probes only). run=True makes REAL metered calls under the META cage (caps.meta), grades each output via<br>equivalence.grade, and KILLS a variant whose pilot fails the quality bar before the full sample (a losing arm is<br>never paid in full). SIDE EFFECTS when run: metered spend, console progress, and the ineffective-model denylist is<br>updated. Returns a summary of the per-variant results (cost / score / verdict). A per-call failure is COUNTED, never<br>silently swallowed; a deliberate spend stop or cap refusal propagates. |
| run | Any | no | False | A/B/n-test cheaper (model, instruction, effort) VARIANTS for `intent` against its stored call_io samples, and<br>record which hold quality — the programmatic experiment API.<br><br>intent: the job-type label whose sample corpus is tested. models / instructions: the variant axes (lists; None = a<br>default terse-output + cheaper-model set from _variants). n: samples per variant. mode: the equivalence tier<br>('auto' \| 'embed' \| 'rubric' \| 'custom:<module.fn>'). reconsider=True also retests models previously marked<br>ineffective.<br><br>ESTIMATE-FIRST: run=False (the default) only estimates and prints — no workload spend (meta-capped effort/discovery<br>probes only). run=True makes REAL metered calls under the META cage (caps.meta), grades each output via<br>equivalence.grade, and KILLS a variant whose pilot fails the quality bar before the full sample (a losing arm is<br>never paid in full). SIDE EFFECTS when run: metered spend, console progress, and the ineffective-model denylist is<br>updated. Returns a summary of the per-variant results (cost / score / verdict). A per-call failure is COUNTED, never<br>silently swallowed; a deliberate spend stop or cap refusal propagates. |
| reconsider | Any | no | False | A/B/n-test cheaper (model, instruction, effort) VARIANTS for `intent` against its stored call_io samples, and<br>record which hold quality — the programmatic experiment API.<br><br>intent: the job-type label whose sample corpus is tested. models / instructions: the variant axes (lists; None = a<br>default terse-output + cheaper-model set from _variants). n: samples per variant. mode: the equivalence tier<br>('auto' \| 'embed' \| 'rubric' \| 'custom:<module.fn>'). reconsider=True also retests models previously marked<br>ineffective.<br><br>ESTIMATE-FIRST: run=False (the default) only estimates and prints — no workload spend (meta-capped effort/discovery<br>probes only). run=True makes REAL metered calls under the META cage (caps.meta), grades each output via<br>equivalence.grade, and KILLS a variant whose pilot fails the quality bar before the full sample (a losing arm is<br>never paid in full). SIDE EFFECTS when run: metered spend, console progress, and the ineffective-model denylist is<br>updated. Returns a summary of the per-variant results (cost / score / verdict). A per-call failure is COUNTED, never<br>silently swallowed; a deliberate spend stop or cap refusal propagates. |
| mode | Any | no | 'auto' | A/B/n-test cheaper (model, instruction, effort) VARIANTS for `intent` against its stored call_io samples, and<br>record which hold quality — the programmatic experiment API.<br><br>intent: the job-type label whose sample corpus is tested. models / instructions: the variant axes (lists; None = a<br>default terse-output + cheaper-model set from _variants). n: samples per variant. mode: the equivalence tier<br>('auto' \| 'embed' \| 'rubric' \| 'custom:<module.fn>'). reconsider=True also retests models previously marked<br>ineffective.<br><br>ESTIMATE-FIRST: run=False (the default) only estimates and prints — no workload spend (meta-capped effort/discovery<br>probes only). run=True makes REAL metered calls under the META cage (caps.meta), grades each output via<br>equivalence.grade, and KILLS a variant whose pilot fails the quality bar before the full sample (a losing arm is<br>never paid in full). SIDE EFFECTS when run: metered spend, console progress, and the ineffective-model denylist is<br>updated. Returns a summary of the per-variant results (cost / score / verdict). A per-call failure is COUNTED, never<br>silently swallowed; a deliberate spend stop or cap refusal propagates. |

Returns: `Any`

<a id="core-c3c635086527a63123a21c0000fb302716963e821d0ca3d77a41c869ef739a47"></a>

### `spendguard.experiment:promote`

Source: `src/spendguard/experiment.py:371`

Run a WINNING config on real inputs and KEEP the output — a successful test IS production work,
not wasted spend. Runs under the REAL intent → WORKLOAD caps (not the meta cage). Estimate-first.
--batch uses the Batch API (50% off, async) for true large chunks; else realtime for slices.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | yes | required | Run a WINNING config on real inputs and KEEP the output — a successful test IS production work,<br>not wasted spend. Runs under the REAL intent → WORKLOAD caps (not the meta cage). Estimate-first.<br>--batch uses the Batch API (50% off, async) for true large chunks; else realtime for slices. |
| model | Any | yes | required | Run a WINNING config on real inputs and KEEP the output — a successful test IS production work,<br>not wasted spend. Runs under the REAL intent → WORKLOAD caps (not the meta cage). Estimate-first.<br>--batch uses the Batch API (50% off, async) for true large chunks; else realtime for slices. |
| instruction | Any | no | '' | Run a WINNING config on real inputs and KEEP the output — a successful test IS production work,<br>not wasted spend. Runs under the REAL intent → WORKLOAD caps (not the meta cage). Estimate-first.<br>--batch uses the Batch API (50% off, async) for true large chunks; else realtime for slices. |
| input | Any | no | None | Run a WINNING config on real inputs and KEEP the output — a successful test IS production work,<br>not wasted spend. Runs under the REAL intent → WORKLOAD caps (not the meta cage). Estimate-first.<br>--batch uses the Batch API (50% off, async) for true large chunks; else realtime for slices. |
| out | Any | no | None | Run a WINNING config on real inputs and KEEP the output — a successful test IS production work,<br>not wasted spend. Runs under the REAL intent → WORKLOAD caps (not the meta cage). Estimate-first.<br>--batch uses the Batch API (50% off, async) for true large chunks; else realtime for slices. |
| n | Any | no | None | Run a WINNING config on real inputs and KEEP the output — a successful test IS production work,<br>not wasted spend. Runs under the REAL intent → WORKLOAD caps (not the meta cage). Estimate-first.<br>--batch uses the Batch API (50% off, async) for true large chunks; else realtime for slices. |
| run | Any | no | False | Run a WINNING config on real inputs and KEEP the output — a successful test IS production work,<br>not wasted spend. Runs under the REAL intent → WORKLOAD caps (not the meta cage). Estimate-first.<br>--batch uses the Batch API (50% off, async) for true large chunks; else realtime for slices. |
| batch | Any | no | False | Run a WINNING config on real inputs and KEEP the output — a successful test IS production work,<br>not wasted spend. Runs under the REAL intent → WORKLOAD caps (not the meta cage). Estimate-first.<br>--batch uses the Batch API (50% off, async) for true large chunks; else realtime for slices. |

Returns: `Any`

<a id="core-28c5e1c4cb6e8e2bf76f2c4d1e5c3b5ed56904cd9e3a9e77c6d29df0da0d1f63"></a>

### `spendguard.fleet:deregister_interpreter`

Source: `src/spendguard/fleet.py:333`

Forget one explicitly retired deployment without changing that interpreter's gate installation.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| python_executable | Any | yes | required | Source parameter python_executable at src/spendguard/fleet.py:333; see complete callable contract. |

Returns: `Any`

<a id="core-cd1911edba24790145b56a748ab1b86d73a60a24125b3e29adc009fd900562cc"></a>

### `spendguard.fleet:fleet_summary`

Source: `src/spendguard/fleet.py:998`

One dict for `spendguard fleet` / doctor, including any identity-verification gap. $0.

Takes no parameters.

Returns: `Any`

<a id="core-09b69dcbd2f4b678c431485c83c989e895508d30bbc61146b4a152dd8f0d04d9"></a>

### `spendguard.fleet:restart_stale`

Source: `src/spendguard/fleet.py:639`

Bring the fleet onto the green commit. For each STALE live consumer, resolve its launchd label; a
launchd-MANAGED one is kickstarted (`launchctl kickstart -k` → it respawns onto the latest code). An unsupervised
stale process is NEVER killed (we could not bring it back) — daemons are returned in `warn` for the operator to
restart, while ephemeral one-shots are returned in `transient` because they will exit on their own. `apply=False`
(default) is a DRY RUN: it reports what it WOULD do without touching anything. Returns
{restarted:[...], warn:[...], transient:[...], dry_run:bool}. $0, never raises — a restart hiccup is reported,
not thrown.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| apply | Any | no | False | Source parameter apply at src/spendguard/fleet.py:639; see complete callable contract. |
| consumer_id | Any | no | None | Source parameter consumer_id at src/spendguard/fleet.py:639; see complete callable contract. |
| all_supervised | Any | no | False | Source parameter all_supervised at src/spendguard/fleet.py:639; see complete callable contract. |

Returns: `Any`

<a id="core-fe4c9387a10d770272001a14ffd07bde4a526d55666a93d146a776dad512f343"></a>

### `spendguard.fleet:running_consumers`

Source: `src/spendguard/fleet.py:544`

The live fleet: one dict per registered process that is still alive, each with `stale` (its served_sha differs
from the current green pointer — it is running code older/other than what deploy promoted). Dead pids are pruned
from the registry (default). `stale` is None when it cannot be told (no green pointer, or a consumer with no
served_sha — a pinned, non-git install — in which case `version` is the only signal). $0, never raises.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| prune | Any | no | True | Source parameter prune at src/spendguard/fleet.py:544; see complete callable contract. |

Returns: `Any`

<a id="core-8eeae857b1177d8b3c25e670f78964984534875106c330ced5eb9b845f29421c"></a>

### `spendguard.fleet:stop_supervised_consumer`

Source: `src/spendguard/fleet.py:711`

Plan or boot out one identity-verified, explicitly launchd-supervised consumer.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| consumer_id | Any | yes | required | Source parameter consumer_id at src/spendguard/fleet.py:711; see complete callable contract. |
| apply | Any | no | False | Source parameter apply at src/spendguard/fleet.py:711; see complete callable contract. |

Returns: `Any`

<a id="core-3f3df827ad109de7d452d496937da7f4ac37bb2db6439c6510e45f504fcf6041"></a>

### `spendguard.focus_export:emit_csv`

Source: `src/spendguard/focus_export.py:141`

Serialize FOCUS rows with the canonical header and JSON-encoded Tags.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| rows | Any | yes | required | FOCUS row mappings; extra keys are ignored and Tags are JSON encoded. |

Returns: `Any`

<a id="core-cc2b8d9ef7b71f37150440535f18fcbe86f5ac7ba5fcab8818d0ba44793fd5bf"></a>

### `spendguard.focus_export:emit_json`

Source: `src/spendguard/focus_export.py:152`

Serialize FOCUS rows as indented JSON, converting unsupported values to strings.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| rows | Any | yes | required | FOCUS row mappings to serialize. |

Returns: `Any`

<a id="core-e0e2e38363bd19ac8d55de1847b6ca38074475c731e6e4ecd98da20c7cadb8bd"></a>

### `spendguard.focus_export:export_rows`

Source: `src/spendguard/focus_export.py:107`

Every spend_events row in the window, projected to FOCUS. Read-only, through the shared ledger connection.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Every spend_events row in the window, projected to FOCUS. Read-only, through the shared ledger connection. |
| until | Any | no | None | Every spend_events row in the window, projected to FOCUS. Read-only, through the shared ledger connection. |

Returns: `Any`

<a id="core-8cfa7fedd8ff067898b691258877f1895e02a23538cb30bdb431df2226bb7366"></a>

### `spendguard.focus_export:reconciliation_summary`

Source: `src/spendguard/focus_export.py:114`

The cross-check a FinOps reader needs: how much of the export is invoice-anchored, and the money on each FOCUS
axis (never summed across axes — BilledCost and est-value stay apart). $ figures are exact-decimal strings.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| rows | Any | yes | required | The cross-check a FinOps reader needs: how much of the export is invoice-anchored, and the money on each FOCUS<br>axis (never summed across axes — BilledCost and est-value stay apart). $ figures are exact-decimal strings. |

Returns: `Any`

<a id="core-42d81a3a5fc253861eddc15b795e36882b01e76fef1c54d938e9df61159fbd95"></a>

### `spendguard.gate:control_gate_runtime`

Source: `src/spendguard/gate.py:2934 (gate._cli)`

Read enforcement status and current budget usage Bind cmd=status; retain install, stdout and exit0. CLI pre-dispatch first-run nudge lives cli.py:510. Audited CLI invocation: spendguard status --live.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| live | bool | no | false | Bind cmd=status; retain install, stdout and exit0. CLI pre-dispatch first-run nudge lives cli.py:510. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-09f864bb727cece42a26a81cb08f84b4728f6b416b25b9b10011fe39cadc8a20"></a>

### `spendguard.guard:decisions_summary`

Source: `src/spendguard/guard.py:321`

Aggregate the decision rows → {decisions, saved_usd, by_intent:[{intent, decisions, saved_usd}]} — the value
proof, per intent. Without a limit, Σ(by_intent saved) == saved_usd is the un-regressable completeness check;
a limit deliberately returns only the highest-saving intent groups while preserving the all-row totals.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Aggregate the decision rows → {decisions, saved_usd, by_intent:[{intent, decisions, saved_usd}]} — the value<br>proof, per intent. Without a limit, Σ(by_intent saved) == saved_usd is the un-regressable completeness check;<br>a limit deliberately returns only the highest-saving intent groups while preserving the all-row totals. |
| intent_limit | Any | no | None | Aggregate the decision rows → {decisions, saved_usd, by_intent:[{intent, decisions, saved_usd}]} — the value<br>proof, per intent. Without a limit, Σ(by_intent saved) == saved_usd is the un-regressable completeness check;<br>a limit deliberately returns only the highest-saving intent groups while preserving the all-row totals. |

Returns: `Any`

<a id="core-276c0cde09dc2bc98b03ff0d572343051feff1447f0bb22e56a85665b9ea2a8d"></a>

### `spendguard.guard:saved_since`

Source: `src/spendguard/guard.py:203`

The guarded-savings running tally since `since`: {by_source, certain, counterfactual, total} in $ (mean).
Mean = Σ of each event's lognormal k1 (the same additive cumulant the org rollup uses — one distribution, here
collapsed to per-source means for a local readout). `certain` sums the MEASURED sources (CERTAIN); everything
else is `counterfactual` — kept SEPARATE so the two are never blurred into one over-confident number. This is a
THIRD axis (avoided $), never added into real-$ (billed) or est-value (plan).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | The guarded-savings running tally since `since`: {by_source, certain, counterfactual, total} in $ (mean).<br>Mean = Σ of each event's lognormal k1 (the same additive cumulant the org rollup uses — one distribution, here<br>collapsed to per-source means for a local readout). `certain` sums the MEASURED sources (CERTAIN); everything<br>else is `counterfactual` — kept SEPARATE so the two are never blurred into one over-confident number. This is a<br>THIRD axis (avoided $), never added into real-$ (billed) or est-value (plan). |

Returns: `Any`

<a id="core-4148b398c6c6c0607f24cb02d758270dbb7dd9cecacb1f4b8bafdf514a9494cb"></a>

### `spendguard.guard:shed_summary`

Source: `src/spendguard/guard.py:431`

Shed effectiveness from the decision ledger: which lanes work was shed OFF, onto which READY lane, how much
REAL overage $ that avoided, and how much quota it preserved — the proof the shed is working, per route.

Returns {n_sheds, avoided_overage_usd, quota_preserved_usd, served_free_n, paid_n, by_route:[{from_lane, to_lane,
class, n, served_free_n, counterfactual_usd}]}. avoided_overage_usd sums ONLY the 'overage' class served free (the
one case shedding saves REAL money — a plan on paid overage would have billed); quota_preserved_usd sums the
'quota'/'proactive' classes (est-value moved to another free lane, NOT real money) — kept on a SEPARATE axis so
the two are never blurred (the same split the cost doctrine requires for billed vs est-value).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Shed effectiveness from the decision ledger: which lanes work was shed OFF, onto which READY lane, how much<br>REAL overage $ that avoided, and how much quota it preserved — the proof the shed is working, per route.<br><br>Returns {n_sheds, avoided_overage_usd, quota_preserved_usd, served_free_n, paid_n, by_route:[{from_lane, to_lane,<br>class, n, served_free_n, counterfactual_usd}]}. avoided_overage_usd sums ONLY the 'overage' class served free (the<br>one case shedding saves REAL money — a plan on paid overage would have billed); quota_preserved_usd sums the<br>'quota'/'proactive' classes (est-value moved to another free lane, NOT real money) — kept on a SEPARATE axis so<br>the two are never blurred (the same split the cost doctrine requires for billed vs est-value). |

Returns: `Any`

<a id="core-c78880e65f8c3404b23e258ae17227180ba2558a63ec4f5edf5f4c87c031fa5e"></a>

### `spendguard.history:enrich_graph`

Source: `src/spendguard/history.py:177`

Add `preceded` (per-intent chronological run chain) + `derived_from` (insight → cited runs).

Takes no parameters.

Returns: `Any`

<a id="core-9cbbef118f87cd2e3444bdb0045c43b976d481c04f7ea9840a93d050f809915e"></a>

### `spendguard.history:mine_git`

Source: `src/spendguard/history.py:224`

Weak quality signals from git history of the batch-id artifacts. Commit-subject relevance is decided
AGENTICALLY — conv.classify_evidence cost_lesson (nano, ~free, recorded, caged by caps.meta) — replacing the old
_SIGNAL keyword regex (fix|bug|wrong…) that decided by topic word and silently dropped real cost commits.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| repo | Any | yes | required | Source parameter repo at src/spendguard/history.py:224; see complete callable contract. |
| apply | Any | no | False | Source parameter apply at src/spendguard/history.py:224; see complete callable contract. |
| max_commits | Any | no | 400 | Source parameter max_commits at src/spendguard/history.py:224; see complete callable contract. |

Returns: `Any`

<a id="core-50c519383b35875eeee66d2331e2c94482cbf0b754bdaf1f2eebcb84c7f8b415"></a>

### `spendguard.history:reconstruct_intents`

Source: `src/spendguard/history.py:125`

Match the mined intent map against our run nodes; report coverage; optionally write it on.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| repo | Any | yes | required | Source parameter repo at src/spendguard/history.py:125; see complete callable contract. |
| apply | Any | no | False | Source parameter apply at src/spendguard/history.py:125; see complete callable contract. |

Returns: `Any`

<a id="core-eda57e550bf214b1f2efde61b1c321ed7a0fe5e267369e3291bcc2e15e44a63e"></a>

### `spendguard.keys_audit:audit_provider_key_shadows`

Source: `src/spendguard/keys_audit.py:40 (keys_audit.cmd)`

Audit code for key usage and gating coverage Compose config dotenv scan/report and renderer; exit1 on differing shadow or strict any findings; help is presentation. Audited CLI invocation: spendguard keys-audit --json --strict.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| paths | list[str] {"alternatives": [], "enum": [], "items": {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, "properties": [], "python_annotation": "list[str]", "type": "array"} | no | {"source": "src/spendguard/keys_audit.py:cmd", "expression": "nonflag rest or ['.']"} | Compose config dotenv scan/report and renderer; exit1 on differing shadow or strict any findings; help is presentation. |
| strict | bool | no | false | Compose config dotenv scan/report and renderer; exit1 on differing shadow or strict any findings; help is presentation. |
| json | bool | no | false | Compose config dotenv scan/report and renderer; exit1 on differing shadow or strict any findings; help is presentation. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-7e13577e7cf518fb23a200d3e3b03f5e18e8334c07185ca3455f9fd9043664ec"></a>

### `spendguard.lane_balance:bandit_list_coverage`

Source: `src/spendguard/lane_balance.py:1415`

Which bandit allow/deny-list entries are UNMATCHED — they match NO intent recorded in the calls ledger. This
is a FACT, not a verdict: the determination runs the SAME _intent_listed the router uses (exact / trailing ':'
or '*' prefix) against the DISTINCT recorded intents, so an unmatched entry is one the router would never act on
given what has actually been seen. It may be a typo, a stale name, OR an intent not yet run — which of those is
for the operator to judge; the guard's job is only to make the absence VISIBLE (two unmatched denylist entries
once sat in a live config and nothing said so — axis-4). {list_key: {entries, unmatched, seen_n, read_ok}} per
configured list; unmatched is None when the ledger could not be read. Read-only, $0 — surfaced by `spendguard doctor`.

Takes no parameters.

Returns: `Any`

<a id="core-3a95c6dbcc309be1f46006b559696b715b926059da92d2a097eb45bfe3e99237"></a>

### `spendguard.lane_balance:bulk_delegate`

Source: `src/spendguard/lane_balance.py:441`

Fan a LIST of similar tasks across ALL viable idle lanes CONCURRENTLY — the right shape for a BULK job (e.g.
symgrep's ~6k one-sentence symbol descriptions) that the per-call bandit would trickle one at a time. Each task
runs on a lane (round-robin across the lanes the bandit rates GOOD for this intent), each admission BOUNDED by
the dispatch GOVERNOR (per-lane in-flight cap, so it FILLS the plans without tripping their throttle). So it is
FAST (parallel across lanes), $0 (plan-served; a lane failure falls back to that provider's API, flagged
`billed` — unless `refuse_billed`, which makes a lane miss an error row and NEVER bills, $0 by construction), and
spread across EVERY good lane, not one.

`metered_only=True` forces a PINNED (`model_for`) matrix onto the METERED half of its atomic pair — every vote
skips the $0 lane and rides the paid API (adapters.call(metered_only=True)). The opt-in for a consistency-
sensitive fan (a verdict-cached refuter) that needs the concurrency-invariant metered path serial-equivalently,
where the lane CLI's own reasoning scale + warm-daemon concurrency would drift the verdict. It BILLS — the
deliberate trade for a fan where the verdict distribution is the product. Default False = the faithful-for-
capability atomic pair (lane first, same-provider metered fallback). Applies to the pinned/vision runner only.

`record_route=True` (default): with advisor.route_through_queue ON, each per-task adapters.call records its OWN
durable queue row (observability + crash-recovery) as usual. `record_route=False` SUPPRESSES that per-task record
— set ONLY by lane_queue.drain / lane_queue.submit, because those tasks ARE already leased queue rows and a
per-task record would open a SECOND row for the same logical work (a double-record). It threads to adapters.call
as `_route=`, so it holds across the plain runner AND the hedge's nested threads (a thread-local guard would not).

DURABLE (the CHUNK-never-single-shot rule): tasks run in chunks of `chunk_size`; when `checkpoint` (a jsonl path)
is given, EACH completed result is appended before the next chunk, so a crash RESUMES instead of losing the run.
Resume is keyed by CONTENT (sha256 of system+task+intent), never position — a re-run whose task list changed
(different order/composition) maps each saved result back by MEANING, so it can never land on the wrong task; an
older POSITIONAL checkpoint is detected and ignored with a notice, never misread. One task that errors returns an
error row and never wedges the chunk.

`schema` (a JSON Schema) forces STRUCTURED output on every task — so a JSON-envelope job (e.g. warden's
{results:[{id,…}]} contract with cross-model strict-mode parity) can ride the parallel lane fan instead of being
forced through per-call adapters.call. It is threaded straight to adapters.call: the shape rides each lane's
prompt and is validated locally (output_contract), and a lane whose reply is off-shape falls back to that model's
metered API (strict on OpenAI/Anthropic) — so the envelope holds on every lane. The result stays in `text` (the
JSON string), so the success contract, checkpoint/resume, and tier confinement are unchanged.

`expect_ids` closes the ARITY hole for PACKED envelopes: a lane can return a shape-PERFECT {results:[{id,…}]}
that silently OMITS ids (measured ~7% on packed batches), and the per-item shape check is blind to an item that
never came back — so it would be accepted as a $0 lane success and lost. Pass `expect_ids` = a callable
(task → the ids that task packed) or a single id-list (all tasks); each result runs through
output_contract.check_envelope, and an INCOMPLETE envelope becomes a MISS (retried via the checkpoint, surfaced
with `arity_miss`), NEVER silently counted done.

`lanes` CONFINES the fan to an explicit lane subset (e.g. only the fungible lanes, excluding a protected
interactive one) — the same `lanes=` `_bulk_arms` and the tier path already accept, forwarded so a caller can
say "fan across codex+gemini+zai, never claude-code" without mutating advisor.delegate_lanes. None = every
delegate lane. A lane named here that is reserved/cooling still drops out (fail-closed), never widened past.

`model_for` PINS each task to its EXACT vendor:model, NEVER bandit-substituted — the sanctioned governed MATRIX fan
for a cross-vendor CONSENSUS panel (N files × M named vendors, e.g. honestreview's repo review: submit the WHOLE
matrix, set no concurrency number, and the governor bounds per-vendor in-flight, queues the rest, keys results).
Each task rides its PROVIDER-LOCKED atomic pair: the $0 subscription LANE first (an openai pin → the codex lane, an
anthropic pin → claude-code, an agy/Gemini pin → the gemini lane), falling back to that SAME provider's metered API
at EQUAL-OR-GREATER reasoning (reasoning_equivalence.resolve_metered — never a different vendor, never less
reasoning), governed + checkpointed/resumed + arity-checked by the SAME durable core as the lane fan. WHICH VENDOR
answered is the measurement and never changes; `served_by_metered_api` on each row shows WHICH HALF of the pair
served (a $0 lane vs the paid API), and `reasoning`/`effort` show the tier applied — so a caller can see exactly how
each vote ran. This is the TEXT case of the pinned runner; `model_for(task)` selects the model whether or not there
are images (the VISION case rides the metered API by construction — the lanes are text-only).

`images_for` + `vision_model` are the IMAGE case of that same runner — the lane executors are text-only CLIs with
no image channel (the trap behind a labeler that cold-400'd). Pass `images_for` = a callable (task → the image
path(s)/data-URL(s)) or a single image list, and `vision_model` = a vision-capable API model (e.g.
'openai:gpt-5-nano'), or `model_for` for a per-task VISION panel. Every task rides adapters.call(images=…) —
governed, checkpointed/resumed, refuse-billed and arity-checked, just a different per-task runner. lanes/tier/arms
do not apply to either pinned shape; vision_model (or model_for) is REQUIRED when images_for is set (else every
task errors, re-runnable). Hedging/bandit are lane-only — a pinned task must not race onto another vendor.

`gate_sig` carries the bulk gate onto the LANE path (a lane runs a CLI subprocess, so the SDK-patch gate never
fires — lane bulk was otherwise ungoverned). A genuinely-bulk fan (>= bulkgate.preview_max) is subject to the
SAME SPENDGUARD_ENFORCE discipline the SDK path gets: run bulkgate.gated_batch(...) (estimate→test→eval) and pass
the sig you gated as `gate_sig`; an ungated fan is refused under `block`, logged 'would-block' under `warn`
(the default), allowed under `off`. force=True overrides. ($0 lanes have no spend to cap — this gate is the
test/eval discipline, which is about whether the job was proven before N thousand items ran, not about dollars.)

`task_key` + `return_keyed` pair results back to tasks by MEANING, never position — the same "never by
position" rule the checkpoint/resume already enforces (`_content_key`), extended to the RETURN so a caller
can't mis-pair either. The default return is a task-ordered LIST (unchanged), but zipping it with anything but
the exact `tasks` list — a caller that deduped, filtered, or reordered before fanning — silently crosses rows
onto the wrong item (the mis-paired-card bug). Pass `task_key` = a callable (task → a hashable key) or a list
of keys (one per task) and `return_keyed=True` to get back a DICT {key: row}: pairing is explicit and can
never drift. Keys MUST be unique per task — a duplicate raises (a colliding key would silently DROP a result,
the very failure this closes), fail-loud not fail-quiet. `task_key=None` keys by index (always unique).

`on_miss` names what happens to a task the LANES do not serve — the degradation DIRECTION, made explicit
instead of discovered on an invoice (the default per-task realtime fallback is the MOST expensive shape):
  · "api"   — the current default: a lane miss falls to that model's per-task REALTIME metered API.
  · "error" — a lane miss is a free error row, never billed ($0 by construction). == refuse_billed=True.
  · "batch" — run lanes with NO realtime fallback, then take the MISS SET (no text) as one group. Callers with
              a batch path want this: lanes first, batch the remainder (~50% cheaper). If `batch_submit` (a
              callable: the miss tasks → a batch handle/id, e.g. via bulkgate.gated_batch) is given, it is
              invoked ONCE on the misses and those rows become reason="queued_batch" carrying `batch`=<handle>
              (async: bulk_delegate never blocks on the 24h batch window); without it, the misses become
              reason="batch_eligible" (a STRUCTURED signal to route them to your own batch path). Pairs with
              return_keyed so the caller re-associates queued rows by key, never by position.
              With NO batch_submit but a resolvable batch model (`batch_model=` or config advisor.batch_model),
              submit_chat_tasks becomes the DEFAULT overflow submitter — estimate-first + request-capped inside
              guarded_submit, `batch_cap=` / advisor.batch_cap_usd the optional $ ceiling — so strategy="auto"
              submits the overflow to the REAL OpenAI Batch API with no caller wiring. No batch model resolvable
              → the misses stay batch_eligible (an uncapped auto-submit never happens by omission).
`on_miss` takes precedence over `refuse_billed` when both are set; None keeps the `refuse_billed` behaviour.

`strategy="auto"` is the QUOTA-AWARE split (the economic model as one flag): spendguard picks WHICH plan lanes to
fan and BATCHES the overflow, from the MEASURED lane economics, so the caller sets no lanes/on_miss. It fans only
the UNDER-USED, non-reserved plans (idle/warm by est-value — a HOT plan's quota is scarce and would cost more than
a batch, so it is spared) and defaults on_miss="batch" (pass batch_submit for the overflow). Reuses
lane_utilization's idle/warm/HOT state + prompt_lane_reserved — no new threshold. Text lane fan only (a
pinned/vision matrix is metered by construction); economics-unavailable → the normal fan (still batches overflow).

`hedge_ms` overrides the config tail-hedge window for THIS call (a small-fan caller opts in without a global that
would also arm bulk); None → `dispatch.lane_hedge_ms`. Only fires on the lane path with spare capacity (never a
pinned matrix). See dispatch.lane_hedge_ms.

`sla_class="batch"` admits every task of this fan against the governor's BATCH sub-limit, so a big low-priority fan
can't starve a concurrent realtime call of a slot (only bites where dispatch.realtime_reserve[_<key>] is set for
that lane/vendor; off by default → no change). The queue drain passes the leased item's sla_class here; None (the
default) is realtime and uses the full limit.

Returns a task-ordered LIST [{text, lane, use_name, model, billed, error}], or {key: row} when return_keyed.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| tasks | Any | yes | required | Source parameter tasks at src/spendguard/lane_balance.py:441; see complete callable contract. |
| intent | Any | yes | required | Source parameter intent at src/spendguard/lane_balance.py:441; see complete callable contract. |
| system | Any | no | None | Source parameter system at src/spendguard/lane_balance.py:441; see complete callable contract. |
| reasoning | Any | no | None | Source parameter reasoning at src/spendguard/lane_balance.py:441; see complete callable contract. |
| max_workers | Any | no | None | Source parameter max_workers at src/spendguard/lane_balance.py:441; see complete callable contract. |
| deadline_s | Any | no | 120.0 | Source parameter deadline_s at src/spendguard/lane_balance.py:441; see complete callable contract. |
| checkpoint | Any | no | None | Source parameter checkpoint at src/spendguard/lane_balance.py:441; see complete callable contract. |
| chunk_size | Any | no | 100 | Source parameter chunk_size at src/spendguard/lane_balance.py:441; see complete callable contract. |
| refuse_billed | Any | no | False | Source parameter refuse_billed at src/spendguard/lane_balance.py:441; see complete callable contract. |
| stats | Any | no | None | Source parameter stats at src/spendguard/lane_balance.py:441; see complete callable contract. |
| force | Any | no | False | Source parameter force at src/spendguard/lane_balance.py:441; see complete callable contract. |
| tier | Any | no | None | Source parameter tier at src/spendguard/lane_balance.py:441; see complete callable contract. |
| schema | Any | no | None | Source parameter schema at src/spendguard/lane_balance.py:441; see complete callable contract. |
| expect_ids | Any | no | None | Source parameter expect_ids at src/spendguard/lane_balance.py:441; see complete callable contract. |
| gate_sig | Any | no | None | Source parameter gate_sig at src/spendguard/lane_balance.py:441; see complete callable contract. |
| lanes | Any | no | None | Source parameter lanes at src/spendguard/lane_balance.py:441; see complete callable contract. |
| images_for | Any | no | None | Source parameter images_for at src/spendguard/lane_balance.py:441; see complete callable contract. |
| vision_model | Any | no | None | Source parameter vision_model at src/spendguard/lane_balance.py:441; see complete callable contract. |
| model_for | Any | no | None | Source parameter model_for at src/spendguard/lane_balance.py:441; see complete callable contract. |
| prompt_for | Any | no | None | Source parameter prompt_for at src/spendguard/lane_balance.py:441; see complete callable contract. |
| task_key | Any | no | None | Source parameter task_key at src/spendguard/lane_balance.py:441; see complete callable contract. |
| return_keyed | Any | no | False | Source parameter return_keyed at src/spendguard/lane_balance.py:441; see complete callable contract. |
| on_miss | Any | no | None | Source parameter on_miss at src/spendguard/lane_balance.py:441; see complete callable contract. |
| batch_submit | Any | no | None | Source parameter batch_submit at src/spendguard/lane_balance.py:441; see complete callable contract. |
| batch_model | Any | no | None | Source parameter batch_model at src/spendguard/lane_balance.py:441; see complete callable contract. |
| batch_cap | Any | no | None | Source parameter batch_cap at src/spendguard/lane_balance.py:441; see complete callable contract. |
| hedge_ms | Any | no | None | Source parameter hedge_ms at src/spendguard/lane_balance.py:441; see complete callable contract. |
| strategy | Any | no | None | Source parameter strategy at src/spendguard/lane_balance.py:441; see complete callable contract. |
| metered_only | Any | no | False | Source parameter metered_only at src/spendguard/lane_balance.py:441; see complete callable contract. |
| sla_class | Any | no | None | Source parameter sla_class at src/spendguard/lane_balance.py:441; see complete callable contract. |
| record_route | Any | no | True | Source parameter record_route at src/spendguard/lane_balance.py:441; see complete callable contract. |
| budget_usd | Any | no | None | Source parameter budget_usd at src/spendguard/lane_balance.py:441; see complete callable contract. |
| auth_over_quota | Any | no | False | Source parameter auth_over_quota at src/spendguard/lane_balance.py:441; see complete callable contract. |

Returns: `Any`

<a id="core-3f109b35b1bf688a76d42f91663c9d8c85ea1691cd3a1ae8352fbee7f0a9f211"></a>

### `spendguard.lane_balance:confirm_substitute`

Source: `src/spendguard/lane_balance.py:176`

The 'confirm once' step: promote one proposed substitute to CONFIRMED so the router may use it. Idempotent.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | yes | required | Source parameter intent at src/spendguard/lane_balance.py:176; see complete callable contract. |
| substitute | Any | yes | required | Source parameter substitute at src/spendguard/lane_balance.py:176; see complete callable contract. |

Returns: `Any`

<a id="core-d25c092df161c0f7945bda126685f54b33aa813e858eb57cf8fe78b8c2c872f2"></a>

### `spendguard.lane_balance:delegate`

Source: `src/spendguard/lane_balance.py:187`

Offload one task to the cheapest VIABLE idle subscription lane and return the answer — so heavy work runs $0
on an idle plan while the orchestrator (e.g. this Claude Code session) spends nothing but coordination.

enqueue=True instead DURABLY QUEUES the task (at `priority`, default INTERACTIVE so it drains ahead of bulk
backfill) for the drainer to run later on an idle lane, and returns {queued: id, ...} without running it — the
right call under high utilization or for fire-and-forget work (the drainer, `spendguard lanes --drain`, empties
it onto idle plans at $0).

Picks from the viable delegation lanes (config `advisor.delegate_lanes`, default the ones MEASURED fast + $0:
gemini, zai — codex is EXCLUDED, its CLI is an agent, >75s on a real prompt → metered fallback), LEAST-UTILISED
first, running each lane's model from `advisor.lane_models` at LOW reasoning (gemini-HIGH returns EMPTY — hidden
reasoning eats the budget, so lane_models should point gemini at a `-low` variant). EMPTY or errored output is a
FAILURE → fall through to the next lane; billed fallback is NOT silent (a lane that answered via the metered API
is flagged `billed=True`). Returns {text, lane, model, cost, billed, executor, tried} or {text:None, error, tried}.
The model that answered is the one recorded — attribution stays honest.

`schema` (a JSON Schema) forces STRUCTURED output — threaded to adapters.call, which folds the shape into the
lane's prompt, validates the reply locally (output_contract), and falls back to that model's metered API (strict
on OpenAI/Anthropic) if a lane's output is off-shape. The JSON answer stays in `text`. (A schema request skips
the text-only lane bandit and uses this cheapest-idle path, which still gets adapters-level lane arbitrage.)

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| task | Any | yes | required | Source parameter task at src/spendguard/lane_balance.py:187; see complete callable contract. |
| system | Any | no | None | Source parameter system at src/spendguard/lane_balance.py:187; see complete callable contract. |
| lanes | Any | no | None | Source parameter lanes at src/spendguard/lane_balance.py:187; see complete callable contract. |
| reasoning | Any | no | 'low' | Source parameter reasoning at src/spendguard/lane_balance.py:187; see complete callable contract. |
| max_tokens | Any | no | None | Source parameter max_tokens at src/spendguard/lane_balance.py:187; see complete callable contract. |
| intent | Any | no | None | Source parameter intent at src/spendguard/lane_balance.py:187; see complete callable contract. |
| enqueue | Any | no | False | Source parameter enqueue at src/spendguard/lane_balance.py:187; see complete callable contract. |
| priority | Any | no | None | Source parameter priority at src/spendguard/lane_balance.py:187; see complete callable contract. |
| schema | Any | no | None | Source parameter schema at src/spendguard/lane_balance.py:187; see complete callable contract. |
| refuse_billed | Any | no | False | Source parameter refuse_billed at src/spendguard/lane_balance.py:187; see complete callable contract. |

Returns: `Any`

<a id="core-3ef84fd65013aac7caa70bc75e435d895b158bb30362778f25cca47a77fd4145"></a>

### `spendguard.lane_balance:estimate_fan`

Source: `src/spendguard/lane_balance.py:1265`

ZERO-SPEND preview of a bulk_delegate fan — the estimate-first discipline for the LANE path. bulk_delegate
itself just runs; this answers "what would it cost / where would it land" BEFORE a single call is made. Returns
a plan dict, never touches a lane or an API:
  · n_tasks / n_distinct — total vs DISTINCT calls (identical tasks share one content key → one real call), so
    the dedup win is visible before running;
  · arms — the (lane, model) set the fan WOULD spread across, resolved the SAME way bulk_delegate resolves it
    (tier= → each lane's right-sized model; else the bandit's GOOD lanes for this intent), reserved lanes
    filtered out identically; or `viable=False` + a reason when none is available (no spend either way);
  · est_metered_usd_worst — the CEILING: every DISTINCT task falls off its lane to that model's METERED API
    (the refuse_billed=False worst case). On plan-served lanes the real spend is $0; this is the number to
    approve a fan against, never the expected cost. Priced from `pricing` (never a literal); INPUT tokens are
    exact from the task+system text, OUTPUT from the intent's MEASURED p99 (bulkgate.maxtokens) when known,
    else `out_est`, else a config nominal — `out_basis` says which so the estimate is auditable.
Mirrors crossllm's "budget_usd is None → estimate only, never spends" pattern for the lane fan.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| tasks | Any | yes | required | Source parameter tasks at src/spendguard/lane_balance.py:1265; see complete callable contract. |
| intent | Any | yes | required | Source parameter intent at src/spendguard/lane_balance.py:1265; see complete callable contract. |
| system | Any | no | None | Source parameter system at src/spendguard/lane_balance.py:1265; see complete callable contract. |
| lanes | Any | no | None | Source parameter lanes at src/spendguard/lane_balance.py:1265; see complete callable contract. |
| tier | Any | no | None | Source parameter tier at src/spendguard/lane_balance.py:1265; see complete callable contract. |
| out_est | Any | no | None | Source parameter out_est at src/spendguard/lane_balance.py:1265; see complete callable contract. |

Returns: `Any`

<a id="core-602afd1cb98557e7b1c5288f94ea93dd1ffa8008d7175c71e89d7e8c7b45215d"></a>

### `spendguard.lane_balance:format_utilization`

Source: `src/spendguard/lane_balance.py:1562`

One line per lane for `spendguard lanes --balance` and the router's rationale. Pure est-VALUE (plan usage) —
split from billed $ per the cost-display rule, and explicitly NOT the provider's quota.

Takes no parameters.

Returns: `Any`

<a id="core-f802c7bc16c272770f48be601912ebfccd701a135bc09340c37b1b3a7bbd418d"></a>

### `spendguard.lane_balance:propose_substitutes`

Source: `src/spendguard/lane_balance.py:1608`

AGENTIC 'model proposes' step: a cheap judge (advisor.judge_model) decides which idle-lane candidate models are
acceptable substitutes for `intent`, RECORDED AS PENDING for Ash to confirm (never auto-used). Caged as the
meta intent so its own tiny spend is attributed. Returns {acceptable, rationale, pending}.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | yes | required | Source parameter intent at src/spendguard/lane_balance.py:1608; see complete callable contract. |
| primary_model | Any | yes | required | Source parameter primary_model at src/spendguard/lane_balance.py:1608; see complete callable contract. |
| candidates | Any | no | None | Source parameter candidates at src/spendguard/lane_balance.py:1608; see complete callable contract. |

Returns: `Any`

<a id="core-37c4c731f0144a74954d208ffa637875b991ae2b96585ec326f4ae72569339ad"></a>

### `spendguard.lane_balance:register_critical`

Source: `src/spendguard/lane_balance.py:1444`

Register no-substitution VENDOR-CRITICAL intent patterns into advisor.bandit_denylist — the SANCTIONED way for a
CONSUMER (e.g. warden) to keep its cross-vendor-panel pins in spendguard's config so they can't DRIFT from the
consumer's own declaration. The REAL guarantee stays the per-call `no_substitution=True`; this belt-and-suspenders
layer is now drift-proof AND attributable: `source` is recorded per pattern in advisor.bandit_denylist_sources, so
`doctor` reads a not-yet-run REGISTERED pin as "registered by <source>" rather than flagging it as a possible typo.
Idempotent — merge + sort + dedup; re-registering the same pattern is a no-op. Returns the merged denylist.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| patterns | Any | yes | required | Source parameter patterns at src/spendguard/lane_balance.py:1444; see complete callable contract. |
| source | Any | no | None | Source parameter source at src/spendguard/lane_balance.py:1444; see complete callable contract. |

Returns: `Any`

<a id="core-cfbb4ab8138c26e70e0a9f8c70d4662e850c6a3b623fdb26e5215a1ed5d2541f"></a>

### `spendguard.lane_bandit:estimate_judge_cost`

Source: `src/spendguard/lane_bandit.py:406`

ZERO-SPEND estimate of the bandit's ONLY real cost — the bake-off JUDGE. Per bake-off is ONE cheap judge call
(the two lane answers are $0, plan-served). Prices the judge prompt (boilerplate + task + two answers, at the
BOUNDED sizes the judge truncates to) + the tiny output cap, at the judge model's rate, then projects a monthly $
for a few bake-off counts. No LLM is called — this is arithmetic over pricing.py.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| bakeoffs | Any | no | (10, 100, 1000) | Source parameter bakeoffs at src/spendguard/lane_bandit.py:406; see complete callable contract. |

Returns: `Any`

<a id="core-4d269fb22816632e9215ae3b0be5bdf40d941879c1023dd353d6a1c1b5097379"></a>

### `spendguard.lane_bandit:learned_table`

Source: `src/spendguard/lane_bandit.py:422`

What the bandit has learned, for `spendguard lanes --learn`: {intent: [(lane, use_name, winrate, trials), …]}
sorted best-first. All intents when intent is None.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | no | None | Source parameter intent at src/spendguard/lane_bandit.py:422; see complete callable contract. |

Returns: `Any`

<a id="core-fbf2bd88e1402f846dc39fef7cdab67e403f6651d4052db5c60bb149ec7960cf"></a>

### `spendguard.lane_bandit:run_bakeoff`

Source: `src/spendguard/lane_bandit.py:310`

Run the two LEAST-tried live arms on the SAME task, judge which is better, record the outcome for BOTH, and
RETURN the winner's text (the caller still gets a usable answer). None if fewer than 2 live arms. This is where
the bandit LEARNS — two $0 lane calls + one cheap judge call.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | yes | required | Source parameter intent at src/spendguard/lane_bandit.py:310; see complete callable contract. |
| task | Any | yes | required | Source parameter task at src/spendguard/lane_bandit.py:310; see complete callable contract. |
| system | Any | no | None | Source parameter system at src/spendguard/lane_bandit.py:310; see complete callable contract. |
| reasoning | Any | no | None | Source parameter reasoning at src/spendguard/lane_bandit.py:310; see complete callable contract. |
| timeout_s | Any | no | None | Source parameter timeout_s at src/spendguard/lane_bandit.py:310; see complete callable contract. |

Returns: `Any`

<a id="core-02c3faf57d29410f840bf770b52c29175b6a11451c4a3c02c969dc473349f053"></a>

### `spendguard.lane_catalog:catalog`

Source: `src/spendguard/lane_catalog.py:119`

The whole table: per lane → {provider, base, reasoning: {style, levels, default}, use_names, price_1m_1m}. This
is what `spendguard lanes --catalog` prints and what the bandit reads its ARMS from.

Takes no parameters.

Returns: `Any`

<a id="core-53af2eeb48467a2eef169b9ff28df83d60229725aad7311f909a45c63f5477b5"></a>

### `spendguard.lane_catalog:format_lane_fallback`

Source: `src/spendguard/lane_catalog.py:178`

The `spendguard lanes --fallback` view of audit_lane_fallback: one line per lane use-name → its metered-API
equivalent, with priced/served and a ✓/✗. Names any BROKEN equivalence (a lane that could not fall back to the
paid API) at the end, so 'a down lane strands the call' is caught here, not in production.

Takes no parameters.

Returns: `Any`

<a id="core-3d48a9dd4df10dc2b90952096e3946663352212258d8cfadf983db2cc333f616"></a>

### `spendguard.lane_economics:format_economics`

Source: `src/spendguard/lane_economics.py:480`

The `spendguard lanes --economics` view: per plan, the MEASURED token cap, tokens left, effective $/token, the
fee at risk if the window resets unused, and what the same traffic WOULD have cost on the cheapest sunk-pool
metered (so a plan's saving is visible). Read-only; no spend. Lanes with no quota surface are named, not faked.
The evidence COUNT (consumption pairs) is shown raw so the reader judges sufficiency — no hard-coded 'measured'
cutoff.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| headroom_rows | Any | no | None | Source parameter headroom_rows at src/spendguard/lane_economics.py:480; see complete callable contract. |

Returns: `Any`

<a id="core-be26dee7a15789982c814ac30f6401b4d2d7eec71ab5de4cdc8f665a1361d6ff"></a>

### `spendguard.lane_eligibility:lane_eligible_report`

Source: `src/spendguard/lane_eligibility.py:74`

{"eligible_usd", "eligible_tok", "eligible_calls", "total_metered_usd", "judged", "unjudged",
"unjudged_usd", "est_judge_usd", "by_intent": [...]} for realtime metered spend since `since` (default: month
start). $0 with execute=False (reads only cached verdicts; counts how many intents remain unjudged and what it
would cost to classify them). With execute=True it judges the unjudged intents (opt-in spend, cached). The figure
is intent-folded (summed across models); a representative model + average input size drive the judge prompt.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Source parameter since at src/spendguard/lane_eligibility.py:74; see complete callable contract. |
| execute | Any | no | False | Source parameter execute at src/spendguard/lane_eligibility.py:74; see complete callable contract. |

Returns: `Any`

<a id="core-cfc85a9600677ff20dfe9c0a25629293e8eca80e9ca7bf0e92dd2f7a780e0aed"></a>

### `spendguard.lane_queue:drain`

Source: `src/spendguard/lane_queue.py:1506`

Lease → run via lane_balance.bulk_delegate → settle, repeatedly. FOREGROUND (forever=False): stop after
`idle_rounds` consecutive empty leases (the queue is drained). DAEMON (forever=True): keep waiting for new work.
Respects a local load ceiling (pauses leasing while the machine is overloaded). `max_iters` hard-caps total loop
passes (a safety stop for bounded drains / tests / a persistently-overloaded box that would otherwise spin).
Returns a summary {ran, done, failed, billed, by_lane, rounds} (rounds = passes that actually ran work).
A capped stop with unfinished or unknown work also returns capped=True and remaining_at_cap counts
(None means the status read was unavailable). Never
raises out.

Reuses the whole existing execution path — governor admission, bandit routing, $0 plan-served, API fallback —
so the queue is PURELY the durability + priority + crash-recovery layer on top.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| worker | Any | no | None | Source parameter worker at src/spendguard/lane_queue.py:1506; see complete callable contract. |
| batch | Any | no | None | Source parameter batch at src/spendguard/lane_queue.py:1506; see complete callable contract. |
| lease_s | Any | no | None | Source parameter lease_s at src/spendguard/lane_queue.py:1506; see complete callable contract. |
| idle_rounds | Any | no | None | Source parameter idle_rounds at src/spendguard/lane_queue.py:1506; see complete callable contract. |
| idle_sleep | Any | no | None | Source parameter idle_sleep at src/spendguard/lane_queue.py:1506; see complete callable contract. |
| load_ceiling | Any | no | None | Source parameter load_ceiling at src/spendguard/lane_queue.py:1506; see complete callable contract. |
| forever | Any | no | False | Source parameter forever at src/spendguard/lane_queue.py:1506; see complete callable contract. |
| max_iters | Any | no | None | Source parameter max_iters at src/spendguard/lane_queue.py:1506; see complete callable contract. |

Returns: `Any`

<a id="core-54e5561dce65f9d44ec8f456e9519013568678fbd6fc48be7c6a03a5b1b86fe2"></a>

### `spendguard.lane_queue:enqueue_many`

Source: `src/spendguard/lane_queue.py:358`

Append many tasks of ONE intent in a single transaction (a 6k-item backfill is one commit). `sla_class`
('realtime' | 'batch') and `deadline_ts` (absolute ISO SLA deadline, or None) are stamped on every row so the
scheduler/drain can serve tight-deadline realtime work first within capacity. Returns the row ids in order; empty
input or a transient sqlite OperationalError returns [] (the latter with a loud stderr diagnostic). Guard,
deliberate-stop, lock, and programming errors propagate.

CONTENT-ADDRESSED DEDUP (`dedup`, default True): the queue NEVER re-buys work it already has. Before inserting,
each task's fingerprint (_fingerprint of intent/task/system/reasoning) is looked up; if a non-FAILED row with the
same fingerprint already exists, this task COALESCES onto it — returning that existing id instead of inserting a
row that would re-execute — preferring a DONE row (its result is reused for $0) over an in-flight pending/leased
one (the single execution serves every caller). Duplicates WITHIN this same call coalesce too. Measured
2026-10-09: with no dedup, one intent had ~20x content-duplicate rows (4,698 metered calls for ~239 unique tasks)
— draining them would have re-bought finished work. A caller that deliberately wants independent repeats of an
identical prompt (e.g. variance sampling) passes dedup=False. The fingerprint is ALWAYS stored so later enqueues
and settle-coalescing can dedup against this row regardless of this flag. The returned ids stay 1:1 with `tasks`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | yes | required | Source parameter intent at src/spendguard/lane_queue.py:358; see complete callable contract. |
| tasks | Any | yes | required | Source parameter tasks at src/spendguard/lane_queue.py:358; see complete callable contract. |
| system | Any | no | None | Source parameter system at src/spendguard/lane_queue.py:358; see complete callable contract. |
| reasoning | Any | no | None | Source parameter reasoning at src/spendguard/lane_queue.py:358; see complete callable contract. |
| priority | Any | no | 0 | Source parameter priority at src/spendguard/lane_queue.py:358; see complete callable contract. |
| max_attempts | Any | no | None | Source parameter max_attempts at src/spendguard/lane_queue.py:358; see complete callable contract. |
| sla_class | Any | no | 'batch' | Source parameter sla_class at src/spendguard/lane_queue.py:358; see complete callable contract. |
| deadline_ts | Any | no | None | Source parameter deadline_ts at src/spendguard/lane_queue.py:358; see complete callable contract. |
| dedup | Any | no | True | Source parameter dedup at src/spendguard/lane_queue.py:358; see complete callable contract. |

Returns: `Any`

<a id="core-bf32bcd9f460e8fe104df07d2d2a1e9b23e87edb5790ea8a49ea53e72b965601"></a>

### `spendguard.lane_queue:manage_queue_tick`

Source: `src/spendguard/lane_queue.py:1420`

Clean every tick, then conditionally run one bounded, load-gated foreground drain.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| execute | Any | no | False | Clean every tick, then conditionally run one bounded, load-gated foreground drain. |
| process | Any | no | True | Clean every tick, then conditionally run one bounded, load-gated foreground drain. |
| stale_days | Any | no | None | Clean every tick, then conditionally run one bounded, load-gated foreground drain. |

Returns: `Any`

<a id="core-394d72c86f387eaae6bd5e2d2cafac6c280e38cb78e220d53b33568fc3cc84b9"></a>

### `spendguard.lane_queue:migrate_from_ledger`

Source: `src/spendguard/lane_queue.py:281`

ONE-TIME move of the queue out of the spend.db money ledger into its own lane_queue.db (see
config.lane_queue_db_path). Copies the NON-TERMINAL rows (pending/leased — the live work) and DROPs the old
`lane_queue` table from the ledger, reclaiming its pages (every .backup snapshot + the B2 backup copy only used
pages, so they shrink immediately; the live ledger file reuses the freed pages over time or on a later VACUUM).

Terminal rows (done/failed) are NOT re-imported — they are the archival backlog purge() discards; migrating 137k
of them would just recreate the bloat. `id` is NOT copied (the new db assigns fresh ids; a migrated leased row's
stale lease simply expires and it is re-leased). Idempotent + re-runnable: once the ledger has no lane_queue table
it is a no-op. Run with the drain DISABLED and AFTER the long-lived writers are on this code (so none recreates the
table in the ledger via _ensure_queue_schema). Returns {moved, dropped, note} or {error}. Never raises.

Takes no parameters.

Returns: `Any`

<a id="core-59ad89c22f339a54b6fa4eae408944fbba3f0e9180c93e6638215082ce8c73af"></a>

### `spendguard.lane_queue:purge`

Source: `src/spendguard/lane_queue.py:1341`

Archive then delete two distinct structural classes: stale live work, then retained terminal history.

Stale work is pending, or leased only when its lease is expired, and is retired when its declared deadline has
passed or its creation age exceeds ``stale_days``.  Terminal rows retain the established ``retain_days`` policy.
Both passes use separate bounded ``BEGIN IMMEDIATE`` chunks and the same durable archive. Returns per-class and
aggregate counts (or a named error); never raises.

Each chunk is a SHORT `BEGIN IMMEDIATE` txn (archive-before-delete preserved per chunk) that releases the write
lock between chunks, so purging a large backlog never holds the ledger-grade lock for minutes nor fetches the
whole match-set into memory — the (state, updated_ts) index makes the per-chunk SELECT a bounded range scan. At
worst a crash between a chunk's archive-append and its delete-commit re-logs that chunk next run (a harmless dup in
an append-only audit); it can never DELETE before its archive copy is fsynced and verified.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| retain_days | Any | no | None | Source parameter retain_days at src/spendguard/lane_queue.py:1341; see complete callable contract. |
| archive_path | Any | no | None | Source parameter archive_path at src/spendguard/lane_queue.py:1341; see complete callable contract. |
| chunk | Any | no | None | Source parameter chunk at src/spendguard/lane_queue.py:1341; see complete callable contract. |
| stale_days | Any | no | None | Source parameter stale_days at src/spendguard/lane_queue.py:1341; see complete callable contract. |

Returns: `Any`

<a id="core-e3f6a9901b2e0b4ba8cd452283f226371fa1c46dad81658632613a578397ad83"></a>

### `spendguard.lane_queue:queue_depth`

Source: `src/spendguard/lane_queue.py:1102`

{pending, leased, done, failed, retired, parked} counts — the 'is anything queued' view (parallel to
dispatch.queue_state). `parked` is the SUBSET of pending currently DEFERRED for capacity (defer_until in the
future) — visible backpressure (Step 4), not a separate state. A spend refusal / ledger LOCK propagates; any
other hiccup returns {} (a status read never crashes a caller).

Takes no parameters.

Returns: `Any`

<a id="core-227ce4e2f1a86c6683cfcafec5ac3eae3fd6fbe34a6170d06407c63b8fbaac95"></a>

### `spendguard.lane_queue:queue_row_status`

Source: `src/spendguard/lane_queue.py:1125`

Disposition for one enqueue handle, falling back to both bounded archive generations.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| row_id | Any | yes | required | Source parameter row_id at src/spendguard/lane_queue.py:1125; see complete callable contract. |
| archive_path | Any | no | None | Source parameter archive_path at src/spendguard/lane_queue.py:1125; see complete callable contract. |

Returns: `Any`

<a id="core-bdd0fa8273740598314d8a7c0f94b5ff083ecd8389e65aeff7319a0bdc3e1a69"></a>

### `spendguard.lane_servability:refresh_lane_servability`

Source: `src/spendguard/lane_servability.py:349`

Observe every configured lane (or one), write its served/rejected sets into the catalog, and fire the loud
shift detector. The $0 entry point a periodic refresh or a transport-error burst calls. With reprobe=True it also
RECOVERY-probes each lane's rejected models (a $0 lane call apiece) so a model that came back self-heals — OFF by
default so the hot failure-trigger path stays cheap; a periodic/manual refresh turns it on. Returns
{lane: {"served", "rejected", "shifted", "shifts", "recovered"}}. Never raises.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| lane | Any | no | None | Source parameter lane at src/spendguard/lane_servability.py:349; see complete callable contract. |
| announce | Any | no | True | Source parameter announce at src/spendguard/lane_servability.py:349; see complete callable contract. |
| reprobe | Any | no | False | Source parameter reprobe at src/spendguard/lane_servability.py:349; see complete callable contract. |

Returns: `Any`

<a id="core-5e97e6bff77bafa5edbe4be73ce29903ce640152bff79463014aa58b2aaca918"></a>

### `spendguard.lane_value:stamp_from_ledger`

Source: `src/spendguard/lane_value.py:61`

Price active lanes' subscription calls at API rates and replace the routed-value cache.

Returns {executor: month_value_usd}, with None when a lane has current-month activity containing an unpriceable
call. This store is deliberately separate from the miners' transcript-value cache. Best-effort; never raises.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| window_days | int | no | _WINDOW_DAYS | Price active lanes' subscription calls at API rates and replace the routed-value cache.<br><br>Returns {executor: month_value_usd}, with None when a lane has current-month activity containing an unpriceable<br>call. This store is deliberately separate from the miners' transcript-value cache. Best-effort; never raises. |

Returns: `dict`

<a id="core-7d2f4e35c79636a1c63722e3fb431709173a3bb901194fed5491c4eb529c275a"></a>

### `spendguard.lane_value:sync`

Source: `src/spendguard/lane_value.py:185`

Push the registered lanes' ledger-derived plan VALUE (billed=False, channel=<lane>) to the server, so the org
dashboard's work-done / unit-economics count them (the client twin of the server's EST_VALUE_CHANNELS). Filters to
THIS connection's owned projects (no cross-attribution); declares a per-channel replace so re-syncs never stack.
Runs inside `saas sync` (hourly via launchd). Best-effort; never raises out.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| dry | Any | no | False | Push the registered lanes' ledger-derived plan VALUE (billed=False, channel=<lane>) to the server, so the org<br>dashboard's work-done / unit-economics count them (the client twin of the server's EST_VALUE_CHANNELS). Filters to<br>THIS connection's owned projects (no cross-attribution); declares a per-channel replace so re-syncs never stack.<br>Runs inside `saas sync` (hourly via launchd). Best-effort; never raises out. |

Returns: `Any`

<a id="core-b15e1d895b15d4e6bb8080223cf5e798a27d3fae56cedd57b391cbd1d7f84f8c"></a>

### `spendguard.lanes:lane_headroom`

Source: `src/spendguard/lanes.py:245`

Read structural reader/data state per enabled lane and persist the routing snapshot.

An absent reader and a reader returning no usable data are distinct unknown states;
neither establishes availability or a provider capability verdict. The hot path reads
only the persisted snapshot and never launches a probe.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| do_fetch | Any | no | True | Source parameter do_fetch at src/spendguard/lanes.py:245; see complete callable contract. |

Returns: `Any`

<a id="core-8711ae3ddefdf7a4350d173765a59eb738e52d33fb78555109372dba51d538f3"></a>

### `spendguard.lanes:lanes_status`

Source: `src/spendguard/lanes.py:136`

One dict per lane: is it enabled by advisor.executor, is its CLI on this host, does a login artifact exist,
and the exact activation step if not. DATA-DRIVEN — one row per lane in lane_registry, NO per-lane code here
(add a lane's row and it appears). Free — no network, no model calls. No static login URL exists to print: each
CLI generates a ONE-TIME OAuth link when you start its login; the row's `login` is the link-generator step.

Takes no parameters.

Returns: `Any`

<a id="core-317bb746f8055d1dc99b6fd2169415945c0c310f3b748914813412bbb923b31c"></a>

### `spendguard.lanes:probe`

Source: `src/spendguard/lanes.py:177`

Definitive activation check: ONE tiny prompt per enabled lane, straight through its CLI ($0 billed —
plan-covered; the only spend is a few plan tokens). Each probe is bounded by `timeout_s` when given — a
reachability ping must NOT wait a lane's full work-timeout (agy's is 300s), so a dead lane fails FAST (in
timeout_s) instead of wedging the whole sweep. None → each lane's own default. Returns per-lane live results;
a fresh probe each call (a point-in-time reachability, not a resumable job — nothing to checkpoint).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| timeout_s | Any | no | None | Source parameter timeout_s at src/spendguard/lanes.py:177; see complete callable contract. |

Returns: `Any`

<a id="core-811b8693fb88d34de4deae4d4b4a7d34e9fbf23935f0bbcfee1ac4fd75c329ea"></a>

### `spendguard.lanes:set_declared_lane_model`

Source: `src/spendguard/lanes.py:386 (lanes.main)`

Declare a validated subscription lane model assignment Extract set-model branch; registered lane/model/priced validation, preserve other tiers on tier update, scalar replacement otherwise; _write_advisor_cfg and served-tier print. Audited CLI invocation: spendguard lanes set-model LANE [TIER] MODEL; governing flags: see command inventory `lanes`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| lane | str | yes | null | Extract set-model branch; registered lane/model/priced validation, preserve other tiers on tier update, scalar replacement otherwise; _write_advisor_cfg and served-tier print. |
| model | str | yes | null | Extract set-model branch; registered lane/model/priced validation, preserve other tiers on tier update, scalar replacement otherwise; _write_advisor_cfg and served-tier print. |
| tier | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract set-model branch; registered lane/model/priced validation, preserve other tiers on tier update, scalar replacement otherwise; _write_advisor_cfg and served-tier print. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-f67b31ebb2ff87950b46bd6abb6b2ce5534113fae77503d7eccd3d78893f69b6"></a>

### `spendguard.ledger_performance_benchmark:run_ledger_performance_benchmark`

Source: `src/spendguard/ledger_performance_benchmark.py`

bench --record measures p50/p95/p99 hot reads and record_event plus data-flow throughput, records provenance and the derived load profile, and commits the result for the current runner_class. bench --check measures the same paths and fails closed against that runner_class only: latency must remain at or below baseline multiplied by latency_multiplier, and throughput at or above baseline divided by throughput_divisor. --iterations controls retained samples, --warmups controls discarded warmups, and --volume supplies the synthetic fallback only when spend_events/calls metadata is empty.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| record | bool | no | False | Measure and commit latency, throughput, provenance, and load-profile evidence for the current runner_class. |
| check | bool | no | False | Measure and fail when a metric breaches its same-runner_class baseline and configured relative tolerance. |
| iterations | int | no | LEDGER_PERFORMANCE_DEFAULT_ITERATIONS | Measured iterations per hot operation after warmup. |
| warmups | int | no | LEDGER_PERFORMANCE_DEFAULT_WARMUPS | Warmup iterations discarded before measurement. |
| volume | int | no | LEDGER_PERFORMANCE_DEFAULT_VOLUME | Synthetic fallback volume used only when spend_events and calls have no retrospective metadata. |

Returns: `int`

<a id="core-a0a020bdbbce41054d34d973da5805386f95fe519d5a0e87d8361b0b2db5fe92"></a>

### `spendguard.ledger_read:ledger_read_metrics`

Source: `src/spendguard/ledger_read.py:219`

Return process-local read attempts, elapsed seconds and managed connections.

Takes no parameters.

Returns: `Any`

<a id="core-9b9f14c10ac3d2ecf6ecdbe4ada57f7f5326618a2756a57a70c7d5b6166d316c"></a>

### `spendguard.ledger_read:read_cc_conversation_cost`

Source: `src/spendguard/ledger_read.py:1035`

Read the ledger cost aggregation for a conversation.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| conv | Any | yes | required | Conversation identifier bound to the cost query. |
| path | Any | no | None | Ledger database path; omission uses the configured ledger. |

Returns: `Any`

<a id="core-4898a48fcf24a81148d55271f972b8d1b144069ef475e3beff388c188bf0215f"></a>

### `spendguard.ledger_read:read_cc_top_conversations`

Source: `src/spendguard/ledger_read.py:1029`

Read bounded top conversation costs from the ledger.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| limit | Any | no | None | Maximum conversations; omission uses the ledger read row cap. |
| path | Any | no | None | Ledger database path; omission uses the configured ledger. |

Returns: `Any`

<a id="core-f7572ae4d0cf3263a4263a25b1433bf400c30ee5648cf3950f9ae8160057701c"></a>

### `spendguard.ledger_read:read_conversation_spend_since`

Source: `src/spendguard/ledger_read.py:835`

Thin in-process name for the existing countable conversation aggregate.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | yes | required | Thin in-process name for the existing countable conversation aggregate. |
| conv | Any | yes | required | Thin in-process name for the existing countable conversation aggregate. |
| deadline_s | Any | no | config.LEDGER_QUERY_DEADLINE_S | Thin in-process name for the existing countable conversation aggregate. |

Returns: `Any`

<a id="core-d3ffae247de77b97c78a30abd46b08799555e210d078955e0f674d8ac2dd3d0b"></a>

### `spendguard.ledger_read:read_filtered_ledger_count`

Source: `src/spendguard/ledger_read.py:844`

Count every matching record without materializing it or applying a row cap.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| table | Any | no | LEDGER_EVENTS_TABLE | Count every matching record without materializing it or applying a row cap. |
| since | Any | no | None | Count every matching record without materializing it or applying a row cap. |
| until | Any | no | None | Count every matching record without materializing it or applying a row cap. |
| where | Any | no | None | Count every matching record without materializing it or applying a row cap. |
| path | Any | no | None | Count every matching record without materializing it or applying a row cap. |

Returns: `Any`

<a id="core-fae328bd5129e10d684c1478adb8482ce57569324c91915aa2019a90f6fceb0f"></a>

### `spendguard.ledger_read:read_ledger_event`

Source: `src/spendguard/ledger_read.py:912`

Read a single ledger event, returning None when absent.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| event_id | Any | yes | required | Exact ledger event identifier to select. |
| path | Any | no | None | Ledger database path; omission uses the configured ledger. |

Returns: `Any`

<a id="core-2aaec9a980a22b3c742f76f114200ed3f1bc7fffa4229dbc846a0dcd9a15239b"></a>

### `spendguard.ledger_read:read_recent_calls`

Source: `src/spendguard/ledger_read.py:917`

Read recent recorded calls ordered newest first with bounded rows.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Inclusive earliest call timestamp. |
| until | Any | no | None | Inclusive latest call timestamp. |
| where | Any | no | None | Equality filters keyed by validated recent-call columns. |
| limit | Any | no | None | Maximum rows; omission uses the ledger read row cap. |
| path | Any | no | None | Ledger database path; omission uses the configured ledger. |

Returns: `Any`

<a id="core-ae84997608ea7521682713af895e129c5dd07123dc9f987873cbf01bf8e5401c"></a>

### `spendguard.ledger_read:read_recorded_call_total`

Source: `src/spendguard/ledger_read.py:811`

Return raw SUM(calls.cost), never countable billed spend or an unknown zero.

Missing/unreadable/locked/corrupt ledgers and missing schema raise
LedgerReadError. Consumers may explicitly catch that and choose zero or None.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| path | Any | no | None | Return raw SUM(calls.cost), never countable billed spend or an unknown zero.<br><br>Missing/unreadable/locked/corrupt ledgers and missing schema raise<br>LedgerReadError. Consumers may explicitly catch that and choose zero or None. |
| time_budget | Any | no | DEFAULT_LEDGER_READ_TIME_BUDGET_S | Return raw SUM(calls.cost), never countable billed spend or an unknown zero.<br><br>Missing/unreadable/locked/corrupt ledgers and missing schema raise<br>LedgerReadError. Consumers may explicitly catch that and choose zero or None. |

Returns: `Any`

<a id="core-ddf77ddf36904d1ca7c1735445f192f7c2f647a660bd13f978c0364b9cff0d48"></a>

### `spendguard.ledger_read:read_spend_aggregates`

Source: `src/spendguard/ledger_read.py:933`

Aggregate spend with exact decimal sums and validated grouping dimensions.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| group_by | Any | no | ('provider', 'day') | Sequence of validated ledger aggregate dimensions. |
| since | Any | no | None | Inclusive earliest event day. |
| until | Any | no | None | Inclusive latest event day. |
| path | Any | no | None | Ledger database path; omission uses the configured ledger. |

Returns: `Any`

<a id="core-b398bf40107dab1cea7744cd16ecc001901f0ea2385d41431cf38cbafa101fe2"></a>

### `spendguard.ledger_read:read_spend_events`

Source: `src/spendguard/ledger_read.py:890`

Read bounded spend events using validated columns and inclusive day bounds.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Inclusive earliest event day. |
| until | Any | no | None | Inclusive latest event day. |
| where | Any | no | None | Equality filters keyed by validated spend-event read columns. |
| category_column | Any | no | None | USD category column whose non-null events are selected. |
| limit | Any | no | None | Maximum rows; omission uses the ledger read row cap. |
| path | Any | no | None | Ledger database path; omission uses the configured ledger. |

Returns: `Any`

<a id="core-b079f527d01361bb8ce7f9e8d40fe51060c0c336bfaf942993a32f2402d051a7"></a>

### `spendguard.ledger_read:read_spend_since`

Source: `src/spendguard/ledger_read.py:835`

Return complete countable workload spend from a bounded read-only snapshot.

Optional project and conversation scopes retain budget.spent_since semantics;
no write-ledger initialization or migration is performed.

CLI DELIBERATE variant restriction: In-process budget aggregate; command consumers use existing ledger-read aggregate operations.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | yes | required | Inclusive earliest workload spend timestamp or day. |
| project | Any | no | None | Optional repository project scope. |
| conv | Any | no | None | Optional conversation scope. |
| deadline_s | Any | no | config.LEDGER_QUERY_DEADLINE_S | Relative aggregate deadline; expiry raises LedgerQueryDeadline. |

Returns: `Any`

<a id="core-5f041357820d1ca3ab2fdc26a8f9d5f21340af1d9f909730e9e78b77593946e3"></a>

### `spendguard.ledger_sync:audit_completeness`

Source: `src/spendguard/ledger_sync.py:95`

Triple-check the batch reconciliation is COMPLETE (regular keys only). Enumerate EVERY provider batch;
the only ones legitimately without usage are genuine zero-cost (0 completed requests). Any batch with
completed requests but no usage is flagged UNACCOUNTED — surfaced, never silently dropped. complete=True
iff there are no unaccounted batches.

Takes no parameters.

Returns: `Any`

<a id="core-9362401b412b1b805c125045e4ee7d7de9c47aaf0ced3ddfadb9def5512f1657"></a>

### `spendguard.ledger_sync:leak_line`

Source: `src/spendguard/ledger_sync.py:694`

One-line batch status for the report (or None if nothing to compare). Distinguishes:
  • a real LEAK — provider truth NOT in the ledger at all (alarm, ungoverned spend);
  • a capture-rate gap — gate didn't record it live, but reconcile backfilled it (accounted, just not captured
    at the source — informational, NOT an alarm);
  • pre-batch-recording history — provider batch before the gate tracked batch (expected; one reconcile absorbs it).
Reporting a capture-rate gap as a LEAK is the bug this fixes: it cried '~$1.9k ungoverned, install the gate' when
every batch since recording began was in fact accounted.
Every SUCCESSFUL computation (including a 'nothing to compare' None — that too is knowledge) persists to
leak_line.json for cached_leak_line() readers; a failed compute is NOT cached (failure isn't knowledge).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Source parameter since at src/spendguard/ledger_sync.py:694; see complete callable contract. |

Returns: `Any`

<a id="core-3b294fad2d72933749669cebe8c7015e385bb34bce9437265355849572e0e1e7"></a>

### `spendguard.ledger_sync:reconcile_into_ledger`

Source: `src/spendguard/ledger_sync.py:256`

Make the LOCAL ledger reflect PROVIDER-billed batch truth, from BOTH sides: (1) true_down() nets the gate's
batch ESTIMATE rows down to billed actuals (estimate bias / failed batches never billed), then (2) the
per-(provider,day) GAP between provider billing and gate-recorded batch is written as attributed rows
(provider > recorded = pre-ledger / ungated / ungoverned). Idempotent (rebuilds both). The gate-recorded
spend stays attributed (project/user). Zero model spend (provider GETs are free). Returns a summary. This is
what makes the ledger correct + the dashboard show the real total, and it rides the existing daily reconcile
cadence — no separate scheduler.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Source parameter since at src/spendguard/ledger_sync.py:256; see complete callable contract. |

Returns: `Any`

<a id="core-37d9901b1f114021d51f5ea71f507bc2b00ac2fd3aaafb41c78d48887210c6ee"></a>

### `spendguard.ledger_sync:sync`

Source: `src/spendguard/ledger_sync.py:722`

Compare provider batch truth with accounted and live-captured local ledger spend.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Inclusive reconciliation start day; omission uses the current UTC month start. |

Returns: `Any`

<a id="core-ea5874a8af3dd6893dfe4cb22bf1e71c6eaaff0c5a8c6229271a54d8a078319d"></a>

### `spendguard.manifest:build_surface_manifest`

Source: `src/spendguard/manifest.py:208`

Eagerly discover, verify, and atomically persist the complete bounded inventory.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| roots | Any | no | None | Source parameter roots at src/spendguard/manifest.py:208; see complete callable contract. |
| settings | Any | no | None | Source parameter settings at src/spendguard/manifest.py:208; see complete callable contract. |
| now | Any | no | None | Source parameter now at src/spendguard/manifest.py:208; see complete callable contract. JSON transport: iso_datetime. |

Returns: `dict`

<a id="core-14ef0906be3a20daa5a0c3ff196863e92439006764906946bdad4298b8fd0b49"></a>

### `spendguard.manifest:read_surface_manifest`

Source: `src/spendguard/manifest.py:278`

Read and validate the persisted manifest used as distribution's SSOT.

Takes no parameters.

Returns: `dict`

<a id="core-35c9492d1c30fb54dd8eef2633765ca4f615093ee823d66cc14259468e608b7b"></a>

### `spendguard.mcp_server:capability_spend_overview_operation`

Source: `src/spendguard/mcp_server.py:380`

The headline for THIS account: REAL $ out the door (subscription base + Claude Code overage + API credits) shown SEPARATELY from est-value (plan-covered Claude Code usage, what it's worth at API rates). The two axes are never summed. $0, read-only. Promote unchanged; read_cc_spend_overview + guard.saved_since/decisions_summary preserving separate real$/estimated-value/savings axes.

Takes no parameters.

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-368fb7d5e641a3b98fa1c329756c2e52007fd0115d365bde59acac295596c715"></a>

### `spendguard.mcp_server:capability_vision_operation`

Source: `src/spendguard/mcp_server.py:279`

Estimate, budget-gate the estimate, and cache one governed image request Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | str | yes | required | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| prompt | str | yes | required | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| images | list[str] {"alternatives": [], "enum": [], "items": {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, "properties": [], "python_annotation": "list[str]", "type": "array"} | yes | required | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| schema | dict \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "object"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "dict \| None", "type": "any"} | no | null | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| system | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| budget_usd | float \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "number"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "float \| None", "type": "any"} | no | null | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| max_out_tokens | int \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "integer"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "int \| None", "type": "any"} | no | null | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| no_cache | bool | no | false | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |
| timeout_s | int \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "integer"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "int \| None", "type": "any"} | no | null | Parameters are exact spendguard_vision schema below; promote entire governed cache/estimate/gate/image-size composition unchanged; adapters.vision alone loses controls. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-55a92f3c43f9f3700470fea7650728885a83cf67950e6999794380ef36917377"></a>

### `spendguard.mcp_server:register_client`

Source: `src/spendguard/mcp_server.py:1176`

Register (or unregister) spendguard as a stdio MCP server in the Claude Code client config, so its tools
are reachable from every repo. Mirrors exactly how symgrep / 7thsense / ccwatch are registered — a top-level
`mcpServers` entry in ~/.claude.json — and both the executable path (via receipt._spendguard_bin, resolved
from the running install, never hardcoded) and the write (via config.update_json: atomic + emacs-style backup
+ never silently clobbering an unparseable file) are borrowed from the code that already does this well.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| remove | Any | no | False | Register (or unregister) spendguard as a stdio MCP server in the Claude Code client config, so its tools<br>are reachable from every repo. Mirrors exactly how symgrep / 7thsense / ccwatch are registered — a top-level<br>`mcpServers` entry in ~/.claude.json — and both the executable path (via receipt._spendguard_bin, resolved<br>from the running install, never hardcoded) and the write (via config.update_json: atomic + emacs-style backup<br>+ never silently clobbering an unparseable file) are borrowed from the code that already does this well. |

Returns: `Any`

<a id="core-3194de0820d507046e1d01e515ae23d15061817e9132824218d14bf082879b07"></a>

### `spendguard.measurement:compare`

Source: `src/spendguard/measurement.py:249`

$0 DRIFT-FLAG: are two readings COMPARABLE — same instrument_id (same judge + sample + rubric + candidates)?
If so, the per-candidate delta; if not, WHY the ruler changed. So a time series is never silently continued
across an instrument change: a 0.88→0.85 that is really 'different judge' is named, not read as a quality drop.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| a_id | Any | yes | required | Source parameter a_id at src/spendguard/measurement.py:249; see complete callable contract. |
| b_id | Any | yes | required | Source parameter b_id at src/spendguard/measurement.py:249; see complete callable contract. |

Returns: `Any`

<a id="core-eee384b60fd4218e6afe19313b8456dcbba0de0be60c485baa328018039dbd31"></a>

### `spendguard.measurement:inspect`

Source: `src/spendguard/measurement.py:238`

A stored reading by id, or None. For a PAST run with no stored receipt, use reconstruct_reading(intent) —
inspect never invents a judge for an unstamped number.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| reading_id | Any | yes | required | Source parameter reading_id at src/spendguard/measurement.py:238; see complete callable contract. |

Returns: `Any`

<a id="core-376c86bbaadf06591fc00bebb0aa553e4823518e4f84c505395c9925d977421e"></a>

### `spendguard.measurement:list_readings`

Source: `src/spendguard/measurement.py:197`

Return newest measurement readings, optionally filtered by intent.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | no | None | Exact intent filter; omission includes all intents. |
| limit | Any | no | 25 | Maximum newest readings returned. |

Returns: `Any`

<a id="core-8eee5362e3659eb54329ef61c17a23f031add8119b5bb0ad3c87a04d390fb4f5"></a>

### `spendguard.measurement:reconstruct_reading`

Source: `src/spendguard/measurement.py:211`

Best-effort reading for a PAST bakeoff of `intent`, rebuilt from the `calls` corpus (caller='bakeoff'). Recovers
candidates, good_rate, N and the date range — but the JUDGE is 'unknown': it was never stamped before receipts
existed, and an unstamped judge is an absence of evidence, NEVER a guessed model. This is the RECONSTRUCT
identity applied to a measurement; going forward, record_reading stamps the real judge so inspect shows it.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | yes | required | Source parameter intent at src/spendguard/measurement.py:211; see complete callable contract. |

Returns: `Any`

<a id="core-f9c8b4c7962886ca1c988ad06d67f39f170a3b39a7ed876530abcb9e94b84248"></a>

### `spendguard.measurement:rerun`

Source: `src/spendguard/measurement.py:306`

Re-run a reading's INSTRUMENT — same judge (pinned), same sample, same rubric — for a COMPARABLE number.
ESTIMATE-FIRST: no budget_usd → returns the estimate only (0 spend); with it, bakeoff refuses over budget before
spending. Records a CHILD reading (parent=reading_id) and reports the delta vs the baseline + whether the
instrument reproduced. same_sample=False re-baselines on a FRESH sample with the same judge (tracks the
population — a NEW instrument, flagged as such by compare). Rerun re-judges → it SPENDS.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| reading_id | Any | yes | required | Source parameter reading_id at src/spendguard/measurement.py:306; see complete callable contract. |
| budget_usd | Any | no | None | Source parameter budget_usd at src/spendguard/measurement.py:306; see complete callable contract. |
| same_sample | Any | no | True | Source parameter same_sample at src/spendguard/measurement.py:306; see complete callable contract. |

Returns: `Any`

<a id="core-5f80f7c6c5c55b65e678da7420f530a6e8908a338c5d88d5f2332c8cb2b561b1"></a>

### `spendguard.metadata_audit:backbone_health`

Source: `src/spendguard/metadata_audit.py:108`

The full report: {ok, cache:{...}, drift:[...], unknown:[...], capabilities:{...}}. ok=False when the cache is
unhealthy OR any measured cap has drifted below its published ceiling — the two states that silently degrade
output_cap. Capability completeness is INFORMATIONAL (an uncovered bleeding-edge model is expected, not a fault).

Takes no parameters.

Returns: `Any`

<a id="core-f7cc8c03d8c88dcc006b3cab9e208c48b5e4a7ccf273e3dd0f7c2a2f512f5bb1"></a>

### `spendguard.migrate_charges:run_cutover`

Source: `src/spendguard/migrate_charges.py:112`

THE clear, runnable migration: rebuild `spend_events` from `charges` under the v6 exact-Decimal
schema, and PROVE the sum is preserved. One command, start to finish — this is what was missing (the
migration existed only as a buried backfill function with no runner and no cutover).

LOSSLESS + CLEAN. Before touching anything it snapshots the WHOLE db file (the real recovery path — the
existing, tested snapshot machinery), then RENAMES any existing `spend_events` aside to
`spend_events_precutover`. Both matter, and both were proven necessary on the live db: the existing table
was STALE/PARTIAL (24,642 charges rows at the old schema while `charges` now holds 45,941), so a rebuild
ONTO it would dedup-append and leave old rows with the old mapping; and it held a few NON-charges rows (2,
from guard/adjust ops) that a charges-only rebuild drops — the file snapshot preserves every one. The
rebuild then reads `charges` (the complete float source of record) and records each row as exact Decimal.

Returns stats incl. the EXACT Decimal residual (Σ charges − Σ spend_events), which must be $0.00 to trust
the cutover. Σ is over EVERY dollar (include_void), so quarantined-impossible rows are proven carried too.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| db_path | Any | no | None | THE clear, runnable migration: rebuild `spend_events` from `charges` under the v6 exact-Decimal<br>schema, and PROVE the sum is preserved. One command, start to finish — this is what was missing (the<br>migration existed only as a buried backfill function with no runner and no cutover).<br><br>LOSSLESS + CLEAN. Before touching anything it snapshots the WHOLE db file (the real recovery path — the<br>existing, tested snapshot machinery), then RENAMES any existing `spend_events` aside to<br>`spend_events_precutover`. Both matter, and both were proven necessary on the live db: the existing table<br>was STALE/PARTIAL (24,642 charges rows at the old schema while `charges` now holds 45,941), so a rebuild<br>ONTO it would dedup-append and leave old rows with the old mapping; and it held a few NON-charges rows (2,<br>from guard/adjust ops) that a charges-only rebuild drops — the file snapshot preserves every one. The<br>rebuild then reads `charges` (the complete float source of record) and records each row as exact Decimal.<br><br>Returns stats incl. the EXACT Decimal residual (Σ charges − Σ spend_events), which must be $0.00 to trust<br>the cutover. Σ is over EVERY dollar (include_void), so quarantined-impossible rows are proven carried too. |

Returns: `Any`

<a id="core-57b94d9c05a84cb88cfe670204b279029c19687a51709c294bd66ce8f55ee9b6"></a>

### `spendguard.model_catalog:all_records`

Source: `src/spendguard/model_catalog.py:230`

The whole {id: record} map (a live reference to the memoised dict — treat as read-only).

Takes no parameters.

Returns: `Any`

<a id="core-24c7aaad1ed7c465b07d18e05496d5a1bff34d7465d95cc7ce4faae9c1ccc076"></a>

### `spendguard.model_preflight:preflight_models`

Source: `src/spendguard/model_preflight.py:87`

Resolve `specs` (each 'vendor:model' or bare 'model') against the live catalog + pricing. $0 per served id;
when correct=True a STALE id costs one tiny (fact-cached) agentic identity call to name its current replacement.
Returns ONE dict per input spec — {spec, provider, model, metered_id, served, corrected, priced, usable, note} —
where served ∈ {served, stale, unchecked, unknown-provider, empty, error}; usable is True only when a
served-or-unchecked, PRICED id (the requested one or its correction) exists. Nothing generated; nothing spent on a
model call. EVERY spec produces a row (empties + errors included), so len(out) == len(specs); a spec that errors
is a LOUD error row, never a lost list or a wedge; only a DELIBERATE stop (gate/budget/dispatch) propagates.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| specs | Any | yes | required | Resolve `specs` (each 'vendor:model' or bare 'model') against the live catalog + pricing. $0 per served id;<br>when correct=True a STALE id costs one tiny (fact-cached) agentic identity call to name its current replacement.<br>Returns ONE dict per input spec — {spec, provider, model, metered_id, served, corrected, priced, usable, note} —<br>where served ∈ {served, stale, unchecked, unknown-provider, empty, error}; usable is True only when a<br>served-or-unchecked, PRICED id (the requested one or its correction) exists. Nothing generated; nothing spent on a<br>model call. EVERY spec produces a row (empties + errors included), so len(out) == len(specs); a spec that errors<br>is a LOUD error row, never a lost list or a wedge; only a DELIBERATE stop (gate/budget/dispatch) propagates. |
| correct | Any | no | True | Resolve `specs` (each 'vendor:model' or bare 'model') against the live catalog + pricing. $0 per served id;<br>when correct=True a STALE id costs one tiny (fact-cached) agentic identity call to name its current replacement.<br>Returns ONE dict per input spec — {spec, provider, model, metered_id, served, corrected, priced, usable, note} —<br>where served ∈ {served, stale, unchecked, unknown-provider, empty, error}; usable is True only when a<br>served-or-unchecked, PRICED id (the requested one or its correction) exists. Nothing generated; nothing spent on a<br>model call. EVERY spec produces a row (empties + errors included), so len(out) == len(specs); a spec that errors<br>is a LOUD error row, never a lost list or a wedge; only a DELIBERATE stop (gate/budget/dispatch) propagates. |

Returns: `Any`

<a id="core-72f469725265bb5d296e87a2e1345c0c2678a53a6e32cb6c254f81fcef964baa"></a>

### `spendguard.models:add_fact`

Source: `src/spendguard/models.py:81`

Store a per-model learning (overrides the family default for `key`). e.g. add_fact('gpt-5-nano',
'reasoning','minimal', source='experiment') or ('gpt-5-nano','quality:phase23','3% match vs gpt-5.5').

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | Any | yes | required | Store a per-model learning (overrides the family default for `key`). e.g. add_fact('gpt-5-nano',<br>'reasoning','minimal', source='experiment') or ('gpt-5-nano','quality:phase23','3% match vs gpt-5.5'). |
| key | Any | yes | required | Store a per-model learning (overrides the family default for `key`). e.g. add_fact('gpt-5-nano',<br>'reasoning','minimal', source='experiment') or ('gpt-5-nano','quality:phase23','3% match vs gpt-5.5'). |
| value | Any | yes | required | Store a per-model learning (overrides the family default for `key`). e.g. add_fact('gpt-5-nano',<br>'reasoning','minimal', source='experiment') or ('gpt-5-nano','quality:phase23','3% match vs gpt-5.5'). |
| confidence | Any | no | 0.9 | Store a per-model learning (overrides the family default for `key`). e.g. add_fact('gpt-5-nano',<br>'reasoning','minimal', source='experiment') or ('gpt-5-nano','quality:phase23','3% match vs gpt-5.5'). |
| source | Any | no | 'manual' | Store a per-model learning (overrides the family default for `key`). e.g. add_fact('gpt-5-nano',<br>'reasoning','minimal', source='experiment') or ('gpt-5-nano','quality:phase23','3% match vs gpt-5.5'). |
| verified | Any | no | True | Store a per-model learning (overrides the family default for `key`). e.g. add_fact('gpt-5-nano',<br>'reasoning','minimal', source='experiment') or ('gpt-5-nano','quality:phase23','3% match vs gpt-5.5'). |

Returns: `Any`

<a id="core-4d61408f35787546e435139f01903fc2671c6fe9703b1e0bc52b8bbdeae01320"></a>

### `spendguard.models:clear_fact`

Source: `src/spendguard/models.py:99`

Delete a per-model learning — for a fact recorded WRONG (e.g. an auto-heal that learned an implausible
max_output from a NON-budget 400) that must stop overriding the family/table value. Returns rows removed; after
this the model falls back to the family default and can re-learn correctly.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | Any | yes | required | Delete a per-model learning — for a fact recorded WRONG (e.g. an auto-heal that learned an implausible<br>max_output from a NON-budget 400) that must stop overriding the family/table value. Returns rows removed; after<br>this the model falls back to the family default and can re-learn correctly. |
| key | Any | yes | required | Delete a per-model learning — for a fact recorded WRONG (e.g. an auto-heal that learned an implausible<br>max_output from a NON-budget 400) that must stop overriding the family/table value. Returns rows removed; after<br>this the model falls back to the family default and can re-learn correctly. |

Returns: `Any`

<a id="core-4bbf09490ba58768da16cf794a6b89dc396aace323e8baf1bc9fabaa60d5da29"></a>

### `spendguard.models:facts`

Source: `src/spendguard/models.py:110`

Read stored model facts with confidence, provenance and verification status.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | Any | yes | required | Exact model identifier whose stored facts are selected. |

Returns: `Any`

<a id="core-5a5acc2d52c9db8d4d48ce750ce5dfd5eacfb20de58d5ba24304427aaf27a5cb"></a>

### `spendguard.models:profile`

Source: `src/spendguard/models.py:129`

Family defaults merged with stored per-model facts (facts win).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | Any | yes | required | Family defaults merged with stored per-model facts (facts win). |

Returns: `Any`

<a id="core-69a895154de3fda61f610db97431ed6ea0032fab0f2ebb9fd31d42cc7620bd0c"></a>

### `spendguard.openai_batch_cli:download_completed_batch_output`

Source: `src/spendguard/openai_batch_cli.py:97 (openai_batch_cli.fetch_batch_output)`

Collect an OpenAI batch into a result file Bind existing _fetch_under_context with OpenAI client under calls.context; preserve nonterminal3/failed2, out/error files, refusal to clobber and force backup. Not callio.collect. Audited CLI invocation: spendguard batch-fetch --batch-id --chain --force --intent --out.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| batch_id | str | yes | null | Bind existing _fetch_under_context with OpenAI client under calls.context; preserve nonterminal3/failed2, out/error files, refusal to clobber and force backup. Not callio.collect. |
| out | str | yes | null | Bind existing _fetch_under_context with OpenAI client under calls.context; preserve nonterminal3/failed2, out/error files, refusal to clobber and force backup. Not callio.collect. |
| force | bool | no | false | Bind existing _fetch_under_context with OpenAI client under calls.context; preserve nonterminal3/failed2, out/error files, refusal to clobber and force backup. Not callio.collect. |
| intent | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Bind existing _fetch_under_context with OpenAI client under calls.context; preserve nonterminal3/failed2, out/error files, refusal to clobber and force backup. Not callio.collect. |
| chain | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Bind existing _fetch_under_context with OpenAI client under calls.context; preserve nonterminal3/failed2, out/error files, refusal to clobber and force backup. Not callio.collect. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-0a36a2f884875b52ca9eb67cab9f0215c513595163c94b6e5d211726d0a18f44"></a>

### `spendguard.otel_ingest:ingest`

Source: `src/spendguard/otel_ingest.py:148`

Record every GenAI span in `doc` as a spend_events row (basis reconstructed / unpriced). IDEMPOTENT on the
span id — re-ingesting the same trace never double-counts. dry_run counts + prices without writing. Returns a
summary: {ingested, skipped, unpriced, cost, spans}.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| doc | Any | yes | required | Record every GenAI span in `doc` as a spend_events row (basis reconstructed / unpriced). IDEMPOTENT on the<br>span id — re-ingesting the same trace never double-counts. dry_run counts + prices without writing. Returns a<br>summary: {ingested, skipped, unpriced, cost, spans}. |
| project | Any | no | None | Record every GenAI span in `doc` as a spend_events row (basis reconstructed / unpriced). IDEMPOTENT on the<br>span id — re-ingesting the same trace never double-counts. dry_run counts + prices without writing. Returns a<br>summary: {ingested, skipped, unpriced, cost, spans}. |
| dry_run | Any | no | False | Record every GenAI span in `doc` as a spend_events row (basis reconstructed / unpriced). IDEMPOTENT on the<br>span id — re-ingesting the same trace never double-counts. dry_run counts + prices without writing. Returns a<br>summary: {ingested, skipped, unpriced, cost, spans}. |

Returns: `Any`

<a id="core-1d58deb95680aa31e9e86f9bb0fb8750c22a4717445e3442e3d78e14dc3dffed"></a>

### `spendguard.overage:current_overage_status`

Source: `src/spendguard/overage.py:97`

Return the observable current overage state and historical real-dollar overage by month.

Cached with a short TTL (plan_admission.overage_status_ttl_s, default 60s) because the underlying probe re-reads
the whole claude-code transcript tree (~10s) and the result changes only slowly. The cache is in-process AND
disk-backed so a fresh CLI process does not re-pay the scan. `force=True` (or `max_age_s=0`) bypasses the cache
and recomputes; `max_age_s` overrides the TTL for a caller that needs a tighter freshness bound. Every return
carries `as_of`/`age_s`/`cached` (see _stamp), so a TTL-served value is self-describing, never a silent skip.
A normal call is strictly cache-only and therefore bounded: a missing or stale cache returns an explicit
unknown verdict. Only ``force=True`` performs the full refresh, keeping transcript and ledger scans out of
doctor/admission hot paths. Test isolation disables disk reuse but does not weaken that bound.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| max_age_s | Any | no | None | Return the observable current overage state and historical real-dollar overage by month.<br><br>Cached with a short TTL (plan_admission.overage_status_ttl_s, default 60s) because the underlying probe re-reads<br>the whole claude-code transcript tree (~10s) and the result changes only slowly. The cache is in-process AND<br>disk-backed so a fresh CLI process does not re-pay the scan. `force=True` (or `max_age_s=0`) bypasses the cache<br>and recomputes; `max_age_s` overrides the TTL for a caller that needs a tighter freshness bound. Every return<br>carries `as_of`/`age_s`/`cached` (see _stamp), so a TTL-served value is self-describing, never a silent skip.<br>A normal call is strictly cache-only and therefore bounded: a missing or stale cache returns an explicit<br>unknown verdict. Only ``force=True`` performs the full refresh, keeping transcript and ledger scans out of<br>doctor/admission hot paths. Test isolation disables disk reuse but does not weaken that bound. |
| force | Any | no | False | Return the observable current overage state and historical real-dollar overage by month.<br><br>Cached with a short TTL (plan_admission.overage_status_ttl_s, default 60s) because the underlying probe re-reads<br>the whole claude-code transcript tree (~10s) and the result changes only slowly. The cache is in-process AND<br>disk-backed so a fresh CLI process does not re-pay the scan. `force=True` (or `max_age_s=0`) bypasses the cache<br>and recomputes; `max_age_s` overrides the TTL for a caller that needs a tighter freshness bound. Every return<br>carries `as_of`/`age_s`/`cached` (see _stamp), so a TTL-served value is self-describing, never a silent skip.<br>A normal call is strictly cache-only and therefore bounded: a missing or stale cache returns an explicit<br>unknown verdict. Only ``force=True`` performs the full refresh, keeping transcript and ledger scans out of<br>doctor/admission hot paths. Test isolation disables disk reuse but does not weaken that bound. |

Returns: `Any`

<a id="core-8b3ec2ebe70fca1529f4b14d6787c943eb5b5588ce61d460616fc51406a59a2e"></a>

### `spendguard.pricing:cross_check_openrouter`

Source: `src/spendguard/pricing.py:399`

Price cross-check against OpenRouter's public models JSON. (rows, matched, total).

TWO JUDGEMENTS, BOTH AGENTIC. Which of their ids names the same model as ours (_same_model_as_ours),
and whether a rate gap means our number needs re-verifying (_judge_price_gaps). Everything mechanical
here is provable: fetching, exact id equality, and comparing two published numbers for inequality.

Neither judgement was agentic before. Identity was `re.sub(r"[^a-z0-9]", "", id.split("/")[-1])`, and
significance was `> 0.10`. Both are wrong in the direction that reads as healthy — a false miss and a
sub-threshold gap both render as "nothing to see".

`scope` bounds which of OUR ids get a paid identity resolution when they do not match exactly; it
defaults to the curated table, the prices we publish and are answerable for. Exact matching still runs
across the whole table for free, and what stayed unresolved is returned rather than hidden.

`run=False` makes this entirely zero-spend: both agentic steps report an estimate and decide nothing.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| run | Any | no | False | Price cross-check against OpenRouter's public models JSON. (rows, matched, total).<br><br>TWO JUDGEMENTS, BOTH AGENTIC. Which of their ids names the same model as ours (_same_model_as_ours),<br>and whether a rate gap means our number needs re-verifying (_judge_price_gaps). Everything mechanical<br>here is provable: fetching, exact id equality, and comparing two published numbers for inequality.<br><br>Neither judgement was agentic before. Identity was `re.sub(r"[^a-z0-9]", "", id.split("/")[-1])`, and<br>significance was `> 0.10`. Both are wrong in the direction that reads as healthy — a false miss and a<br>sub-threshold gap both render as "nothing to see".<br><br>`scope` bounds which of OUR ids get a paid identity resolution when they do not match exactly; it<br>defaults to the curated table, the prices we publish and are answerable for. Exact matching still runs<br>across the whole table for free, and what stayed unresolved is returned rather than hidden.<br><br>`run=False` makes this entirely zero-spend: both agentic steps report an estimate and decide nothing. |
| scope | Any | no | None | Price cross-check against OpenRouter's public models JSON. (rows, matched, total).<br><br>TWO JUDGEMENTS, BOTH AGENTIC. Which of their ids names the same model as ours (_same_model_as_ours),<br>and whether a rate gap means our number needs re-verifying (_judge_price_gaps). Everything mechanical<br>here is provable: fetching, exact id equality, and comparing two published numbers for inequality.<br><br>Neither judgement was agentic before. Identity was `re.sub(r"[^a-z0-9]", "", id.split("/")[-1])`, and<br>significance was `> 0.10`. Both are wrong in the direction that reads as healthy — a false miss and a<br>sub-threshold gap both render as "nothing to see".<br><br>`scope` bounds which of OUR ids get a paid identity resolution when they do not match exactly; it<br>defaults to the curated table, the prices we publish and are answerable for. Exact matching still runs<br>across the whole table for free, and what stayed unresolved is returned rather than hidden.<br><br>`run=False` makes this entirely zero-spend: both agentic steps report an estimate and decide nothing. |

Returns: `Any`

<a id="core-abfa7c8e78dafcb0985002e7811f19bc59f7f0c95b51b43f467791ca3b22a3e9"></a>

### `spendguard.pricing:freshness`

Source: `src/spendguard/pricing.py:510`

(verified_date, days_old, is_stale) — is the price table older than stale_after_days?

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| today | Any | no | None | Source parameter today at src/spendguard/pricing.py:510; see complete callable contract. JSON transport: iso_date. |

Returns: `Any`

<a id="core-78584dd26c132b4fc7a2312e70f401fe8ae75e5ab8d272af8999ee1f060e3763"></a>

### `spendguard.pricing:main`

Source: `src/spendguard/pricing.py:805 (pricing.main)`

Read available model prices and price verification Existing main is already no-arg composition; verify_pricing, full table, worked example and return0. Audited CLI invocation: spendguard pricing (takes no flags; positional/mode grammar in inventory).

Takes no parameters.

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-48684c4a316361c99450b70639660d96ad37238bfa1200244eccf5ff4a6e772d"></a>

### `spendguard.pricing:price`

Source: `src/spendguard/pricing.py:631`

The RAW per-token RATE row for `model` — {in_, out, cached_in, batch_in, batch_out, provider, …} in DOLLARS
PER MILLION TOKENS. This is the rate TABLE, not a cost: do NOT multiply raw token counts by these values (that is
off by 1e6 — a caller printed a $258,249 quote for a $0.26 job doing exactly that). To get DOLLARS, call
`pricing.realtime_cost(model, in_tok, out_tok[, cached_in_tok])` or `pricing.batch_cost(...)` — or
`cost_or_unpriced(...)` for None instead of a raise on an unpriced model. The input-rate key is `in_` (not `in`,
which is a Python keyword). Raises KeyError for an unknown model — never guesses a price.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | str | yes | required | Source parameter model at src/spendguard/pricing.py:631; see complete callable contract. |
| provider | str | no | None | Source parameter provider at src/spendguard/pricing.py:631; see complete callable contract. |

Returns: `dict`

<a id="core-9ec1948e817480a8c1d513055d84debe5e0a1edc51da5cd62d78a8bc7128594d"></a>

### `spendguard.pricing:providers`

Source: `src/spendguard/pricing.py:304`

{provider: [model, ...]} — the configured services and models.

Takes no parameters.

Returns: `Any`

<a id="core-8165f9ee2b860ac9dc79631d403f9fb71214d6d9f7879b53b797bbf7ec0cdf6e"></a>

### `spendguard.pricing:set_price`

Source: `src/spendguard/pricing.py:72`

Author a catalog price with mandatory source and verification provenance.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | Any | yes | required | Author a catalog price with mandatory source and verification provenance. |
| provider | Any | yes | required | Author a catalog price with mandatory source and verification provenance. |
| in_usd | Any | yes | required | Author a catalog price with mandatory source and verification provenance. |
| out_usd | Any | yes | required | Author a catalog price with mandatory source and verification provenance. |
| source | Any | yes | required | Author a catalog price with mandatory source and verification provenance. |
| batch_in | Any | no | None | Author a catalog price with mandatory source and verification provenance. |
| batch_out | Any | no | None | Author a catalog price with mandatory source and verification provenance. |
| cached_in | Any | no | None | Author a catalog price with mandatory source and verification provenance. |
| verified | Any | no | None | Author a catalog price with mandatory source and verification provenance. |

Returns: `Any`

<a id="core-ec4078f7cbb4c682715cba32a22e5e0645c5ac88cd996483a022776ac4c29634"></a>

### `spendguard.prompts:lint`

Source: `src/spendguard/prompts.py:85`

Findings, ranked by measured $ at stake. Each: {intent, kind, detail, est_usd, next}.

The structural findings (boilerplate / context_spread / truncation / model_mix) are $0 — each fires on an
OBJECTIVE fact whose remedy always applies. `batch_savings` is different: many small realtime calls only WASTE
money if they are PACKABLE (independent items of one job), not if they are latency-sensitive interactive turns —
and that is a MEANING judgement, not a threshold. So it is emitted ONLY under `judge_batchable=True`, which asks a
small meta-caged LLM to rule packability per candidate (never a free $0 threshold-verdict). Default off → $0.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | no | None | Findings, ranked by measured $ at stake. Each: {intent, kind, detail, est_usd, next}.<br><br>The structural findings (boilerplate / context_spread / truncation / model_mix) are $0 — each fires on an<br>OBJECTIVE fact whose remedy always applies. `batch_savings` is different: many small realtime calls only WASTE<br>money if they are PACKABLE (independent items of one job), not if they are latency-sensitive interactive turns —<br>and that is a MEANING judgement, not a threshold. So it is emitted ONLY under `judge_batchable=True`, which asks a<br>small meta-caged LLM to rule packability per candidate (never a free $0 threshold-verdict). Default off → $0. |
| since | Any | no | None | Findings, ranked by measured $ at stake. Each: {intent, kind, detail, est_usd, next}.<br><br>The structural findings (boilerplate / context_spread / truncation / model_mix) are $0 — each fires on an<br>OBJECTIVE fact whose remedy always applies. `batch_savings` is different: many small realtime calls only WASTE<br>money if they are PACKABLE (independent items of one job), not if they are latency-sensitive interactive turns —<br>and that is a MEANING judgement, not a threshold. So it is emitted ONLY under `judge_batchable=True`, which asks a<br>small meta-caged LLM to rule packability per candidate (never a free $0 threshold-verdict). Default off → $0. |
| min_calls | Any | no | MIN_CALLS | Findings, ranked by measured $ at stake. Each: {intent, kind, detail, est_usd, next}.<br><br>The structural findings (boilerplate / context_spread / truncation / model_mix) are $0 — each fires on an<br>OBJECTIVE fact whose remedy always applies. `batch_savings` is different: many small realtime calls only WASTE<br>money if they are PACKABLE (independent items of one job), not if they are latency-sensitive interactive turns —<br>and that is a MEANING judgement, not a threshold. So it is emitted ONLY under `judge_batchable=True`, which asks a<br>small meta-caged LLM to rule packability per candidate (never a free $0 threshold-verdict). Default off → $0. |
| judge_batchable | Any | no | False | Findings, ranked by measured $ at stake. Each: {intent, kind, detail, est_usd, next}.<br><br>The structural findings (boilerplate / context_spread / truncation / model_mix) are $0 — each fires on an<br>OBJECTIVE fact whose remedy always applies. `batch_savings` is different: many small realtime calls only WASTE<br>money if they are PACKABLE (independent items of one job), not if they are latency-sensitive interactive turns —<br>and that is a MEANING judgement, not a threshold. So it is emitted ONLY under `judge_batchable=True`, which asks a<br>small meta-caged LLM to rule packability per candidate (never a free $0 threshold-verdict). Default off → $0. |

Returns: `Any`

<a id="core-24c4d357d8ae8c8de17790907e11a4e3ac5c86bc3e8d0be04f8fcff15152ccd3"></a>

### `spendguard.provider_tokens:calibrate`

Source: `src/spendguard/provider_tokens.py:304`

Measure each provider's ratio statistics from the call_io ground-truth, then AGENTICALLY choose each factor.
Reading + counting is $0; the factor CHOICE is one small gated meta call (like `recommend`/`bakeoff`, the user
invoking this is the approval). `store=False` (--dry-run) computes + returns the stats AND a $0 estimate of the
choice call, and makes NO call.

METHOD. For every non-truncated call_io row with a positive recorded in_tok and NO structured-output schema
(schema framing inflates in_tok in a way the text base can't see — excluded so the ratio measures TEXT), take
the row's own text = system+prompt, count it with o200k, and accumulate ratio = in_tok/o200k plus Σin_tok and
Σo200k. The full panel (n, median, aggregate, mean, p10, p90, spread) goes to _choose_factors_agentically,
whose per-provider factor + confidence + rationale are stored. char_ratio (the tiktoken-absent fallback) is the
measured median. If the meta choice is unavailable, providers are LEFT on the neutral proxy (1.0) with a note —
never a mechanical factor dressed as the judged one. Every fetched row is accounted for (used + Σskipped ==
n_rows); every returned choice is accounted for (stored + Σignored == choices). Requires tiktoken (the base).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| store | Any | no | True | Source parameter store at src/spendguard/provider_tokens.py:304; see complete callable contract. |
| model | Any | no | None | Source parameter model at src/spendguard/provider_tokens.py:304; see complete callable contract. |

Returns: `Any`

<a id="core-24e8ac9b590f22a563af6fb66741cd40c5d1200be1a8cbba7068737b2a3d1c33"></a>

### `spendguard.provider_tokens:factor`

Source: `src/spendguard/provider_tokens.py:155`

(multiplier, meta) for a provider's o200k→native token correction — the AGENTICALLY-chosen factor, applied
as-is (the model already weighed which central estimate and how far to trust it). meta carries the model's
confidence + rationale + n for audit. No stored factor (or unknown provider) → (1.0, uncalibrated proxy).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| provider | Any | yes | required | Source parameter provider at src/spendguard/provider_tokens.py:155; see complete callable contract. |

Returns: `Any`

<a id="core-0e359380813e4fec8c4f012774068f7f81c1a53af3fab16bc5131e124bce7c61"></a>

### `spendguard.queue_planner:plan_loop`

Source: `src/spendguard/queue_planner.py:268`

Run tick() on a PERIODIC cadence — the continuous ~1s look-ahead the drain's per-round consult cannot give (a
drain round lasts as long as its batch, not ~1s, so 429 prediction lags the burst). Each cycle: tick() ($0, NO
execution — it DECIDES, never spends or mutates the queue), hand the plan to `on_tick(t)` (the CLI prints it; a
daemon could log/alert/feed a dashboard), then wait `interval_s`. Read-only, so it is SAFE to run continuously
ALONGSIDE the drain — the drain stays the single executor; this is the eyes, not the hands.

Bounded + stoppable + OBSERVABLE: `iterations` is the REQUESTED number of ticks (None = run continuously) and forms
the loop's range; `stop_event` (a threading.Event) stops it PROMPTLY mid-interval. A deliberate stop from tick() OR
from the on_tick observer PROPAGATES — never downgraded to 'keep polling' (refusal-containment). `interval_s`
defaults to config queue.planner_interval_s. Returns {ticks, stopped_by} — how many ticks ran AND why it ended
('iterations' completed | 'stop_event' | 'interrupt' | 'error') — so the termination is never a silent stop.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| interval_s | Any | no | None | Source parameter interval_s at src/spendguard/queue_planner.py:268; see complete callable contract. |
| iterations | Any | no | None | Source parameter iterations at src/spendguard/queue_planner.py:268; see complete callable contract. |
| on_tick | Any | no | None | Source parameter on_tick at src/spendguard/queue_planner.py:268; see complete callable contract. CLI DELIBERATE parameter restriction: A Python callback requires the Python API. MCP DELIBERATE parameter restriction: A Python callback requires the Python API. |
| avg_call_tokens | Any | no | NOMINAL_CALL_TOKENS | Source parameter avg_call_tokens at src/spendguard/queue_planner.py:268; see complete callable contract. |
| stop_event | Any | no | None | Source parameter stop_event at src/spendguard/queue_planner.py:268; see complete callable contract. CLI DELIBERATE parameter restriction: A threading.Event retains process-local identity and requires the Python API. MCP DELIBERATE parameter restriction: A threading.Event retains process-local identity and requires the Python API. |

Returns: `Any`

<a id="core-82fea51d1aebe63a6af678794ebf08e958692b87b88ef9b13e15a0cee4c944c3"></a>

### `spendguard.queue_planner:tick`

Source: `src/spendguard/queue_planner.py:225`

One PLANNER TICK — the periodic, $0, NO-EXECUTION brain the drain loop (and the CLI/MCP) call: read the live
429 forecast + the per-intent realtime backlog, and return the concrete PLAN — which vendors to PACE (approaching
the ceiling), which intents to OFFLOAD to the Batch API and HOW to chunk them, and which are unpriceable this tick.
It DECIDES; the executor submits (estimate-first, durably) — this never spends and never mutates the queue, so it
is safe to call every cycle. `pending_by_intent` defaults to the LIVE lane_queue realtime backlog (pending_counts).

Returns {forecast, offload, skipped, pace, saturated_vendors, batch_eligible, note}.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| avg_call_tokens | Any | no | NOMINAL_CALL_TOKENS | Source parameter avg_call_tokens at src/spendguard/queue_planner.py:225; see complete callable contract. |
| pending_by_intent | Any | no | None | Source parameter pending_by_intent at src/spendguard/queue_planner.py:225; see complete callable contract. |

Returns: `Any`

<a id="core-20358bc26412c54dfc04b25bb456afb9f03bb558af38b001fed1172984a9dcca"></a>

### `spendguard.realized:measure`

Source: `src/spendguard/realized.py:21`

[{intent, adopted_ts, before_rate, after_rate, delta_per_call, after_calls, realized_usd}] —
adoption point = the EARLIEST non-refuted insight recorded for that intent.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | no | None | [{intent, adopted_ts, before_rate, after_rate, delta_per_call, after_calls, realized_usd}] —<br>adoption point = the EARLIEST non-refuted insight recorded for that intent. |
| min_each | Any | no | MIN_EACH | [{intent, adopted_ts, before_rate, after_rate, delta_per_call, after_calls, realized_usd}] —<br>adoption point = the EARLIEST non-refuted insight recorded for that intent. |

Returns: `Any`

<a id="core-52d49445a95739e6e14a81aed9c8e9a01741cb5919500e7f72889cbd15eb5166"></a>

### `spendguard.realized:sync_to_guarded`

Source: `src/spendguard/realized.py:62`

Record POSITIVE realized deltas into the guarded pipeline (source='realized'), incrementally:
only calls not yet counted for an intent contribute, so re-runs never double-record. Returns
{synced_usd, intents}. Regressions (negative delta) are reported by measure() but never 'saved'.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| rows | Any | no | None | Record POSITIVE realized deltas into the guarded pipeline (source='realized'), incrementally:<br>only calls not yet counted for an intent contribute, so re-runs never double-record. Returns<br>{synced_usd, intents}. Regressions (negative delta) are reported by measure() but never 'saved'. |

Returns: `Any`

<a id="core-b65f75899acffccfc70ff1769f463645cbf938d99fdcade7a67a255f766381a3"></a>

### `spendguard.reasoning_equivalence:format_map`

Source: `src/spendguard/reasoning_equivalence.py:370`

`spendguard lanes --reasoning-map`: one line per (lane, model, level) → the SAME-provider metered EQUAL-model
call it falls back to (id + effort), its status and whether it is usable (priced+served), grouped by lane, with
the policy and any NON-equal or UNUSABLE cells called out at the end.

Takes no parameters.

Returns: `Any`

<a id="core-0cbeb93f39616435a0c6cf5f1ece04e7331ded7d461ce5f85094fa7d8cf94e70"></a>

### `spendguard.receipt:render_intent_spend_receipt`

Source: `src/spendguard/receipt.py:1649 (receipt.cli)`

Read receipt allocation by intent Promote typed window inputs; preserve month default, today>week>all, explicit since overrides, budget.by_intent exact countable filter, cost order and JSON/text; receipt.cli upfront lane-value refresh remains existing CLI behavior. Audited CLI invocation: spendguard receipt --by-intent; governing flags: see command inventory `receipt`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| today | bool | no | false | Promote typed window inputs; preserve month default, today>week>all, explicit since overrides, budget.by_intent exact countable filter, cost order and JSON/text; receipt.cli upfront lane-value refresh remains existing CLI behavior. |
| week | bool | no | false | Promote typed window inputs; preserve month default, today>week>all, explicit since overrides, budget.by_intent exact countable filter, cost order and JSON/text; receipt.cli upfront lane-value refresh remains existing CLI behavior. |
| all | bool | no | false | Promote typed window inputs; preserve month default, today>week>all, explicit since overrides, budget.by_intent exact countable filter, cost order and JSON/text; receipt.cli upfront lane-value refresh remains existing CLI behavior. |
| since | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Promote typed window inputs; preserve month default, today>week>all, explicit since overrides, budget.by_intent exact countable filter, cost order and JSON/text; receipt.cli upfront lane-value refresh remains existing CLI behavior. |
| json | bool | no | false | Promote typed window inputs; preserve month default, today>week>all, explicit since overrides, budget.by_intent exact countable filter, cost order and JSON/text; receipt.cli upfront lane-value refresh remains existing CLI behavior. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-59169e5e8683ddee91c773df8afb0828295e3011c3a6084091168cadceddab39"></a>

### `spendguard.receipt:render_line`

Source: `src/spendguard/receipt.py:936`

One compact status-bar line — REAL $ (API + subs + remote + external) then est-value, SEPARATE, never summed.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| t | Optional[dict] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "object"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[dict]", "type": "any"} | no | None | Source parameter t at src/spendguard/receipt.py:936; see complete callable contract. |

Returns: `str`

<a id="core-ff54ec28ea4b2901430b56b4dcc44c02cbf19c0454e73e405abb3dbdc2ea8b8e"></a>

### `spendguard.receipt:render_tree`

Source: `src/spendguard/receipt.py:1191`

The receipt: the two-axis Actual$ | Est-value$ TABLE, then the ORG → TEAM → PROJECT est-value tree (the
attribution, matching the server). `scope_org` limits to one org (None = all orgs).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| scope_org | Any | no | None | Source parameter scope_org at src/spendguard/receipt.py:1191; see complete callable contract. |
| project | Any | no | None | Source parameter project at src/spendguard/receipt.py:1191; see complete callable contract. |

Returns: `str`

<a id="core-e3f1e34ed8684336ce6523bbbb51bedf56966a1b4c4815fb631f2c570be97679"></a>

### `spendguard.receipt:tally`

Source: `src/spendguard/receipt.py:654`

The two axes, returned SEPARATELY and never added together:
  • REAL $ (money out the door) = `api` (per-token API billing, from the gate ledger) + `subscription` (the flat
    plan fee — Anthropic Max, OpenAI Pro, …) + `remote` (billed GPU/vast.ai, cached by resources.sync)
    + `external` (billed MCP/tool + external-API cost, from the ledger's external axis; None when there's none).
  • `est_value` = the value of subscription-covered usage (Claude Code/claude.ai/Codex), NOT billed.
`real_month` = api.month + subscription + remote.month + external.month. `actual` is an alias of `api`.
With `project`, global real-dollar components use its proportional plan-usage share (per window;
subscription uses month). Missing/stale usage refuses allocation rather than inventing a share.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| project | Any | no | None | Source parameter project at src/spendguard/receipt.py:654; see complete callable contract. |
| conv | Any | no | None | Source parameter conv at src/spendguard/receipt.py:654; see complete callable contract. |

Returns: `dict`

<a id="core-6887069946a073c9eb4a0f4d8317bc8f9bc1b7af57a1450b9f5887070e5cf57e"></a>

### `spendguard.reconcile:report`

Source: `src/spendguard/reconcile.py:205`

Print the unified reconciliation across all sources — truth vs captured vs residual per source, account-
anchored, with the under-attribution warning + a CROSS-SOURCE completeness verdict. The single source-of-truth view.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| ptmap | Any | no | None | Print the unified reconciliation across all sources — truth vs captured vs residual per source, account-<br>anchored, with the under-attribution warning + a CROSS-SOURCE completeness verdict. The single source-of-truth view. |
| since | Any | no | None | Print the unified reconciliation across all sources — truth vs captured vs residual per source, account-<br>anchored, with the under-attribution warning + a CROSS-SOURCE completeness verdict. The single source-of-truth view. |

Returns: `Any`

<a id="core-d86705726b02fe02ca859b52bd99415b61a2908135f4cace85eafa89fa457286"></a>

### `spendguard.refresh:refresh_verified_catalog_prices`

Source: `src/spendguard/refresh.py:106 (refresh.main)`

Research and optionally write changed model prices Extract existing _llm_lookup pricing refresh; requires Anthropic key; paid lookup even preview, diff, optional validated _write and status. Default model is source declared, never invented. Audited CLI invocation: spendguard refresh-prices [aliases: refresh] --model --write.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | str | no | {"source": "src/spendguard/refresh.py:main", "expression": "argparse --model default"} | Extract existing _llm_lookup pricing refresh; requires Anthropic key; paid lookup even preview, diff, optional validated _write and status. Default model is source declared, never invented. |
| write | bool | no | false | Extract existing _llm_lookup pricing refresh; requires Anthropic key; paid lookup even preview, diff, optional validated _write and status. Default model is source declared, never invented. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-d2ce5e36f9ae7960f2a528f1b65f014da7c94717295f6237fe96503eb5a8ad63"></a>

### `spendguard.release:converge_deployed_release`

Source: `src/spendguard/release.py:208`

Release completion/scheduler entry: return the full fail-closed fleet attestation.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| check | Any | no | False | Source parameter check at src/spendguard/release.py:208; see complete callable contract. |
| roots | Any | no | None | Source parameter roots at src/spendguard/release.py:208; see complete callable contract. |
| dry_run | Any | no | False | Source parameter dry_run at src/spendguard/release.py:208; see complete callable contract. |
| settings | Any | no | None | Source parameter settings at src/spendguard/release.py:208; see complete callable contract. |

Returns: `Any`

<a id="core-09c7d0490739d52d23e294b6759d3828232c8e629dd413e61923fb2770600181"></a>

### `spendguard.release:release_status`

Source: `src/spendguard/release.py:187`

One dict for the version tool / doctor / CLI: what THIS process serves, what the green pointer names, and
whether they diverge — the $0 signal that answers 'is this server running the latest blessed code?'.

Takes no parameters.

Returns: `Any`

<a id="core-50b08c061170775a558481ddc11e90285ce0a31c03bd9823615140699596769d"></a>

### `spendguard.reliability:lane_fallback_spend`

Source: `src/spendguard/reliability.py:318`

The metered $ this install spent FALLING OVER from each $0 lane — the "$0 savings lost while a lane was down"
view, read straight from the durable calls ledger (the `fell_from` column adapters stamps at the failover site).
Mechanical aggregation only — `SUM(cost) GROUP BY fell_from`, most-costly lane first — no judgement: WHY the lane
was down lives in each row's outcome/provider_error and in lane_health; this answers only "how much did it cost."
`since` is an inclusive ISO-timestamp lower bound on ts (e.g. the week's start). Returns
[{lane, n_calls, metered_usd}]; [] on any error or a ledger not yet carrying the column. $0, read-only.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Source parameter since at src/spendguard/reliability.py:318; see complete callable contract. |

Returns: `Any`

<a id="core-ff68b50581fddd0031141957736e71cdb2940f4f7ebd8bf83d1fea30ea15622a"></a>

### `spendguard.reliability:remediate`

Source: `src/spendguard/reliability.py:288`

Per UNREACHABLE lane/provider in a sweep result, the agentic remediation (cached). [] when all healthy —
so a routine check is $0 when nothing is wrong, and only classifies NEW failures. This is the 'tell me which
lane needs a login' layer: schedule `spendguard reliability --run --remediate` and act on its ACTION lines.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| sweep_result | Any | yes | required | Source parameter sweep_result at src/spendguard/reliability.py:288; see complete callable contract. |
| model | Any | no | None | Source parameter model at src/spendguard/reliability.py:288; see complete callable contract. |

Returns: `Any`

<a id="core-f90a7adf4fa593e11fc57b73b62464c3739031cb0e75acd8f452f3f46840af9f"></a>

### `spendguard.reliability:sweep`

Source: `src/spendguard/reliability.py:166`

The reachability matrix. run=False → estimate only ($0). run=True → probe every lane ($0) + every metered
provider (a tiny gated ping) → {resource: {reachable, executor, cost, reason, latency}}. For a LANE, `reachable`
is "the subscription CLI answered"; for a METERED provider it is stricter — "the RAW METERED KEY authenticated"
(the ping forces metered_only, so a provider whose key is stale reads NOT reachable even when its lane works, and
`reason` says which). That is the field metered_key_ok/health_reds/the doctor key line trust. Each probe is BOUNDED
by `timeout_s`, so ONE hung endpoint fails in ~timeout_s instead of its full work-timeout (the agy lane's 300s
was exactly that wedge). BOTH the lane probes (inside lanes.probe) and the metered pings run CONCURRENTLY, so
the whole sweep is bounded by the slowest single probe — N providers fit the window one does (a serial metered
pass overran the spendguard_health MCP call window).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| run | Any | no | False | Source parameter run at src/spendguard/reliability.py:166; see complete callable contract. |
| timeout_s | Any | no | 20 | Source parameter timeout_s at src/spendguard/reliability.py:166; see complete callable contract. |

Returns: `Any`

<a id="core-a8159467ebed72ac15f9bcaf23122cddaab4f2c46556c6ea33965e9bf7e04a4a"></a>

### `spendguard.remote:onstart_snippet`

Source: `src/spendguard/remote.py:68`

Bash to install + hook spendguard so every python3 on the box is gated from boot. Idempotent + secret-free
(attribution happens at `sync` time, so no key lives on the box). `--from-git` pulls latest main instead of PyPI.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| home | str | no | _DEFAULT_HOME | Source parameter home at src/spendguard/remote.py:68; see complete callable contract. |
| from_git | bool | no | False | Source parameter from_git at src/spendguard/remote.py:68; see complete callable contract. |

Returns: `str`

<a id="core-38bcee45de98175d3c29d27feb70f53d10625d596df0d9d4f8e6e1ed7252fb8e"></a>

### `spendguard.remote:sync`

Source: `src/spendguard/remote.py:134`

Pull the box's realtime_log.jsonl and roll its spend into the LOCAL ledger tagged `project` (so the org sees
it and it survives teardown). Idempotent: keyed by conv_id `remote:<label>` — re-syncing the same box REPLACES
its prior rows, never double-counts. `label` defaults to the ssh target. Returns {rows, usd, project}.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| ssh | str | yes | required | Source parameter ssh at src/spendguard/remote.py:134; see complete callable contract. |
| project | str | yes | required | Source parameter project at src/spendguard/remote.py:134; see complete callable contract. |
| label | str | no | None | Source parameter label at src/spendguard/remote.py:134; see complete callable contract. |
| home | str | no | _DEFAULT_HOME | Source parameter home at src/spendguard/remote.py:134; see complete callable contract. |
| timeout | int | no | 60 | Source parameter timeout at src/spendguard/remote.py:134; see complete callable contract. |
| _run | Any | no | None | Source parameter _run at src/spendguard/remote.py:134; see complete callable contract. CLI DELIBERATE parameter restriction: A subprocess runner callable requires the Python API. MCP DELIBERATE parameter restriction: A subprocess runner callable requires the Python API. |

Returns: `dict`

<a id="core-677160830bd9c596dd627317db085c0efccd88c635bb061acba1da2a512301d4"></a>

### `spendguard.remote:sync_all`

Source: `src/spendguard/remote.py:173`

SWEEP every LIVE vast.ai box: pull its realtime_log into the LOCAL ledger under its attributed project — the
AT-SOURCE path that makes an ephemeral box's realtime spend a complete local record WITHOUT reconstruction
(onstart gates+logs on the box; this pulls it in; sync is idempotent per box). This is the durable answer to
'always report back to local' — schedule it + `resources snapshot`, and sync before every teardown. A box with
no SSH endpoint yet is SKIPPED (not an error); a failed pull is SURFACED (remote.sync never reads it as zero).
Returns per-box results. Injection points (_instances/_sync/_attrib) keep it offline-testable.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| dry | Any | no | False | Source parameter dry at src/spendguard/remote.py:173; see complete callable contract. |
| home | str | no | _DEFAULT_HOME | Source parameter home at src/spendguard/remote.py:173; see complete callable contract. |
| _instances | Any | no | None | Source parameter _instances at src/spendguard/remote.py:173; see complete callable contract. |
| _sync | Any | no | None | Source parameter _sync at src/spendguard/remote.py:173; see complete callable contract. CLI DELIBERATE parameter restriction: A synchronization callback requires the Python API. MCP DELIBERATE parameter restriction: A synchronization callback requires the Python API. |
| _attrib | Any | no | None | Source parameter _attrib at src/spendguard/remote.py:173; see complete callable contract. |

Returns: `Any`

<a id="core-f9d640307ab5be2c4e3a6282f25dbea2ad3209b3f0c718405a0343d0fb4de537"></a>

### `spendguard.remote:verify_remote_enforcement`

Source: `src/spendguard/remote.py:87`

Run `<ssh> python3 -m spendguard doctor` and return (ok, detail). FAIL-CLOSED: any error, timeout, or
uncertainty → ok=False, so callers refuse to launch LLM work on an ungated box. `ssh` is the full prefix,
e.g. 'ssh -i ~/.ssh/vastai_ed25519 -p 12345 root@1.2.3.4'.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| ssh | str | yes | required | Source parameter ssh at src/spendguard/remote.py:87; see complete callable contract. |
| timeout | int | no | 30 | Source parameter timeout at src/spendguard/remote.py:87; see complete callable contract. |
| _run | Any | no | None | Source parameter _run at src/spendguard/remote.py:87; see complete callable contract. CLI DELIBERATE parameter restriction: A subprocess runner callable requires the Python API. MCP DELIBERATE parameter restriction: A subprocess runner callable requires the Python API. |

Returns: `tuple`

<a id="core-69113dbcf5ecda85e1d1250027799361d9b29f6002425490d7cc319208867d97"></a>

### `spendguard.report:produce_periodic_spend_report`

Source: `src/spendguard/report.py:186 (report.main)`

Generate and optionally email the multi-window spend report Extract body after parsing; generate_report + watchdog partial output/exit3 + optional configured email + best-effort SaaS sync. generate_report alone loses behavior. Audited CLI invocation: spendguard report --alert-threshold --email --email-to --max-seconds.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| alert_threshold | float \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "number"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "float \| None", "type": "any"} | no | null | Extract body after parsing; generate_report + watchdog partial output/exit3 + optional configured email + best-effort SaaS sync. generate_report alone loses behavior. |
| email | bool | no | false | Extract body after parsing; generate_report + watchdog partial output/exit3 + optional configured email + best-effort SaaS sync. generate_report alone loses behavior. |
| email_to | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract body after parsing; generate_report + watchdog partial output/exit3 + optional configured email + best-effort SaaS sync. generate_report alone loses behavior. |
| max_seconds | float \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "number"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "float \| None", "type": "any"} | no | null | Extract body after parsing; generate_report + watchdog partial output/exit3 + optional configured email + best-effort SaaS sync. generate_report alone loses behavior. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-1e34c5a25669c09195934019309d0e1b75878c7f70c56bb9ad41934abc466ce5"></a>

### `spendguard.residency_gate:evaluate_posttooluse`

Source: `src/spendguard/residency_gate.py:180`

Return a PostToolUse hook dict: {} (silent) unless a LARGE result in a LONG session is judged delegable, in
which case a systemMessage naming the resident-vs-subagent cost. Never blocks, never raises.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| payload | Any | yes | required | Return a PostToolUse hook dict: {} (silent) unless a LARGE result in a LONG session is judged delegable, in<br>which case a systemMessage naming the resident-vs-subagent cost. Never blocks, never raises. |

Returns: `Any`

<a id="core-2f661b732c2c573a9ac2ff1a3e02f424be9211acc79b7262d958de191bfb6391"></a>

### `spendguard.residency_gate:install_residency_gate`

Source: `src/spendguard/residency_gate.py:256`

Merge or remove ONLY spendguard's PostToolUse residency nudge; back up before every changed write. Mirrors
install_agent_gate — same careful, reversible, single-hook merge.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| settings_path | Any | no | None | Merge or remove ONLY spendguard's PostToolUse residency nudge; back up before every changed write. Mirrors<br>install_agent_gate — same careful, reversible, single-hook merge. |
| uninstall | Any | no | False | Merge or remove ONLY spendguard's PostToolUse residency nudge; back up before every changed write. Mirrors<br>install_agent_gate — same careful, reversible, single-hook merge. |
| executable | Any | no | None | Merge or remove ONLY spendguard's PostToolUse residency nudge; back up before every changed write. Mirrors<br>install_agent_gate — same careful, reversible, single-hook merge. |

Returns: `Any`

<a id="core-4c67532231aba94b45c36de6c23861f2955308f72a749489d49eece450b39c5c"></a>

### `spendguard.resources:account_gpu_total`

Source: `src/spendguard/resources.py:233`

vast.ai account spend since the period — PROXY: invoice charges (prepaid top-ups that fund consumption).
Approximate (top-ups are lumpy and ± a balance buffer; vast.ai exposes no per-instance billing), but it's the
account-level GPU truth for the reconcile gap, mirroring the LLM provider-billing gap.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since_ts | Any | no | None | Source parameter since_ts at src/spendguard/resources.py:233; see complete callable contract. |

Returns: `Any`

<a id="core-c290680b6e493307ac0115c2ea8fafc652fd5314ebed68da80928303efb78c43"></a>

### `spendguard.resources:discover_agentic`

Source: `src/spendguard/resources.py:462`

AGENTIC GPU discovery — a caged LLM reads the GPU-relevant transcript excerpts and extracts real instance
lifecycle + attribution (id/gpu/dph/label/project/launched/destroyed/runtime/confidence). This is what the
brittle regex `_parse_instances` could NOT do: tell a real run from discussion/offers, infer destroy dates,
map labels→projects. Caged + estimate-first (run=False → cost only). record=True → record_recovered the
confidently-real, not-already-known instances so the account reconcile auto-fills. Same agentic pattern as
attribution.classify_items / conv.attribute_usage — the shared 'LLM reads conversations to attribute spend'.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| run | Any | no | False | Source parameter run at src/spendguard/resources.py:462; see complete callable contract. |
| record | Any | no | False | Source parameter record at src/spendguard/resources.py:462; see complete callable contract. |
| max_sessions | Any | no | None | Source parameter max_sessions at src/spendguard/resources.py:462; see complete callable contract. |
| now | Any | no | None | Source parameter now at src/spendguard/resources.py:462; see complete callable contract. |

Returns: `Any`

<a id="core-a47190c88ccbe61edc8819fe4ee971300ed3d3bf61a18c413f66749cfcb691e5"></a>

### `spendguard.resources:discover_instances`

Source: `src/spendguard/resources.py:361`

Mine ALL Claude Code transcripts for vast.ai instance records and reconstruct boxes the snapshot recorder
never captured (destroyed before recording began) — from REAL API data in the conversations, not estimates.
record=True → record_recovered any instance not already live/in-history, so the account reconcile auto-fills
(no hand-written recovery scripts). This makes destroyed-box recovery part of the sync/reconcile PROCESS.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| record | Any | no | False | Source parameter record at src/spendguard/resources.py:361; see complete callable contract. |
| now | Any | no | None | Source parameter now at src/spendguard/resources.py:361; see complete callable contract. |

Returns: `Any`

<a id="core-12184a326aa90fce9a036286fdfa59b249b1bc474e8a546a1304eb8381c5e966"></a>

### `spendguard.resources:record_instance_history`

Source: `src/spendguard/resources.py:98`

Record each LIVE instance's reconstruction state (id → gpu/dph/start/end/label/last_seen), so DESTROYED
instances stay reconstructable per-day. vast.ai exposes NO per-day consumption AND drops destroyed instances
from the API (the invoice/CSV export is top-ups only) — so we must persist their state while they're live.
Idempotent (latest state per id). Runs on every `saas sync` + can be cron'd (`resources snapshot`).

Takes no parameters.

Returns: `Any`

<a id="core-ad5a4cf6937909ba18638ece2a431e169df982255106ce40f0c683566eaaa9bd"></a>

### `spendguard.resources:sync`

Source: `src/spendguard/resources.py:877`

Push the connection's GPU spend per-day, via its key → its org. ORG-SCOPED: every box whose (timing-matched)
project is in this connection's org rides one push, EACH ROW KEEPING ITS OWN project/team (not collapsed onto a
single repo) — the same agentic, per-item attribution as the LLM ledger. Account-anchored: the unrecoverable
remainder is an EXPLICIT `residual` (account total − Σ recorded boxes), surfaced but NEVER dumped on a project/org
(a shared vast.ai account would otherwise leak cross-org). record_instance_history() runs first so live boxes are captured.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| dry | Any | no | False | Source parameter dry at src/spendguard/resources.py:877; see complete callable contract. |

Returns: `Any`

<a id="core-03af0892b06aedf0e05d8fcfba8f9d7bbaa20babb6c3cf36be8b6d6a66681f34"></a>

### `spendguard.resources:vast_gpu_accounting_rows_by_day`

Source: `src/spendguard/resources.py:196`

Per (project, gpu, day) GPU cost — each instance's cost SPLIT across the UTC days it ran (dph × hours that
day), not lumped on today. Attributed by label → project. since_ts defaults to the start of this month.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since_ts | Any | no | None | Source parameter since_ts at src/spendguard/resources.py:196; see complete callable contract. |
| now | Any | no | None | Source parameter now at src/spendguard/resources.py:196; see complete callable contract. |
| label_map | Any | no | None | Source parameter label_map at src/spendguard/resources.py:196; see complete callable contract. |

Returns: `Any`

<a id="core-296fff9bbaf0df12bcdccc3de486768f6f1bd90b7a17da5e3f9075682ef14bd5"></a>

### `spendguard.review:review`

Source: `src/spendguard/review.py:102`

Estimate, or explicitly run, approach-quality review and persist insights.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| run | Any | no | False | Execute the governed review call; false returns its estimate only. |
| top | Any | no | 10 | Number of evidence bundles selected for the review. |

Returns: `Any`

<a id="core-f5e5c64c72b4fa74c73ab7bde0027c748a4efda3ff3f0d75702e7c59abbfbd58"></a>

### `spendguard.route_economics:route_report`

Source: `src/spendguard/route_economics.py:209`

Resolve the LIVE inputs for `intent` at volume `n_tasks` and run route_economics, returning its result plus a
`resolved` block that NAMES every input + its basis (so the number is auditable and 'estimating' is visible).
Resolution, each honest about where it came from: the candidate LANE (the converged economics binding; `lane`
forces one), the BATCH model (`batch_model`, else config advisor.batch_model), the per-task token sizes (in_tok/
out_tok, else out estimated via expected_output.expect(sig=intent) and in defaulted to out), and the reserve
fraction (config advisor.route_reserve_frac, default 0.2). $0 — no call is made, only measured state is read.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | yes | required | Resolve the LIVE inputs for `intent` at volume `n_tasks` and run route_economics, returning its result plus a<br>`resolved` block that NAMES every input + its basis (so the number is auditable and 'estimating' is visible).<br>Resolution, each honest about where it came from: the candidate LANE (the converged economics binding; `lane`<br>forces one), the BATCH model (`batch_model`, else config advisor.batch_model), the per-task token sizes (in_tok/<br>out_tok, else out estimated via expected_output.expect(sig=intent) and in defaulted to out), and the reserve<br>fraction (config advisor.route_reserve_frac, default 0.2). $0 — no call is made, only measured state is read. |
| n_tasks | Any | yes | required | Resolve the LIVE inputs for `intent` at volume `n_tasks` and run route_economics, returning its result plus a<br>`resolved` block that NAMES every input + its basis (so the number is auditable and 'estimating' is visible).<br>Resolution, each honest about where it came from: the candidate LANE (the converged economics binding; `lane`<br>forces one), the BATCH model (`batch_model`, else config advisor.batch_model), the per-task token sizes (in_tok/<br>out_tok, else out estimated via expected_output.expect(sig=intent) and in defaulted to out), and the reserve<br>fraction (config advisor.route_reserve_frac, default 0.2). $0 — no call is made, only measured state is read. |
| in_tok | Any | no | None | Resolve the LIVE inputs for `intent` at volume `n_tasks` and run route_economics, returning its result plus a<br>`resolved` block that NAMES every input + its basis (so the number is auditable and 'estimating' is visible).<br>Resolution, each honest about where it came from: the candidate LANE (the converged economics binding; `lane`<br>forces one), the BATCH model (`batch_model`, else config advisor.batch_model), the per-task token sizes (in_tok/<br>out_tok, else out estimated via expected_output.expect(sig=intent) and in defaulted to out), and the reserve<br>fraction (config advisor.route_reserve_frac, default 0.2). $0 — no call is made, only measured state is read. |
| out_tok | Any | no | None | Resolve the LIVE inputs for `intent` at volume `n_tasks` and run route_economics, returning its result plus a<br>`resolved` block that NAMES every input + its basis (so the number is auditable and 'estimating' is visible).<br>Resolution, each honest about where it came from: the candidate LANE (the converged economics binding; `lane`<br>forces one), the BATCH model (`batch_model`, else config advisor.batch_model), the per-task token sizes (in_tok/<br>out_tok, else out estimated via expected_output.expect(sig=intent) and in defaulted to out), and the reserve<br>fraction (config advisor.route_reserve_frac, default 0.2). $0 — no call is made, only measured state is read. |
| lane | Any | no | None | Resolve the LIVE inputs for `intent` at volume `n_tasks` and run route_economics, returning its result plus a<br>`resolved` block that NAMES every input + its basis (so the number is auditable and 'estimating' is visible).<br>Resolution, each honest about where it came from: the candidate LANE (the converged economics binding; `lane`<br>forces one), the BATCH model (`batch_model`, else config advisor.batch_model), the per-task token sizes (in_tok/<br>out_tok, else out estimated via expected_output.expect(sig=intent) and in defaulted to out), and the reserve<br>fraction (config advisor.route_reserve_frac, default 0.2). $0 — no call is made, only measured state is read. |
| batch_model | Any | no | None | Resolve the LIVE inputs for `intent` at volume `n_tasks` and run route_economics, returning its result plus a<br>`resolved` block that NAMES every input + its basis (so the number is auditable and 'estimating' is visible).<br>Resolution, each honest about where it came from: the candidate LANE (the converged economics binding; `lane`<br>forces one), the BATCH model (`batch_model`, else config advisor.batch_model), the per-task token sizes (in_tok/<br>out_tok, else out estimated via expected_output.expect(sig=intent) and in defaulted to out), and the reserve<br>fraction (config advisor.route_reserve_frac, default 0.2). $0 — no call is made, only measured state is read. |
| reserve_frac | Any | no | None | Resolve the LIVE inputs for `intent` at volume `n_tasks` and run route_economics, returning its result plus a<br>`resolved` block that NAMES every input + its basis (so the number is auditable and 'estimating' is visible).<br>Resolution, each honest about where it came from: the candidate LANE (the converged economics binding; `lane`<br>forces one), the BATCH model (`batch_model`, else config advisor.batch_model), the per-task token sizes (in_tok/<br>out_tok, else out estimated via expected_output.expect(sig=intent) and in defaulted to out), and the reserve<br>fraction (config advisor.route_reserve_frac, default 0.2). $0 — no call is made, only measured state is read. |
| now | Any | no | None | Resolve the LIVE inputs for `intent` at volume `n_tasks` and run route_economics, returning its result plus a<br>`resolved` block that NAMES every input + its basis (so the number is auditable and 'estimating' is visible).<br>Resolution, each honest about where it came from: the candidate LANE (the converged economics binding; `lane`<br>forces one), the BATCH model (`batch_model`, else config advisor.batch_model), the per-task token sizes (in_tok/<br>out_tok, else out estimated via expected_output.expect(sig=intent) and in defaulted to out), and the reserve<br>fraction (config advisor.route_reserve_frac, default 0.2). $0 — no call is made, only measured state is read. |

Returns: `Any`

<a id="core-0e7e2a8858fc7f6a5f9755a4c5a18b9ee04dbcf7ecda5a144d3f354335cd46bb"></a>

### `spendguard.route_horizon:horizon_report`

Source: `src/spendguard/route_horizon.py:215`

Resolve the LIVE horizon inputs and run plan_horizon, returning its result plus a `resolved` block naming every
input + its basis (auditable, 'estimating' visible) — the horizon twin of route_economics.route_report. `groups`
is [{intent, n, in_tok?, out_tok?, urgency?}]; a missing out_tok is estimated per-intent via expected_output, a
missing in_tok defaults to out_tok. $0 — only measured state is read, no call is made.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| groups | Any | yes | required | Resolve the LIVE horizon inputs and run plan_horizon, returning its result plus a `resolved` block naming every<br>input + its basis (auditable, 'estimating' visible) — the horizon twin of route_economics.route_report. `groups`<br>is [{intent, n, in_tok?, out_tok?, urgency?}]; a missing out_tok is estimated per-intent via expected_output, a<br>missing in_tok defaults to out_tok. $0 — only measured state is read, no call is made. |
| lane | Any | no | None | Resolve the LIVE horizon inputs and run plan_horizon, returning its result plus a `resolved` block naming every<br>input + its basis (auditable, 'estimating' visible) — the horizon twin of route_economics.route_report. `groups`<br>is [{intent, n, in_tok?, out_tok?, urgency?}]; a missing out_tok is estimated per-intent via expected_output, a<br>missing in_tok defaults to out_tok. $0 — only measured state is read, no call is made. |
| batch_model | Any | no | None | Resolve the LIVE horizon inputs and run plan_horizon, returning its result plus a `resolved` block naming every<br>input + its basis (auditable, 'estimating' visible) — the horizon twin of route_economics.route_report. `groups`<br>is [{intent, n, in_tok?, out_tok?, urgency?}]; a missing out_tok is estimated per-intent via expected_output, a<br>missing in_tok defaults to out_tok. $0 — only measured state is read, no call is made. |
| realtime_model | Any | no | None | Resolve the LIVE horizon inputs and run plan_horizon, returning its result plus a `resolved` block naming every<br>input + its basis (auditable, 'estimating' visible) — the horizon twin of route_economics.route_report. `groups`<br>is [{intent, n, in_tok?, out_tok?, urgency?}]; a missing out_tok is estimated per-intent via expected_output, a<br>missing in_tok defaults to out_tok. $0 — only measured state is read, no call is made. |
| reserve_frac | Any | no | None | Resolve the LIVE horizon inputs and run plan_horizon, returning its result plus a `resolved` block naming every<br>input + its basis (auditable, 'estimating' visible) — the horizon twin of route_economics.route_report. `groups`<br>is [{intent, n, in_tok?, out_tok?, urgency?}]; a missing out_tok is estimated per-intent via expected_output, a<br>missing in_tok defaults to out_tok. $0 — only measured state is read, no call is made. |
| now | Any | no | None | Resolve the LIVE horizon inputs and run plan_horizon, returning its result plus a `resolved` block naming every<br>input + its basis (auditable, 'estimating' visible) — the horizon twin of route_economics.route_report. `groups`<br>is [{intent, n, in_tok?, out_tok?, urgency?}]; a missing out_tok is estimated per-intent via expected_output, a<br>missing in_tok defaults to out_tok. $0 — only measured state is read, no call is made. |

Returns: `Any`

<a id="core-020d1e50cc35858a5c3cba19cf1ab0292a24d26d429ddeffa9b28108c3edd231"></a>

### `spendguard.route_realized:route_realized_report`

Source: `src/spendguard/route_realized.py:10`

Report realized routing history without creating an absent calls database.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Any | yes | required | Restrict history to this intent when supplied. |
| since | Any | no | None | Earliest routing-history timestamp. |
| as_of | Any | no | None | Routing-history upper observation bound. |
| limit | Any | no | 20 | Maximum routing-history rows returned. |

Returns: `Any`

<a id="core-2413114d0e799845d40365402eaa4fb76162d262959d39e4dec529a3a2c2e33f"></a>

### `spendguard.saas:crosscheck`

Source: `src/spendguard/saas.py:1039`

Cross-check the LOCAL ACTUAL-$ ledger against the SERVER, row by row, via the per-row uid. GET /v1/ledger (this
key's scope) and diff vs locally-computed rows → matched · value-drift · local-only (pushed-but-missing or never
pushed) · server-only (stale, should be pruned). Scoped to the ACTUAL-$ axis (batch · realtime · GPU); est-VALUE
rows (claude-code/claude-ai, billed=false) are excluded — they're reconciled by the chat loop, not here, so they'd
otherwise read as false server_only. The trust layer over the sync. Free (no spend).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Source parameter since at src/spendguard/saas.py:1039; see complete callable contract. |

Returns: `Any`

<a id="core-508d17187b4e4d1c203f94257048892156621c93ec2586b5c0564e94273ec8ce"></a>

### `spendguard.saas:link`

Source: `src/spendguard/saas.py:280`

Device-link this install to the org: start a link with the org key, a teammate approves in the browser
(Clerk sign-in), then we write their VERIFIED email as the contributor — no hand-editing config. Free, no spend.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| open_browser | Any | no | True | Source parameter open_browser at src/spendguard/saas.py:280; see complete callable contract. |
| timeout | Any | no | 900 | Source parameter timeout at src/spendguard/saas.py:280; see complete callable contract. |

Returns: `Any`

<a id="core-3820509460b1850692de02af2f928a7e1a7b102c0c33a220214707f09ed06e8e"></a>

### `spendguard.saas:ping`

Source: `src/spendguard/saas.py:149`

Liveness check against the server (GET /v1/health).

Takes no parameters.

Returns: `Any`

<a id="core-246135974cd517e9fe3c037923355c436032d895f926dc6bd75e517214ad7070"></a>

### `spendguard.saas:pull_insights`

Source: `src/spendguard/saas.py:747`

Pull pooled (scrubbed) learnings as LOW-TRUST priors needing local corroboration.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| scope | Any | no | 'team' | Source parameter scope at src/spendguard/saas.py:747; see complete callable contract. |

Returns: `Any`

<a id="core-005d079603e4e8649215dd120f839ba1193128e6938b32904d862e36d0aaa6bc"></a>

### `spendguard.saas:push_rollup`

Source: `src/spendguard/saas.py:523`

Push this machine's per-day spend roll-up + GUARDED cumulants (NOT per-call, NOT prompts), stamped with the
contributor so the server can roll up per user → team → org. Honors visibility: no-op note if private.
dry=True returns the payload without sending (offline-testable).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Source parameter since at src/spendguard/saas.py:523; see complete callable contract. |
| dry | Any | no | False | Source parameter dry at src/spendguard/saas.py:523; see complete callable contract. |

Returns: `Any`

<a id="core-bced83578c706735ba276bb332cf29b745bc1d9736ada143b7255df6a200a2ba"></a>

### `spendguard.saas:push_workdone`

Source: `src/spendguard/saas.py:611`

Push the WORK-DONE roll-up — git commit subjects + LLM batch-intent counts per period·project — so the
dashboard reads "spent $X, here's what got done." Tier-1: deterministic + FREE (no diffs, no prompts). Honors
visibility (private → no-op) and the connection's project filter. Monthly periods by default, to match the
dashboard's current-month view. dry=True returns the payload without sending (offline-testable).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Push the WORK-DONE roll-up — git commit subjects + LLM batch-intent counts per period·project — so the<br>dashboard reads "spent $X, here's what got done." Tier-1: deterministic + FREE (no diffs, no prompts). Honors<br>visibility (private → no-op) and the connection's project filter. Monthly periods by default, to match the<br>dashboard's current-month view. dry=True returns the payload without sending (offline-testable). |
| by | Any | no | 'month' | Push the WORK-DONE roll-up — git commit subjects + LLM batch-intent counts per period·project — so the<br>dashboard reads "spent $X, here's what got done." Tier-1: deterministic + FREE (no diffs, no prompts). Honors<br>visibility (private → no-op) and the connection's project filter. Monthly periods by default, to match the<br>dashboard's current-month view. dry=True returns the payload without sending (offline-testable). |
| dry | Any | no | False | Push the WORK-DONE roll-up — git commit subjects + LLM batch-intent counts per period·project — so the<br>dashboard reads "spent $X, here's what got done." Tier-1: deterministic + FREE (no diffs, no prompts). Honors<br>visibility (private → no-op) and the connection's project filter. Monthly periods by default, to match the<br>dashboard's current-month view. dry=True returns the payload without sending (offline-testable). |

Returns: `Any`

<a id="core-521ae65689bc498f3deb3695333f42cce4c52af7003a21da6a43b9b78c160300"></a>

### `spendguard.saas:ready`

Source: `src/spendguard/saas.py:58`

(ok, reason). ok only if enabled AND a url is set AND a key is set — i.e. we COULD talk to a server.

Takes no parameters.

Returns: `Any`

<a id="core-0311663dc7d7278026a60d4743b0a03e4e6c33c1fe868321697ef15d7def5473"></a>

### `spendguard.saas:run_commands`

Source: `src/spendguard/saas.py:816`

Drain the server's command queue and run each LOCALLY (the data + context live here), then report a
SCRUBBED result. FREE today: reconcile (provider-vs-local leak) + deterministic re-tag. The LLM-residual
re-tag is gated/estimate-first (tag.estimate_llm_retag) and never auto-runs here. `attribute` = an ORG REQUEST
to run the chat-attribution loop: adopt the org taxonomy + run IF the member already consented (chat.enabled);
otherwise record the request as awaiting consent (never force-enables a member's personal-session adapter).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Source parameter since at src/spendguard/saas.py:816; see complete callable contract. |

Returns: `Any`

<a id="core-b674645088ae831f263e5b90490ae72bb001fba25884b0b8eda7b69c0e6991c7"></a>

### `spendguard.saas:sync`

Source: `src/spendguard/saas.py:892`

Push spend roll-up + insights + work-done. With if_due=True (cron/report), no-op unless the interval has
elapsed. Always safe: not-connected / private / not-due all return a note instead of raising.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| if_due | Any | no | False | Source parameter if_due at src/spendguard/saas.py:892; see complete callable contract. |
| since | Any | no | None | Source parameter since at src/spendguard/saas.py:892; see complete callable contract. |

Returns: `Any`

<a id="core-3dd568a724d46665511eefb11f79f5b9a484b8c8ee44c07f77796b85d4c450c4"></a>

### `spendguard.scan:collect`

Source: `src/spendguard/scan.py:22`

{source display name: {sessions, projects, models, days, total_usd, error}} — via the transcript PORT
(`sources.transcript_sources`), so a tool we've never heard of shows up here the moment its adapter is
installed. Never raises for a missing/broken source.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| days | Any | no | None | {source display name: {sessions, projects, models, days, total_usd, error}} — via the transcript PORT<br>(`sources.transcript_sources`), so a tool we've never heard of shows up here the moment its adapter is<br>installed. Never raises for a missing/broken source. |

Returns: `Any`

<a id="core-c6be327575f65cb0a2a40e21dbbcbda731ee5bb778b5b0de695f39b50fd418d5"></a>

### `spendguard.schedule:install_schedule`

Source: `src/spendguard/schedule.py:24`

Install or remove the current platform scheduler integration.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| interval | Any | no | 'hourly' | Scheduler interval forwarded to the platform-specific installer. |
| remove | Any | no | False | Remove the installed schedule instead of installing it. |

Returns: `Any`

<a id="core-ba29c5f8d123dbbd6c6c5601e126769e181657f82dccba9b0d8e836673ac0ab7"></a>

### `spendguard.semcache:dedup_jsonl`

Source: `src/spendguard/semcache.py:257`

Collapse a batch jsonl: drop within-batch duplicate prompts AND prompts already in the persistent
cache (already processed in a prior run/retry — the real saver). Writes the unique requests to
out_path. NOTE: on FRESH unique-prompt workloads this is ~0%; its win is re-runs/retries/overlap.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| input_path | Any | yes | required | Collapse a batch jsonl: drop within-batch duplicate prompts AND prompts already in the persistent<br>cache (already processed in a prior run/retry — the real saver). Writes the unique requests to<br>out_path. NOTE: on FRESH unique-prompt workloads this is ~0%; its win is re-runs/retries/overlap. |
| out_path | Any | yes | required | Collapse a batch jsonl: drop within-batch duplicate prompts AND prompts already in the persistent<br>cache (already processed in a prior run/retry — the real saver). Writes the unique requests to<br>out_path. NOTE: on FRESH unique-prompt workloads this is ~0%; its win is re-runs/retries/overlap. |
| model | Any | no | _SCOPE_ANY | Collapse a batch jsonl: drop within-batch duplicate prompts AND prompts already in the persistent<br>cache (already processed in a prior run/retry — the real saver). Writes the unique requests to<br>out_path. NOTE: on FRESH unique-prompt workloads this is ~0%; its win is re-runs/retries/overlap. |
| map_path | Any | no | None | Collapse a batch jsonl: drop within-batch duplicate prompts AND prompts already in the persistent<br>cache (already processed in a prior run/retry — the real saver). Writes the unique requests to<br>out_path. NOTE: on FRESH unique-prompt workloads this is ~0%; its win is re-runs/retries/overlap. |

Returns: `Any`

<a id="core-c2cc0f3850836b9329b684185701435dafc85cd9d79f296e5dc703cfdaeb81a7"></a>

### `spendguard.semcache:populate_jsonl`

Source: `src/spendguard/semcache.py:313`

After a batch completes, store prompt→output so a future dedup skips those items (free re-runs).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| input_path | Any | yes | required | After a batch completes, store prompt→output so a future dedup skips those items (free re-runs). |
| results_path | Any | yes | required | After a batch completes, store prompt→output so a future dedup skips those items (free re-runs). |
| model | Any | no | _SCOPE_ANY | After a batch completes, store prompt→output so a future dedup skips those items (free re-runs). |

Returns: `Any`

<a id="core-b46715adf6600b630b45758b2d16ee628268430200b603a911590fa7ac30b2f4"></a>

### `spendguard.semcache:stats`

Source: `src/spendguard/semcache.py:220`

Return semantic-cache entry and embedding counts with process cache statistics.

Takes no parameters.

Returns: `Any`

<a id="core-47d7e6b4683dd01ad67105d6a7b01f03b6584df0cf64eb6790d0b8b317fef27a"></a>

### `spendguard.setup:coverage`

Source: `src/spendguard/setup.py:539`

Show, across EVERY python on this machine (you use 3.11/3.14/…), which can make LLM calls and which
are actually GATED. The gate is per-interpreter, so this is how you confirm nothing is silently ungated.
`spendguard gate-coverage [interp_or_venv ...]` (distinct from `coverage`, which reports per-VENV realtime capture).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| extra | Any | no | None | Show, across EVERY python on this machine (you use 3.11/3.14/…), which can make LLM calls and which<br>are actually GATED. The gate is per-interpreter, so this is how you confirm nothing is silently ungated.<br>`spendguard gate-coverage [interp_or_venv ...]` (distinct from `coverage`, which reports per-VENV realtime capture). |

Returns: `Any`

<a id="core-4169f7db5fb119327ad3df05d32a361253df4b86c9bf9aabc0c9ebd499b261a6"></a>

### `spendguard.setup:install_hook`

Source: `src/spendguard/setup.py:731`

Gate every process in another interpreter:
  --venv <path>            pip-install spendguard + a sitecustomize hook (clean venv).
  --user [--python <interp>]  write a PATH-INJECTING usercustomize into that interpreter's user site —
                           NO pip, so it works on PEP668 'externally-managed' pythons (Homebrew/system).
Closes the system-python bypass. `--uninstall` removes the hook.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| venv | Any | no | None | Gate every process in another interpreter:<br>  --venv <path>            pip-install spendguard + a sitecustomize hook (clean venv).<br>  --user [--python <interp>]  write a PATH-INJECTING usercustomize into that interpreter's user site —<br>                           NO pip, so it works on PEP668 'externally-managed' pythons (Homebrew/system).<br>Closes the system-python bypass. `--uninstall` removes the hook. |
| uninstall | Any | no | False | Gate every process in another interpreter:<br>  --venv <path>            pip-install spendguard + a sitecustomize hook (clean venv).<br>  --user [--python <interp>]  write a PATH-INJECTING usercustomize into that interpreter's user site —<br>                           NO pip, so it works on PEP668 'externally-managed' pythons (Homebrew/system).<br>Closes the system-python bypass. `--uninstall` removes the hook. |
| install_pkg | Any | no | True | Gate every process in another interpreter:<br>  --venv <path>            pip-install spendguard + a sitecustomize hook (clean venv).<br>  --user [--python <interp>]  write a PATH-INJECTING usercustomize into that interpreter's user site —<br>                           NO pip, so it works on PEP668 'externally-managed' pythons (Homebrew/system).<br>Closes the system-python bypass. `--uninstall` removes the hook. |
| user | Any | no | False | Gate every process in another interpreter:<br>  --venv <path>            pip-install spendguard + a sitecustomize hook (clean venv).<br>  --user [--python <interp>]  write a PATH-INJECTING usercustomize into that interpreter's user site —<br>                           NO pip, so it works on PEP668 'externally-managed' pythons (Homebrew/system).<br>Closes the system-python bypass. `--uninstall` removes the hook. |
| python | Any | no | None | Gate every process in another interpreter:<br>  --venv <path>            pip-install spendguard + a sitecustomize hook (clean venv).<br>  --user [--python <interp>]  write a PATH-INJECTING usercustomize into that interpreter's user site —<br>                           NO pip, so it works on PEP668 'externally-managed' pythons (Homebrew/system).<br>Closes the system-python bypass. `--uninstall` removes the hook. |

Returns: `Any`

<a id="core-3a1ef21e6048434a64600f0fd3e8207b91727ecfcc61cc708fc43a957ad4dbb3"></a>

### `spendguard.setup:install_rule`

Source: `src/spendguard/setup.py:352`

Write the spendguard usage rule into the AI-assistant rule files so EVERY assistant conversation in that
project auto-wires spendguard into whatever it builds — CLAUDE.md (Claude Code), AGENTS.md (Codex + the
cross-tool standard), and .cursorrules (Cursor). `--project <dir>` (default: cwd) writes all three; `--global`
(~/.claude) writes the one file with a standard global home, CLAUDE.md. Idempotent: replaces the marked block
if present, else appends (never clobbering the user's own content; the prior file is backed up first). Re-run
after `spendguard` upgrades.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| target | Any | no | None | Write the spendguard usage rule into the AI-assistant rule files so EVERY assistant conversation in that<br>project auto-wires spendguard into whatever it builds — CLAUDE.md (Claude Code), AGENTS.md (Codex + the<br>cross-tool standard), and .cursorrules (Cursor). `--project <dir>` (default: cwd) writes all three; `--global`<br>(~/.claude) writes the one file with a standard global home, CLAUDE.md. Idempotent: replaces the marked block<br>if present, else appends (never clobbering the user's own content; the prior file is backed up first). Re-run<br>after `spendguard` upgrades. |
| glob_ | Any | no | False | Write the spendguard usage rule into the AI-assistant rule files so EVERY assistant conversation in that<br>project auto-wires spendguard into whatever it builds — CLAUDE.md (Claude Code), AGENTS.md (Codex + the<br>cross-tool standard), and .cursorrules (Cursor). `--project <dir>` (default: cwd) writes all three; `--global`<br>(~/.claude) writes the one file with a standard global home, CLAUDE.md. Idempotent: replaces the marked block<br>if present, else appends (never clobbering the user's own content; the prior file is backed up first). Re-run<br>after `spendguard` upgrades. |

Returns: `Any`

<a id="core-3a242780fcf1ad323ba13bdbe18ecac6f3ce2f6d97b0d0ce90e7439fbf4febc8"></a>

### `spendguard.setup:install_skills`

Source: `src/spendguard/setup.py:852`

Deploy the repo's skills/ as Claude slash-commands (copy into ~/.claude/skills/). They then work
as /<name> in Claude Code (CLI + the VS Code extension). `spendguard install-skills`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| dest | Any | no | None | Deploy the repo's skills/ as Claude slash-commands (copy into ~/.claude/skills/). They then work<br>as /<name> in Claude Code (CLI + the VS Code extension). `spendguard install-skills`. |

Returns: `Any`

<a id="core-1304f9ccb5e8e5adcb9a74ede8f81001e323da97bd79cd8ea4353b731e0dfc82"></a>

### `spendguard.setup:set_declared_configuration_value`

Source: `src/spendguard/setup.py:980 (setup.cmd_config)`

Update one effective configuration setting Extract set branch: SETTINGS lookup/suggestions, _coerce by kind, _write_store, _resolve_setting after write, secret masking. Existing CLI raw joins trailing tokens with spaces. Audited CLI invocation: spendguard config set <dotted.key> <value>; governing flags: see command inventory `config`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| dotted | str | yes | null | Extract set branch: SETTINGS lookup/suggestions, _coerce by kind, _write_store, _resolve_setting after write, secret masking. Existing CLI raw joins trailing tokens with spaces. |
| raw | str | yes | null | Extract set branch: SETTINGS lookup/suggestions, _coerce by kind, _write_store, _resolve_setting after write, secret masking. Existing CLI raw joins trailing tokens with spaces. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-5d23f0a882e35bfd1405cb18f6c7295b51cf73e8aa65a11d32d2a32d4da18235"></a>

### `spendguard.setup:show_declared_configuration`

Source: `src/spendguard/setup.py:980 (setup.cmd_config)`

Read effective spendguard configuration Extract show/list SETTINGS iteration, _resolve_setting precedence and secret masking; includes source/store paths. Audited CLI invocation: spendguard config [show|list].

Takes no parameters.

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-4be058be90090e01e2f82cfc04bd153afda0848087bad1e1ad983b04e85b88a7"></a>

### `spendguard.share:export_scrubbed_insights`

Source: `src/spendguard/share.py:146 (share.main)`

Export scrubbed cost learnings to a file Extract _shareable(min_conf, require_active=not all), scrub/generalize, preview, optional schema envelope JSON write. Audited CLI invocation: spendguard insights export; governing flags: see command inventory `insights`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| out | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract _shareable(min_conf, require_active=not all), scrub/generalize, preview, optional schema envelope JSON write. |
| min_conf | float | no | {"source": "src/spendguard/share.py:cmd_export", "expression": "argparse --min-conf default"} | Extract _shareable(min_conf, require_active=not all), scrub/generalize, preview, optional schema envelope JSON write. |
| all | bool | no | false | Extract _shareable(min_conf, require_active=not all), scrub/generalize, preview, optional schema envelope JSON write. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-aeeaecf8023e2c3e145e3d403d05d5fd480ae38a0c0f700677deb6cdf6225d78"></a>

### `spendguard.share:import_community_insights`

Source: `src/spendguard/share.py:146 (share.main)`

Import cost learnings with explicit trust handling Extract dict-envelope/list parse and add_insight candidate community entries; preserve confidence zero/clamping/trust cap and existing lesson500 truncation. Audited CLI invocation: spendguard insights import PATH; governing flags: see command inventory `insights`.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| path | str | yes | null | Extract dict-envelope/list parse and add_insight candidate community entries; preserve confidence zero/clamping/trust cap and existing lesson500 truncation. |
| trust | float | no | {"source": "src/spendguard/share.py:cmd_import", "expression": "argparse --trust default"} | Extract dict-envelope/list parse and add_insight candidate community entries; preserve confidence zero/clamping/trust cap and existing lesson500 truncation. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-abbcea1acc11e030d4f289a2437c1aa25eb4a1a30a735bd4a6bd0d13ca20d422"></a>

### `spendguard.share:scrubbed_abstracts`

Source: `src/spendguard/share.py:52`

The scrubbed, shareable rules to push to the SaaS server (`saas.push_insights`). Same scrubber + gate as
`insights export`: identity removed ($ amounts, intent names, evidence snippets), generalizable rule kept
(task_class/regime/condition→action/mechanism/lesson + confidence/quality_basis). Returns a list of dicts.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| min_conf | Any | no | 0.6 | Source parameter min_conf at src/spendguard/share.py:52; see complete callable contract. |
| active_only | Any | no | True | Source parameter active_only at src/spendguard/share.py:52; see complete callable contract. |

Returns: `Any`

<a id="core-c9abf9fa868b78a61f44eeb1b113d38f84c1a97c25f7254bc86f6affb0ce2832"></a>

### `spendguard.signal:build_signals`

Source: `src/spendguard/signal.py:10`

Per (project, intent, model) signal rows from the recovered call corpus + batch costs + quality.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Per (project, intent, model) signal rows from the recovered call corpus + batch costs + quality. |

Returns: `Any`

<a id="core-da3755bd7630c6bc9be819b67c710d4d2630fed507e025e94a980371b7ad1ba8"></a>

### `spendguard.signal:push_signal`

Source: `src/spendguard/signal.py:123`

Push THIS repo's project signal (scrubbed) → /v1/signal via the repo key. Same project filter as the
ledger roll-up: the connection's own project(s), plus 'unattributed'/'llm-spendguard' (own meta) iff it owns_account
— so the account-owner also carries the shared no-evidence cancellation loss, and other repos don't double-count.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| dry | Any | no | False | Push THIS repo's project signal (scrubbed) → /v1/signal via the repo key. Same project filter as the<br>ledger roll-up: the connection's own project(s), plus 'unattributed'/'llm-spendguard' (own meta) iff it owns_account<br>— so the account-owner also carries the shared no-evidence cancellation loss, and other repos don't double-count. |

Returns: `Any`

<a id="core-b7b829b42a30bffb565c1bb98ddc63c88756419d635b45e4ba1f373cedeeedf5"></a>

### `spendguard.sources:discover_sources`

Source: `src/spendguard/sources.py:178`

The whole picture, in one call. Free, local, no LLM.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| days | Any | no | None | The whole picture, in one call. Free, local, no LLM. |

Returns: `Any`

<a id="core-b1acb3bb101b246854411302a62735567d4ca5a7052f6518982bdbdb91f4a559"></a>

### `spendguard.stability:measure_stability`

Source: `src/spendguard/stability.py:21`

Run each prompt `k` times on its normal production path and report run-to-run variance.

`parse` (str -> hashable) extracts the comparable OUTCOME of each reply — a classification label, a decision — which
is PARSING a fixed-shape result, not judging meaning (so it stays on the right side of the agentic-decision rule);
the default compares the stripped raw text. For free-text where equivalence is a judgement, the caller passes a
`parse` that canonicalises, or scores the returned outcomes with its own agentic judge.

Returns a dict:
  model, k, n_prompts, executor (the path that served — a lane name or 'api', from the first served call)
  per_prompt: [{prompt_idx, outcomes:[o_1..o_k], distinct, stable (1 outcome & 0 errors), errors}]
  stable_frac: prompts stable across all k runs / n_prompts  (1.0 == fully reproducible at production settings)
  flipped:     prompt indices whose outcome was NOT identical across the k runs
  label_rate_by_run: {run_idx: {label: count}} — the per-RUN outcome distribution
  rate_spread: {label: {min_frac, max_frac, band}} — min..max of the label's per-run rate across runs; `band` is the
               nondeterminism amplitude (the 7.7%↔31.8% = 0.246 band the operator measured) to compare against the
               between-arm effect an A/B is trying to detect.
Never raises on a call failure — a failed replicate is counted (errors) and surfaced, never silently dropped.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| prompts | Any | yes | required | Run each prompt `k` times on its normal production path and report run-to-run variance.<br><br>`parse` (str -> hashable) extracts the comparable OUTCOME of each reply — a classification label, a decision — which<br>is PARSING a fixed-shape result, not judging meaning (so it stays on the right side of the agentic-decision rule);<br>the default compares the stripped raw text. For free-text where equivalence is a judgement, the caller passes a<br>`parse` that canonicalises, or scores the returned outcomes with its own agentic judge.<br><br>Returns a dict:<br>  model, k, n_prompts, executor (the path that served — a lane name or 'api', from the first served call)<br>  per_prompt: [{prompt_idx, outcomes:[o_1..o_k], distinct, stable (1 outcome & 0 errors), errors}]<br>  stable_frac: prompts stable across all k runs / n_prompts  (1.0 == fully reproducible at production settings)<br>  flipped:     prompt indices whose outcome was NOT identical across the k runs<br>  label_rate_by_run: {run_idx: {label: count}} — the per-RUN outcome distribution<br>  rate_spread: {label: {min_frac, max_frac, band}} — min..max of the label's per-run rate across runs; `band` is the<br>               nondeterminism amplitude (the 7.7%↔31.8% = 0.246 band the operator measured) to compare against the<br>               between-arm effect an A/B is trying to detect.<br>Never raises on a call failure — a failed replicate is counted (errors) and surfaced, never silently dropped. |
| model | Any | yes | required | Run each prompt `k` times on its normal production path and report run-to-run variance.<br><br>`parse` (str -> hashable) extracts the comparable OUTCOME of each reply — a classification label, a decision — which<br>is PARSING a fixed-shape result, not judging meaning (so it stays on the right side of the agentic-decision rule);<br>the default compares the stripped raw text. For free-text where equivalence is a judgement, the caller passes a<br>`parse` that canonicalises, or scores the returned outcomes with its own agentic judge.<br><br>Returns a dict:<br>  model, k, n_prompts, executor (the path that served — a lane name or 'api', from the first served call)<br>  per_prompt: [{prompt_idx, outcomes:[o_1..o_k], distinct, stable (1 outcome & 0 errors), errors}]<br>  stable_frac: prompts stable across all k runs / n_prompts  (1.0 == fully reproducible at production settings)<br>  flipped:     prompt indices whose outcome was NOT identical across the k runs<br>  label_rate_by_run: {run_idx: {label: count}} — the per-RUN outcome distribution<br>  rate_spread: {label: {min_frac, max_frac, band}} — min..max of the label's per-run rate across runs; `band` is the<br>               nondeterminism amplitude (the 7.7%↔31.8% = 0.246 band the operator measured) to compare against the<br>               between-arm effect an A/B is trying to detect.<br>Never raises on a call failure — a failed replicate is counted (errors) and surfaced, never silently dropped. |
| k | Any | no | DEFAULT_REPLICATES | Run each prompt `k` times on its normal production path and report run-to-run variance.<br><br>`parse` (str -> hashable) extracts the comparable OUTCOME of each reply — a classification label, a decision — which<br>is PARSING a fixed-shape result, not judging meaning (so it stays on the right side of the agentic-decision rule);<br>the default compares the stripped raw text. For free-text where equivalence is a judgement, the caller passes a<br>`parse` that canonicalises, or scores the returned outcomes with its own agentic judge.<br><br>Returns a dict:<br>  model, k, n_prompts, executor (the path that served — a lane name or 'api', from the first served call)<br>  per_prompt: [{prompt_idx, outcomes:[o_1..o_k], distinct, stable (1 outcome & 0 errors), errors}]<br>  stable_frac: prompts stable across all k runs / n_prompts  (1.0 == fully reproducible at production settings)<br>  flipped:     prompt indices whose outcome was NOT identical across the k runs<br>  label_rate_by_run: {run_idx: {label: count}} — the per-RUN outcome distribution<br>  rate_spread: {label: {min_frac, max_frac, band}} — min..max of the label's per-run rate across runs; `band` is the<br>               nondeterminism amplitude (the 7.7%↔31.8% = 0.246 band the operator measured) to compare against the<br>               between-arm effect an A/B is trying to detect.<br>Never raises on a call failure — a failed replicate is counted (errors) and surfaced, never silently dropped. |
| intent | Any | no | None | Run each prompt `k` times on its normal production path and report run-to-run variance.<br><br>`parse` (str -> hashable) extracts the comparable OUTCOME of each reply — a classification label, a decision — which<br>is PARSING a fixed-shape result, not judging meaning (so it stays on the right side of the agentic-decision rule);<br>the default compares the stripped raw text. For free-text where equivalence is a judgement, the caller passes a<br>`parse` that canonicalises, or scores the returned outcomes with its own agentic judge.<br><br>Returns a dict:<br>  model, k, n_prompts, executor (the path that served — a lane name or 'api', from the first served call)<br>  per_prompt: [{prompt_idx, outcomes:[o_1..o_k], distinct, stable (1 outcome & 0 errors), errors}]<br>  stable_frac: prompts stable across all k runs / n_prompts  (1.0 == fully reproducible at production settings)<br>  flipped:     prompt indices whose outcome was NOT identical across the k runs<br>  label_rate_by_run: {run_idx: {label: count}} — the per-RUN outcome distribution<br>  rate_spread: {label: {min_frac, max_frac, band}} — min..max of the label's per-run rate across runs; `band` is the<br>               nondeterminism amplitude (the 7.7%↔31.8% = 0.246 band the operator measured) to compare against the<br>               between-arm effect an A/B is trying to detect.<br>Never raises on a call failure — a failed replicate is counted (errors) and surfaced, never silently dropped. |
| system | Any | no | None | Run each prompt `k` times on its normal production path and report run-to-run variance.<br><br>`parse` (str -> hashable) extracts the comparable OUTCOME of each reply — a classification label, a decision — which<br>is PARSING a fixed-shape result, not judging meaning (so it stays on the right side of the agentic-decision rule);<br>the default compares the stripped raw text. For free-text where equivalence is a judgement, the caller passes a<br>`parse` that canonicalises, or scores the returned outcomes with its own agentic judge.<br><br>Returns a dict:<br>  model, k, n_prompts, executor (the path that served — a lane name or 'api', from the first served call)<br>  per_prompt: [{prompt_idx, outcomes:[o_1..o_k], distinct, stable (1 outcome & 0 errors), errors}]<br>  stable_frac: prompts stable across all k runs / n_prompts  (1.0 == fully reproducible at production settings)<br>  flipped:     prompt indices whose outcome was NOT identical across the k runs<br>  label_rate_by_run: {run_idx: {label: count}} — the per-RUN outcome distribution<br>  rate_spread: {label: {min_frac, max_frac, band}} — min..max of the label's per-run rate across runs; `band` is the<br>               nondeterminism amplitude (the 7.7%↔31.8% = 0.246 band the operator measured) to compare against the<br>               between-arm effect an A/B is trying to detect.<br>Never raises on a call failure — a failed replicate is counted (errors) and surfaced, never silently dropped. |
| parse | Any | no | None | Run each prompt `k` times on its normal production path and report run-to-run variance.<br><br>`parse` (str -> hashable) extracts the comparable OUTCOME of each reply — a classification label, a decision — which<br>is PARSING a fixed-shape result, not judging meaning (so it stays on the right side of the agentic-decision rule);<br>the default compares the stripped raw text. For free-text where equivalence is a judgement, the caller passes a<br>`parse` that canonicalises, or scores the returned outcomes with its own agentic judge.<br><br>Returns a dict:<br>  model, k, n_prompts, executor (the path that served — a lane name or 'api', from the first served call)<br>  per_prompt: [{prompt_idx, outcomes:[o_1..o_k], distinct, stable (1 outcome & 0 errors), errors}]<br>  stable_frac: prompts stable across all k runs / n_prompts  (1.0 == fully reproducible at production settings)<br>  flipped:     prompt indices whose outcome was NOT identical across the k runs<br>  label_rate_by_run: {run_idx: {label: count}} — the per-RUN outcome distribution<br>  rate_spread: {label: {min_frac, max_frac, band}} — min..max of the label's per-run rate across runs; `band` is the<br>               nondeterminism amplitude (the 7.7%↔31.8% = 0.246 band the operator measured) to compare against the<br>               between-arm effect an A/B is trying to detect.<br>Never raises on a call failure — a failed replicate is counted (errors) and surfaced, never silently dropped. CLI DELIBERATE parameter restriction: A Python parser callable cannot be transported as JSON. MCP DELIBERATE parameter restriction: A Python parser callable cannot be transported as JSON. |
| reasoning | Any | no | None | Run each prompt `k` times on its normal production path and report run-to-run variance.<br><br>`parse` (str -> hashable) extracts the comparable OUTCOME of each reply — a classification label, a decision — which<br>is PARSING a fixed-shape result, not judging meaning (so it stays on the right side of the agentic-decision rule);<br>the default compares the stripped raw text. For free-text where equivalence is a judgement, the caller passes a<br>`parse` that canonicalises, or scores the returned outcomes with its own agentic judge.<br><br>Returns a dict:<br>  model, k, n_prompts, executor (the path that served — a lane name or 'api', from the first served call)<br>  per_prompt: [{prompt_idx, outcomes:[o_1..o_k], distinct, stable (1 outcome & 0 errors), errors}]<br>  stable_frac: prompts stable across all k runs / n_prompts  (1.0 == fully reproducible at production settings)<br>  flipped:     prompt indices whose outcome was NOT identical across the k runs<br>  label_rate_by_run: {run_idx: {label: count}} — the per-RUN outcome distribution<br>  rate_spread: {label: {min_frac, max_frac, band}} — min..max of the label's per-run rate across runs; `band` is the<br>               nondeterminism amplitude (the 7.7%↔31.8% = 0.246 band the operator measured) to compare against the<br>               between-arm effect an A/B is trying to detect.<br>Never raises on a call failure — a failed replicate is counted (errors) and surfaced, never silently dropped. |

Returns: `Any`

<a id="core-a974e1222b3cc14a610664eebd44b8cbc0e24e14506a23b3b5bd00b3ede9cad2"></a>

### `spendguard.submit:guarded_submit`

Source: `src/spendguard/submit.py:353`

Estimate -> enforce cap -> PRE-FLIGHT one live request -> log -> submit. Raises RuntimeError if it won't pass. `endpoint` is the Batch API
target the .jsonl lines address ('/v1/chat/completions' by default, '/v1/embeddings' for an embeddings batch) —
it must match the lines' `url`, so it is a parameter, not a hardcoded literal at the batches.create call.

`intent` is the caller's job-type label. It is set on the recording CONTEXT for the duration of the submit so the
gate's provisional batch-cost row (gate._decide_and_account → calls.record_call, fired synchronously inside the
gated files.create) attributes to it instead of '(none)' — a batch is attributable work, and its spend must carry
its intent from submission, not only when reconcile later matches it by batch_id.

`metadata` (an OpenAI batch metadata dict, keys/values ≤64/512 chars, ≤16 pairs) is attached to the created batch.
batch_tracker.submit_offload uses it to stamp a DETERMINISTIC offload key on the batch so a crashed-then-retried
offload is de-duplicated against the provider's own batch list (exactly-once) instead of double-submitting.

`deadline_at` is an optional finite absolute time.monotonic() timestamp for ADMISSION:
preflight, upload and create each receive the remaining control-plane timeout with SDK retries
disabled. None preserves existing behavior. It does not bound server-side batch completion
(the provider completion_window does); execution deadlines require governed realtime.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| jsonl_path | Any | yes | required | Source parameter jsonl_path at src/spendguard/submit.py:353; see complete callable contract. |
| model | Any | yes | required | Source parameter model at src/spendguard/submit.py:353; see complete callable contract. |
| cap_dollars | Any | yes | required | Source parameter cap_dollars at src/spendguard/submit.py:353; see complete callable contract. |
| batch | Any | no | True | Source parameter batch at src/spendguard/submit.py:353; see complete callable contract. |
| avg_out_tokens | Any | no | None | Source parameter avg_out_tokens at src/spendguard/submit.py:353; see complete callable contract. |
| expected_cost | Any | no | None | Source parameter expected_cost at src/spendguard/submit.py:353; see complete callable contract. |
| submit | Any | no | True | Source parameter submit at src/spendguard/submit.py:353; see complete callable contract. |
| request_cap | Any | no | 25000 | Source parameter request_cap at src/spendguard/submit.py:353; see complete callable contract. |
| overrun_tolerance | Any | no | DEFAULT_OVERRUN_TOLERANCE | Source parameter overrun_tolerance at src/spendguard/submit.py:353; see complete callable contract. |
| endpoint | Any | no | '/v1/chat/completions' | Source parameter endpoint at src/spendguard/submit.py:353; see complete callable contract. |
| intent | Any | no | None | Source parameter intent at src/spendguard/submit.py:353; see complete callable contract. |
| metadata | Any | no | None | Source parameter metadata at src/spendguard/submit.py:353; see complete callable contract. |
| preflight | Any | no | True | Source parameter preflight at src/spendguard/submit.py:353; see complete callable contract. |
| force | Any | no | False | Source parameter force at src/spendguard/submit.py:353; see complete callable contract. |
| deadline_at | float \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "number"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "float \| None", "type": "any"} | no | None | Source parameter deadline_at at src/spendguard/submit.py:353; see complete callable contract. |

Returns: `Any`

<a id="core-a52bbe600cbd0d4aa7b08937f2017069531a9173f2908c75a6ec70d33ed536c5"></a>

### `spendguard.submit:submit_chat_tasks`

Source: `src/spendguard/submit.py:520`

Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the
first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires
route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto
= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the
ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then
ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns
{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin
callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect
directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error
(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —
batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| tasks | Any | yes | required | Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the<br>first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires<br>route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto<br>= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the<br>ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then<br>ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns<br>{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin<br>callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect<br>directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error<br>(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —<br>batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication. |
| model | Any | yes | required | Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the<br>first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires<br>route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto<br>= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the<br>ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then<br>ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns<br>{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin<br>callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect<br>directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error<br>(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —<br>batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication. |
| system | Any | no | None | Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the<br>first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires<br>route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto<br>= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the<br>ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then<br>ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns<br>{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin<br>callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect<br>directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error<br>(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —<br>batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication. |
| schema | Any | no | None | Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the<br>first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires<br>route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto<br>= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the<br>ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then<br>ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns<br>{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin<br>callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect<br>directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error<br>(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —<br>batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication. |
| reasoning | Any | no | 'minimal' | Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the<br>first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires<br>route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto<br>= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the<br>ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then<br>ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns<br>{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin<br>callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect<br>directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error<br>(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —<br>batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication. |
| max_out | Any | no | None | Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the<br>first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires<br>route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto<br>= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the<br>ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then<br>ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns<br>{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin<br>callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect<br>directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error<br>(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —<br>batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication. |
| cap_dollars | Any | no | None | Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the<br>first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires<br>route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto<br>= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the<br>ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then<br>ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns<br>{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin<br>callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect<br>directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error<br>(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —<br>batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication. |
| submit | Any | no | True | Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the<br>first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires<br>route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto<br>= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the<br>ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then<br>ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns<br>{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin<br>callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect<br>directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error<br>(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —<br>batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication. |
| intent | Any | no | None | Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the<br>first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires<br>route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto<br>= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the<br>ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then<br>ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns<br>{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin<br>callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect<br>directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error<br>(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —<br>batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication. |
| metadata | Any | no | None | Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the<br>first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires<br>route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto<br>= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the<br>ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then<br>ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns<br>{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin<br>callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect<br>directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error<br>(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —<br>batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication. |
| preflight | Any | no | True | Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the<br>first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires<br>route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto<br>= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the<br>ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then<br>ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns<br>{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin<br>callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect<br>directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error<br>(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —<br>batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication. |
| shard_size | Any | no | None | Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the<br>first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires<br>route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto<br>= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the<br>ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then<br>ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns<br>{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin<br>callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect<br>directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error<br>(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —<br>batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication. |
| force | Any | no | False | Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the<br>first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires<br>route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto<br>= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the<br>ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then<br>ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns<br>{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin<br>callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect<br>directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error<br>(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —<br>batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication. |
| avg_out_tokens | Any | no | None | Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the<br>first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires<br>route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto<br>= 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the<br>ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then<br>ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns<br>{batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin<br>callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect<br>directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error<br>(the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —<br>batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication. |

Returns: `Any`

<a id="core-0dc752289be6c59fe664eb585217ed3206540c87f8ca147ab2ce9543f1a694d6"></a>

### `spendguard.submit:submit_message_batch`

Source: `src/spendguard/submit.py:651`

Submit a list of tasks to the Anthropic Message Batches API (~half realtime, 29-day result window) — the
Anthropic twin of submit_chat_tasks, and the Messages-API half of spendguard's batch surface. Each task is a prompt
STRING (custom_id auto = 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the
INLINE requests through the ONE models.apply_call_params authority (build_message_batch_requests), ESTIMATES $0 via
gate.estimate_message_batch (the SAME estimator the gate re-runs at create — no drift), REFUSES over the cap, then
submits via client.messages.batches.create — which the gate intercepts (_gate_anthropic) for the global/daily/monthly
+ per-batch caps AND the provisional batch-cost row (attributed to `intent` via the recording context set here).
Returns {batch_id, requests, estimate, error}; submit=False estimates only ($0). Collect with its SETTLE twin
callio.collect_message_batch(batch_id, intent, model).

COST ESTIMATE BASIS (the authorization number) is what the job will PLAUSIBLY emit, NOT the ceiling we send: the
MEASURED per-intent output (seeded as this `intent` accrues history) → a DECLARED `expected_out_tokens` (per request)
→ the ceiling, named honestly in est['out_basis']. `expected_out_tokens` lets a caller who has measured their output
authorize a cold-intent job without the ceiling's ~100x over-statement; `max_out` informs NEITHER the send (spendguard
sends the ceiling, billed on ACTUAL tokens) NOR the estimate, so a passed max_out is pointed at expected_out_tokens.

DUAL CAP, named: the result states BOTH the caller `cap_dollars` AND the global GATE_CAP and WHICH one bound — so a
pass here can't be followed by a surprise gate refusal citing the other. A genuinely runaway request set is still
caught by the gate's SEPARATE worst-case (ceiling) guard. REFUSE, NEVER DEGRADE: over a cap the result carries the
estimate + a REFUSED error and NO batch is created; a gate refusal at create PROPAGATES — never a silent realtime
fallback. Anthropic-only; a non-Anthropic model returns a clear error.

Unlike OpenAI there is NO `metadata` parameter: Anthropic batches carry no server-side metadata, so the
exactly-once offload key (batch_tracker) rides each request's custom_id instead — see batch_tracker.submit_offload.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| tasks | Any | yes | required | Source parameter tasks at src/spendguard/submit.py:651; see complete callable contract. |
| model | Any | yes | required | Source parameter model at src/spendguard/submit.py:651; see complete callable contract. |
| system | Any | no | None | Source parameter system at src/spendguard/submit.py:651; see complete callable contract. |
| schema | Any | no | None | Source parameter schema at src/spendguard/submit.py:651; see complete callable contract. |
| max_out | Any | no | None | Source parameter max_out at src/spendguard/submit.py:651; see complete callable contract. |
| expected_out_tokens | Any | no | None | Source parameter expected_out_tokens at src/spendguard/submit.py:651; see complete callable contract. |
| cap_dollars | Any | no | None | Source parameter cap_dollars at src/spendguard/submit.py:651; see complete callable contract. |
| submit | Any | no | True | Source parameter submit at src/spendguard/submit.py:651; see complete callable contract. |
| request_cap | Any | no | _ANTHROPIC_BATCH_REQUEST_CAP | Source parameter request_cap at src/spendguard/submit.py:651; see complete callable contract. |
| intent | Any | no | None | Source parameter intent at src/spendguard/submit.py:651; see complete callable contract. |
| preflight | Any | no | True | Source parameter preflight at src/spendguard/submit.py:651; see complete callable contract. |
| shard_size | Any | no | None | Source parameter shard_size at src/spendguard/submit.py:651; see complete callable contract. |
| force | Any | no | False | Source parameter force at src/spendguard/submit.py:651; see complete callable contract. |

Returns: `Any`

<a id="core-f185508c09966120ac63b09dc26e5ebf8cd30a74c273af53b09feb41f5aea688"></a>

### `spendguard.sync:sync`

Source: `src/spendguard/sync.py:174`

Fetch and validate the upstream model snapshot, then atomically refresh the local catalog cache.

Takes no parameters.

Returns: `Any`

<a id="core-b1f8055a1292e26e60da46aaa6b58218eaebdf28ed41e75358a12e6fff49b9f6"></a>

### `spendguard.sync_capabilities:sync_capabilities`

Source: `src/spendguard/sync_capabilities.py:185`

Sync LiteLLM capabilities + upper bounds into model_catalog.json for every model LiteLLM covers. Returns the
summary dict. dry_run computes the summary and writes NOTHING. The write is atomic + backed up (config.update_json).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| dry_run | Any | no | False | Source parameter dry_run at src/spendguard/sync_capabilities.py:185; see complete callable contract. |

Returns: `Any`

<a id="core-4413b4ff5e539e30cc97beb6e33bac73757931e81fb4dcf1035aefca628fbd06"></a>

### `spendguard.tag:estimate_llm_retag`

Source: `src/spendguard/tag.py:99`

Zero-spend estimate for the LLM residual pass (per the API spend protocol — estimate BEFORE spending).
Returns {rows, est_usd, model}. The actual capped pass is a separate, explicitly-approved step.

Takes no parameters.

Returns: `Any`

<a id="core-c66552e492e2a2bee7f156517b79fc7872516b440a8b37d2ef3a685427979521"></a>

### `spendguard.tag:move_project`

Source: `src/spendguard/tag.py:68`

Re-assign a project tag across the ledger — fixes cwd-fallback mistags (e.g. video-captioning that ran
from ~/Documents got tagged 'documents' instead of 'vision-pipeline'). Returns rows changed.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| old | Any | yes | required | Re-assign a project tag across the ledger — fixes cwd-fallback mistags (e.g. video-captioning that ran<br>from ~/Documents got tagged 'documents' instead of 'vision-pipeline'). Returns rows changed. |
| new | Any | yes | required | Re-assign a project tag across the ledger — fixes cwd-fallback mistags (e.g. video-captioning that ran<br>from ~/Documents got tagged 'documents' instead of 'vision-pipeline'). Returns rows changed. |

Returns: `Any`

<a id="core-a78130fd5d3c65d0510c9036010c2ee03ced5e5bcc71107c831bcb48541b0cf7"></a>

### `spendguard.tier_config:reachability_probe`

Source: `src/spendguard/tier_config.py:90`

The check tier_config_report CANNOT make: does each ENABLED lane's DECLARED tier model actually SERVE $0 on
its lane, or does the lane CLI REJECT it and silently fall back to the METERED API (the 'green report, inert
lane' class, one level below tier_config_report)? Dispatches through adapters.call — the SAME production path,
so the lane-boundary resolution (e.g. the Gemini composer that turns a base id into a served tier suffix) is
applied exactly as a real fungible call would be. PINNED (no_substitution) + no_metered_fallback => $0 BY
CONSTRUCTION: a lane miss is an error, never a metered call. served = the lane answered (text, no error).
Persists a snapshot (save) so doctor/tiers render it without re-probing. Returns
{asof, rows:[{lane, model, tiers, served, error}]}.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| save | Any | no | True | The check tier_config_report CANNOT make: does each ENABLED lane's DECLARED tier model actually SERVE $0 on<br>its lane, or does the lane CLI REJECT it and silently fall back to the METERED API (the 'green report, inert<br>lane' class, one level below tier_config_report)? Dispatches through adapters.call — the SAME production path,<br>so the lane-boundary resolution (e.g. the Gemini composer that turns a base id into a served tier suffix) is<br>applied exactly as a real fungible call would be. PINNED (no_substitution) + no_metered_fallback => $0 BY<br>CONSTRUCTION: a lane miss is an error, never a metered call. served = the lane answered (text, no error).<br>Persists a snapshot (save) so doctor/tiers render it without re-probing. Returns<br>{asof, rows:[{lane, model, tiers, served, error}]}. |

Returns: `Any`

<a id="core-8716c532d16cc8478e93dc0478979cb0bc8cecd70b46567a0b63b1959a864553"></a>

### `spendguard.tier_config:tier_config_report`

Source: `src/spendguard/tier_config.py:51`

The bulk-lane surface's config health, as data (doctor + the `tiers` CLI both render this). Answers the two
questions the review named: is each declared GROUP model priced, and does each group have a LANE serving it; and
is each lane's declared model IN some group (else it can never be picked for a --tier fan). issues = hard gaps
(a fan will refuse / mis-cost); warnings = a lane that serves no group. configured=False → advisor.tiers unset.

Takes no parameters.

Returns: `Any`

<a id="core-b853d77c1603634b2e7509ba76a0300c3f329ea583116c26064db6a753fce1fa"></a>

### `spendguard.token_caps:review_output_token_caps`

Source: `src/spendguard/token_caps.py:232 (token_caps.cmd)`

Find and adjudicate hardcoded token caps Extract scan, stale/unjudged verdict display, optional paid adjudication + verdict save + rescan; preserve exit on unresolved coverage. Audited CLI invocation: spendguard token-caps --judge --scan.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| judge | bool | no | false | Extract scan, stale/unjudged verdict display, optional paid adjudication + verdict save + rescan; preserve exit on unresolved coverage. |
| scan | str \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "str \| None", "type": "any"} | no | null | Extract scan, stale/unjudged verdict display, optional paid adjudication + verdict save + rescan; preserve exit on unresolved coverage. |

Returns: `Existing bound core result; printed output retained by JSON surfaces.`

<a id="core-52fe78df70e11466d6e34c0b500b3739acf924ebae51f41e07fb83bd0f193190"></a>

### `spendguard.trust:trust_report`

Source: `src/spendguard/trust.py:84`

Pull provider truth + the local ledger (+ the server total, if connected) and return the verdicts. The
daily trust report. Free.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Pull provider truth + the local ledger (+ the server total, if connected) and return the verdicts. The<br>daily trust report. Free. |
| with_server | Any | no | True | Pull provider truth + the local ledger (+ the server total, if connected) and return the verdicts. The<br>daily trust report. Free. |

Returns: `Any`

<a id="core-eb9fb14f8850a21cb4556f0512d545c0eb561f4691e1b880c9f2fbdc468d30b0"></a>

### `spendguard.truth:push_truth`

Source: `src/spendguard/truth.py:35`

Push truth rows → POST /v1/truth. Honors visibility (private = nothing leaves); tolerates a server
that doesn't implement the endpoint yet (the same forward-compat pattern as push_insights).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Push truth rows → POST /v1/truth. Honors visibility (private = nothing leaves); tolerates a server<br>that doesn't implement the endpoint yet (the same forward-compat pattern as push_insights). |
| dry | Any | no | False | Push truth rows → POST /v1/truth. Honors visibility (private = nothing leaves); tolerates a server<br>that doesn't implement the endpoint yet (the same forward-compat pattern as push_insights). |
| truth_rows | Any | no | None | Push truth rows → POST /v1/truth. Honors visibility (private = nothing leaves); tolerates a server<br>that doesn't implement the endpoint yet (the same forward-compat pattern as push_insights). |

Returns: `Any`

<a id="core-4e3153b7a00383bd4f2f78881a19de7269632d2ae73a910d987450da60822fb6"></a>

### `spendguard.truth:rows`

Source: `src/spendguard/truth.py:17`

[{day, provider, usd}] for every day ≥ `since` (default: 35 days — covers a full statement month
plus reconciliation lag) across all sources the report knows: openai · anthropic · vastai (GPU).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | [{day, provider, usd}] for every day ≥ `since` (default: 35 days — covers a full statement month<br>plus reconciliation lag) across all sources the report knows: openai · anthropic · vastai (GPU). |

Returns: `Any`

<a id="core-d7aededd48b7e4be54ae90907288c404511e3b6c15c85c35d55cfcdbd53c7b24"></a>

### `spendguard.validate:validate`

Source: `src/spendguard/validate.py:140`

Recheck stored insights against current evidence and persist their confidence/status changes.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| verbose | Any | no | True | Print verdict changes, summary counts and active learnings. |

Returns: `Any`

<a id="core-5578d64dce4c619e3414984f06f207068bbcd363fc04250243b740351af5f23d"></a>

### `spendguard.vendor_call:call`

Source: `src/spendguard/vendor_call.py:320`

Call ONE model, bounded by a TOTAL deadline, returning a typed Result. Never raises for a call failure.

`no_metered_fallback=True` is the explicit $0-ONLY contract: any lane miss, including a down executor, returns a
free error instead of billing. The default remains ensure-success: exhaust quality-proven $0 alternatives, then
use the exact model's metered twin as a loud, ledger-accounted last resort.
Default False = a $0 lane that errors or is too busy sheds to its metered twin so the call completes — burst
behaves like isolation (see the dispatch shed below).

INPUT invariant:  deadline_s > 0 and bounds the WHOLE call including every retry.
OUTPUT invariant: a Result whose `.text` is readable ONLY when kind == 'ok'.

`max_tokens` is a TERMINATION bound, not a cost control (billing is on tokens generated). It is sized from
measurement — `expected_output`/`bulkgate.maxtokens` — never guessed, and never silently zero.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| vendor | Any | yes | required | Source parameter vendor at src/spendguard/vendor_call.py:320; see complete callable contract. |
| model | Any | yes | required | Requested model; explicit best-value may substitute a quality-qualified measured model unless model substitution is pinned. |
| prompt | Any | yes | required | Source parameter prompt at src/spendguard/vendor_call.py:320; see complete callable contract. |
| deadline_s | Any | yes | required | Required positive total deadline covering the whole vendor call and every retry; validated against measured latency when evidence exists. |
| purpose | Any | no | '' | Job label propagated into the intent context for attribution; vendor_call uses purpose rather than an intent keyword. |
| system | Any | no | None | Source parameter system at src/spendguard/vendor_call.py:320; see complete callable contract. |
| max_tokens | Any | no | None | Compatibility argument: normal execution obtains the output ceiling from adapters.output_budget and ignores a caller-selected cap. Actual generated tokens determine billing; input context limits are separate. |
| schema | Any | no | None | Source parameter schema at src/spendguard/vendor_call.py:320; see complete callable contract. |
| attempts | Any | no | IN_PROCESS_RETRY_ATTEMPTS | Maximum in-process attempts. Each paid attempt can bill; retry costs accumulate in the Result. |
| backoff_s | Any | no | 2.0 | Base retry-backoff duration, bounded by the remaining total deadline. |
| reasoning | Any | no | None | Explicit 'best-value' selects measured model and reasoning effort for the intent when quality evidence qualifies. Inherited advisor.default_reasoning='best-value' preserves a supplied model and selects effort; no_substitution also pins the model. |
| no_metered_fallback | Any | no | False | True forbids paid API fallback when a subscription lane fails or cannot serve the request. |

Returns: `Any`

<a id="core-7557f07359453eb415ba0ed0ad8d61b1ab4febda99a09a5360921f001379315d"></a>

### `spendguard.verify:verify_system`

Source: `src/spendguard/verify.py:65`

Run the checks and return a structured verdict:
  {preflight:[…], fallback:[…], keys:[…], economics:[…], probe:[…]|None, ok:bool}
ok is True only when every STRUCTURAL check passes (a model id callable-as-written or correctable, every lane's
fallback id priced + served-or-unchecked, every lane provider key present) — and, when probe=True, every live
failover reached the metered API. 'unchecked'/'measuring' states are NOT failures (a can't-know is not a no).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| probe | Any | no | False | Run the checks and return a structured verdict:<br>  {preflight:[…], fallback:[…], keys:[…], economics:[…], probe:[…]\|None, ok:bool}<br>ok is True only when every STRUCTURAL check passes (a model id callable-as-written or correctable, every lane's<br>fallback id priced + served-or-unchecked, every lane provider key present) — and, when probe=True, every live<br>failover reached the metered API. 'unchecked'/'measuring' states are NOT failures (a can't-know is not a no). |

Returns: `Any`

<a id="core-13eed6da6c5f694b78a2450a016e89830b74d6b236fae2c75a79720eadcf0aad"></a>

### `spendguard.warm_stdio_daemon:reap_orphaned_warm_servers`

Source: `src/spendguard/warm_stdio_daemon.py:152`

Reclaim warm-lane servers spendguard spawned whose spawning OWNER is no longer alive (the cross-session leak).

Selection is STRUCTURAL: a candidate is only a process carrying spendguard's own owner-marker env (stamped at
spawn), and it is reaped ONLY when its recorded owner (pid, create_time) is not a currently-living process with
that identity. A live pool's servers (owner alive) and the user's own codex/kimi processes (no marker) are never
touched. Each orphan is a session leader, so its whole process group is signalled.

lane: restrict to one lane label (e.g. "codex"); None reaps every warm lane.
execute: False ⇒ dry run — return the orphans that WOULD be reaped without signalling any.
Returns {"scanned", "orphans", "reaped", "cross_uid_skipped", "unreadable", "errors", "marked"}. ``marked``
exposes the lane and
owner identity facts from THIS SAME authoritative scan for operator reporting; it does not add another selector.
`cross_uid_skipped` counts permission-denied environments, which belong to another uid and therefore cannot be
spendguard warm servers. `unreadable` names every OTHER environment read failure — a VISIBLE same-uid gap (a
possible orphan we could not classify), never silently dropped: "cannot tell" is not "clean".

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| lane | Any | no | None | Reclaim warm-lane servers spendguard spawned whose spawning OWNER is no longer alive (the cross-session leak).<br><br>Selection is STRUCTURAL: a candidate is only a process carrying spendguard's own owner-marker env (stamped at<br>spawn), and it is reaped ONLY when its recorded owner (pid, create_time) is not a currently-living process with<br>that identity. A live pool's servers (owner alive) and the user's own codex/kimi processes (no marker) are never<br>touched. Each orphan is a session leader, so its whole process group is signalled.<br><br>lane: restrict to one lane label (e.g. "codex"); None reaps every warm lane.<br>execute: False ⇒ dry run — return the orphans that WOULD be reaped without signalling any.<br>Returns {"scanned", "orphans", "reaped", "cross_uid_skipped", "unreadable", "errors", "marked"}. ``marked``<br>exposes the lane and<br>owner identity facts from THIS SAME authoritative scan for operator reporting; it does not add another selector.<br>`cross_uid_skipped` counts permission-denied environments, which belong to another uid and therefore cannot be<br>spendguard warm servers. `unreadable` names every OTHER environment read failure — a VISIBLE same-uid gap (a<br>possible orphan we could not classify), never silently dropped: "cannot tell" is not "clean". |
| execute | Any | no | True | Reclaim warm-lane servers spendguard spawned whose spawning OWNER is no longer alive (the cross-session leak).<br><br>Selection is STRUCTURAL: a candidate is only a process carrying spendguard's own owner-marker env (stamped at<br>spawn), and it is reaped ONLY when its recorded owner (pid, create_time) is not a currently-living process with<br>that identity. A live pool's servers (owner alive) and the user's own codex/kimi processes (no marker) are never<br>touched. Each orphan is a session leader, so its whole process group is signalled.<br><br>lane: restrict to one lane label (e.g. "codex"); None reaps every warm lane.<br>execute: False ⇒ dry run — return the orphans that WOULD be reaped without signalling any.<br>Returns {"scanned", "orphans", "reaped", "cross_uid_skipped", "unreadable", "errors", "marked"}. ``marked``<br>exposes the lane and<br>owner identity facts from THIS SAME authoritative scan for operator reporting; it does not add another selector.<br>`cross_uid_skipped` counts permission-denied environments, which belong to another uid and therefore cannot be<br>spendguard warm servers. `unreadable` names every OTHER environment read failure — a VISIBLE same-uid gap (a<br>possible orphan we could not classify), never silently dropped: "cannot tell" is not "clean". |

Returns: `Any`

<a id="core-7b8735c1796f23f5b8146b42260771813a25024bf7469edbcf27e61975dcbaf5"></a>

### `spendguard.weekly_cap_guard:install_weekly_cap_guard`

Source: `src/spendguard/weekly_cap_guard.py:194`

Idempotently merge or remove only spendguard's weekly-cap PreToolUse hook.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| settings_path | Any | no | None | Idempotently merge or remove only spendguard's weekly-cap PreToolUse hook. |
| uninstall | Any | no | False | Idempotently merge or remove only spendguard's weekly-cap PreToolUse hook. |
| executable | Any | no | None | Idempotently merge or remove only spendguard's weekly-cap PreToolUse hook. |

Returns: `Any`

<a id="core-7c1a8e740c4381ec24a4fcbf193ded74db72075b8c1e0867aeb8eb98bdaee85b"></a>

### `spendguard.weekly_cap_guard:weekly_cap_guard_pretooluse`

Source: `src/spendguard/weekly_cap_guard.py:119`

Return the Claude Code PreToolUse decision. Deliberate policy denies closed; internal failures allow open.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| payload | Any | yes | required | Return the Claude Code PreToolUse decision. Deliberate policy denies closed; internal failures allow open. |

Returns: `Any`

<a id="core-ced4e6dea034361e8f1fa08a7620343c48170414360a6a65b7abc34f40e07c3f"></a>

### `spendguard.whole_job:collect_jobs`

Source: `src/spendguard/whole_job.py:439`

Settle the async BATCH handles from a prior run_jobs (the OpenAI Batch API's collect twin). Returns
{job_id: row}, keying each succeeded/failed job by its custom_id (== the job id) from callio.collect_chat_tasks's
STRUCTURED return ({results, failed, not_ready, anomalies, ...}) — a succeeded row {text}, a failed row {error},
both under the job id; a batch still running is reported under its batch_id as {status:'pending'}, unkeyable
anomalies under '<batch_id>-anomalies', and a handle with no batch_id under 'handle-<i>' — so NOTHING is dropped
(each row is a unit of work). $ is booked by callio.collect_chat_tasks under each handle's intent (attribution
preserved). A DELIBERATE stop (spend refusal / deadline) propagates — never downgraded to 'keep going'.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| pending | Any | yes | required | Source parameter pending at src/spendguard/whole_job.py:439; see complete callable contract. |

Returns: `Any`

<a id="core-b18419cfba5c2fee4e321842f113f23e578f870ab9506863def975e6c0279c54"></a>

### `spendguard.whole_job:load_pending`

Source: `src/spendguard/whole_job.py:374`

Recover the durable batch handles a run persisted (for collect_jobs) — the crash-recovery twin of
_persist_pending. Returns [] when the file is absent. Reads the WHOLE file (every handle), never a slice.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| pending_path | Any | yes | required | Source parameter pending_path at src/spendguard/whole_job.py:374; see complete callable contract. |

Returns: `Any`

<a id="core-d7560e6b5b5de16891610f1e872d4bba7bf03cb2eecce87c830ffaeef12f7923"></a>

### `spendguard.whole_job:plan_jobs`

Source: `src/spendguard/whole_job.py:216`

The PLAN for a whole job set — the per-intent method + estimate, and the whole-set estimate — with NO execution
and NO spend. run_jobs calls this first (estimate-first) so the budget gate can refuse before a cent is spent; a
caller can also call it alone to preview what spendguard would do. `est_usd` is None (unknown) if ANY group could
not be priced; `unpriced` names those groups. Returns {plan, est_usd, unpriced, groups, bad_jobs}.

When goal.urgency is 'auto' (the default), the per-group method + estimate come from the item-C HORIZON planner
(route_horizon): the groups are allocated TOGETHER against the ONE shared lane budget, urgent-first, with per-job
urgency deciding fast-realtime-overflow vs cheap-batch-overflow. A forced goal.urgency ('realtime'/'batch') keeps
the per-group route_report pick. Either way the budget gate reads the same est_usd.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| jobs | Any | yes | required | Source parameter jobs at src/spendguard/whole_job.py:216; see complete callable contract. |
| goal | Any | no | None | Source parameter goal at src/spendguard/whole_job.py:216; see complete callable contract. |

Returns: `Any`

<a id="core-fe7c6b7700ca6245e820f73a7eb1103c0fba6eaa7e00702ff590826091f48951"></a>

### `spendguard.whole_job:run_jobs`

Source: `src/spendguard/whole_job.py:256`

Plan + execute a whole job set under a goal, returning ready results + async batch handles + the plan/receipt.
Estimate-first, fail-CLOSED: the whole set is priced BEFORE any spend and REFUSED if the estimate exceeds
goal.budget_usd OR if any group is unpriced under a budget. `checkpoint` (a jsonl path) makes the realtime results
durable (crash-resume via bulk_delegate); each submitted batch handle is persisted to `<checkpoint>.pending.jsonl`
the moment it is submitted (a batch is PAID work — its batch_id must survive a crash). When checkpoint is None a
per-run path is created under the spendguard home so a crash never loses a completed result or a batch handle.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| jobs | Any | yes | required | Source parameter jobs at src/spendguard/whole_job.py:256; see complete callable contract. |
| goal | Any | no | None | Source parameter goal at src/spendguard/whole_job.py:256; see complete callable contract. |
| checkpoint | Any | no | None | Source parameter checkpoint at src/spendguard/whole_job.py:256; see complete callable contract. |

Returns: `Any`

<a id="core-1467dc4a4c4ccec2d7a4e338c9f441086c88a5fc44df28bf57090e2f7c21a632"></a>

### `spendguard.workdone:rollup_workdone`

Source: `src/spendguard/workdone.py:99`

Roll the per-day records up to day | week | month, per project.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Roll the per-day records up to day \| week \| month, per project. |
| by | Any | no | 'day' | Roll the per-day records up to day \| week \| month, per project. |

Returns: `Any`

<a id="core-e59bc32f9ae4540a46d0e05886cb8ce4ca5932fb80adef0de15aed77c1c04e9a"></a>

### `spendguard.workdone:summarize`

Source: `src/spendguard/workdone.py:175`

Generate (or estimate) the scrubbed per-project work summary. Estimate-first: default returns a zero-spend
{projects, est_usd, model}; with run=True it makes the CAGED calls (gate → caps.meta), caches {project: summary}
to ~/.spendguard/work_summaries.json, and returns {summarized, model}. push_workdone attaches the cache.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| since | Any | no | None | Generate (or estimate) the scrubbed per-project work summary. Estimate-first: default returns a zero-spend<br>{projects, est_usd, model}; with run=True it makes the CAGED calls (gate → caps.meta), caches {project: summary}<br>to ~/.spendguard/work_summaries.json, and returns {summarized, model}. push_workdone attaches the cache. |
| run | Any | no | False | Generate (or estimate) the scrubbed per-project work summary. Estimate-first: default returns a zero-spend<br>{projects, est_usd, model}; with run=True it makes the CAGED calls (gate → caps.meta), caches {project: summary}<br>to ~/.spendguard/work_summaries.json, and returns {summarized, model}. push_workdone attaches the cache. |
| model | Any | no | None | Generate (or estimate) the scrubbed per-project work summary. Estimate-first: default returns a zero-spend<br>{projects, est_usd, model}; with run=True it makes the CAGED calls (gate → caps.meta), caches {project: summary}<br>to ~/.spendguard/work_summaries.json, and returns {summarized, model}. push_workdone attaches the cache. |

Returns: `Any`

<a id="core-c74fa4882b1bb1ba92145027d474712bef4341003c76ad9241b691bc89869c35"></a>

### `spendguard:ItemLedger`

Source: `src/spendguard/item_ledger.py:80`

Tracks every requested item to a terminal state, persisting as it goes (append-only, loss-loud).

The backing file is an append-only JSONL holding one row per settled item and one row per attempt round, so
neither answers nor retry budgets are lost to a crash, a spend cap or a restart. Items are identified by a
caller-supplied id that must be STABLE across runs (the same id must name the same item next run, or resume
re-does work / loses track).

Location: pass an explicit `path`, OR a `job` name and the file lands at SPENDGUARD_HOME/item_ledger_<job>.jsonl
(the same HOME every other spendguard adapter persists under). One of the two is required — an item ledger with no
durable file cannot survive the restart it exists to survive.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| requested | Any | yes | required | Source parameter requested at src/spendguard/item_ledger.py:80; see complete callable contract. |
| path | str | no | None | Source parameter path at src/spendguard/item_ledger.py:80; see complete callable contract. |
| job | str | no | None | Source parameter job at src/spendguard/item_ledger.py:80; see complete callable contract. |

Returns: `Any`

<a id="core-6f243e6ee93947337a3de5c0708af43a9403791cad41fde06377898b30e9003e"></a>

### `spendguard:batch_cost`

Source: `src/spendguard/pricing.py:748`

Actual/forecast cost ($) of `in_tok`+`out_tok` via the Batch API (50% off). `provider` pins the vendor
when the model id is bare and several vendors host it (see _vendor_qualified). `cache_creation_tok` optional.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | str | yes | required | Source parameter model at src/spendguard/pricing.py:748; see complete callable contract. |
| in_tok | int | yes | required | Source parameter in_tok at src/spendguard/pricing.py:748; see complete callable contract. |
| out_tok | int | no | 0 | Source parameter out_tok at src/spendguard/pricing.py:748; see complete callable contract. |
| cached_in_tok | int | no | 0 | Source parameter cached_in_tok at src/spendguard/pricing.py:748; see complete callable contract. |
| provider | str | no | None | Source parameter provider at src/spendguard/pricing.py:748; see complete callable contract. |
| cache_creation_tok | int | no | 0 | Source parameter cache_creation_tok at src/spendguard/pricing.py:748; see complete callable contract. |

Returns: `float`

<a id="core-a020cbc8fda01e7960eebf5674f9d43c9fe86873762f8a9304c5a7750f2ac7d9"></a>

### `spendguard:cancel_on_zero_billable`

Source: `src/spendguard/escalation_ladder.py:46`

Run a transport operation and cancel at its soft deadline only with proof of zero billable tokens.

Supported proof contracts: ``subscription`` is always $0; ``metered_streaming`` additionally requires
a provider-event observer in ``zero_billable_proven``. Non-streaming metered and CLI transports can
never prove zero here and therefore run to completion. ``cancel`` must confirm that it stopped the request.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| run | Callable[[], object] | yes | required | Source parameter run at src/spendguard/escalation_ladder.py:46; see complete callable contract. |
| soft_deadline_s | float | yes | required | Source parameter soft_deadline_s at src/spendguard/escalation_ladder.py:46; see complete callable contract. |
| transport | str | yes | required | Source parameter transport at src/spendguard/escalation_ladder.py:46; see complete callable contract. |
| cancel | Callable[[], bool] \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "any"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Callable[[], bool] \| None", "type": "any"} | no | None | Source parameter cancel at src/spendguard/escalation_ladder.py:46; see complete callable contract. |
| zero_billable_proven | Callable[[], bool] \| None {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "any"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Callable[[], bool] \| None", "type": "any"} | no | None | Source parameter zero_billable_proven at src/spendguard/escalation_ladder.py:46; see complete callable contract. |
| provider | Any | no | 'unknown' | Source parameter provider at src/spendguard/escalation_ladder.py:46; see complete callable contract. |
| model | Any | no | 'unknown' | Source parameter model at src/spendguard/escalation_ladder.py:46; see complete callable contract. |
| intent | Any | no | None | Source parameter intent at src/spendguard/escalation_ladder.py:46; see complete callable contract. |

Returns: `Any`

<a id="core-c3d2bdd52d48de18859c4ce1c2c706178dd1248bcaddce83072a170b2ee97c9c"></a>

### `spendguard:check_bulk`

Source: `src/spendguard/bulkgate.py:360`

Call BEFORE a bulk submit. RAISES GateBlocked if this call-class lacks a FRESH estimate+verified-test — UNLESS:
  • it's a PREVIEW (count <= preview_max AND est_usd <= bulk_min_usd) — that IS the allowed test step,
  • mode is `off` (enforcement disabled), or `warn` (logs 'would-block' but allows — the roll-out grace period),
  • force=True or env GATE_FORCE=1 — an explicit, LOGGED human override (never a silent bypass).
Returns the decision string ('preview'|'pass'|'allow:<mode/force>'); raises only in `block` mode without flags.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| sig | Any | yes | required | Source parameter sig at src/spendguard/bulkgate.py:360; see complete callable contract. |
| model | Any | yes | required | Source parameter model at src/spendguard/bulkgate.py:360; see complete callable contract. |
| count | Any | yes | required | Source parameter count at src/spendguard/bulkgate.py:360; see complete callable contract. |
| est_usd | Any | yes | required | Source parameter est_usd at src/spendguard/bulkgate.py:360; see complete callable contract. |
| force | Any | no | False | Source parameter force at src/spendguard/bulkgate.py:360; see complete callable contract. |
| contract | Any | no | None | Source parameter contract at src/spendguard/bulkgate.py:360; see complete callable contract. |
| data_sig | Any | no | None | Source parameter data_sig at src/spendguard/bulkgate.py:360; see complete callable contract. |

Returns: `Any`

<a id="core-46b52fd209ed0cc3759ecbba0a434eeb944749d967e386a813c5dcfc025609ff"></a>

### `spendguard:check_compute`

Source: `src/spendguard/bulkgate.py:971`

REMOTE-COMPUTE (GPU / vast.ai) test-first gate — the same estimate+test rule as check_bulk, on the compute-$
axis. A big/long launch (est_usd > bulk_min_usd) needs a FRESH estimate + a verified SHORT test run (a small/short
instance that proved the workload before scaling fleet×duration) — record_tested after that short run. Composes
with the cap in resources.compute_exceeded. Consumers call this before launching; raises GateBlocked in block mode.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| sig | Any | yes | required | Source parameter sig at src/spendguard/bulkgate.py:971; see complete callable contract. |
| est_usd | Any | yes | required | Source parameter est_usd at src/spendguard/bulkgate.py:971; see complete callable contract. |
| hours | Any | no | None | Source parameter hours at src/spendguard/bulkgate.py:971; see complete callable contract. |
| force | Any | no | False | Source parameter force at src/spendguard/bulkgate.py:971; see complete callable contract. |

Returns: `Any`

<a id="core-a5c0afe9c0cb378e0815f34e17294e35df17b621c6240263bb06c1b1a7a9d573"></a>

### `spendguard:check_realtime`

Source: `src/spendguard/bulkgate.py:1114`

Realtime BURST gate — a LOOP of realtime calls is the discouraged alternative to the Batch API and must obey the
same estimate+test-first rule. Track per-sig calls in a rolling in-process window; the first `preview_max` are the
allowed TEST sample, beyond that the burst needs a FRESH estimate + verified test (delegates to check_bulk on the
cumulative count/$) or it is blocked/warned. Catches the runaway loop (the 47k-call balloon / the $5.61 escalation).
Returns the decision; raises GateBlocked in block mode on an untested burst.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| sig | Any | yes | required | Source parameter sig at src/spendguard/bulkgate.py:1114; see complete callable contract. |
| model | Any | yes | required | Source parameter model at src/spendguard/bulkgate.py:1114; see complete callable contract. |
| est_usd | Any | no | 0.0 | Source parameter est_usd at src/spendguard/bulkgate.py:1114; see complete callable contract. |
| force | Any | no | False | Source parameter force at src/spendguard/bulkgate.py:1114; see complete callable contract. |
| never_block | Any | no | False | Source parameter never_block at src/spendguard/bulkgate.py:1114; see complete callable contract. |
| batch_eligible | Any | no | False | Source parameter batch_eligible at src/spendguard/bulkgate.py:1114; see complete callable contract. |

Returns: `Any`

<a id="core-c9aad690202bd3424a30246ab26d7c752ccbb86224ab6f13c516c558a26f0c44"></a>

### `spendguard:compare_fingerprints`

Source: `src/spendguard/job_fingerprint.py:131`

(plan_changed, [method parts that changed]). A part the recorded fingerprint does not carry counts as CHANGED,
not as matching — an older marker that never recorded the prompt cannot vouch for it.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| recorded | dict | yes | required | Source parameter recorded at src/spendguard/job_fingerprint.py:131; see complete callable contract. |
| current | dict | yes | required | Source parameter current at src/spendguard/job_fingerprint.py:131; see complete callable contract. |

Returns: `Any`

<a id="core-5f78d13b4400dd9116a331347d75e6ff95989fd87f43c9c766b6e79e89da8212"></a>

### `spendguard:compute_job_fingerprint`

Source: `src/spendguard/job_fingerprint.py:76`

The three components a resume depends on, hashed SEPARATELY (plan drift is unrecoverable; method drift is a
judgement — one combined digest would collapse both into "something changed" and force the harshest answer).

`item_ids` must be in the ORDER they will be batched — order is part of the plan, since batch N holds items
[N*pack : (N+1)*pack]. `prompt_sample` is a RENDERED prompt (what the model actually receives, including any
instruction assembled at runtime), not the template source; pass the same representative unit every run so the
digest is stable across runs that differ only in data. A component passed as None is OMITTED rather than hashed as
null — a file stamped retrospectively records only what can be checked (the plan is recoverable from recorded work,
the prompt a past run used is not), and `compare` counts an absent part as CHANGED, so a plan-only marker makes the
next run adjudicate its method rather than silently claim the method matched.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| task | str | yes | required | Source parameter task at src/spendguard/job_fingerprint.py:76; see complete callable contract. |
| pack | int | yes | required | Source parameter pack at src/spendguard/job_fingerprint.py:76; see complete callable contract. |
| item_ids | Any | yes | required | Source parameter item_ids at src/spendguard/job_fingerprint.py:76; see complete callable contract. |
| prompt_sample | str | no | None | Source parameter prompt_sample at src/spendguard/job_fingerprint.py:76; see complete callable contract. |
| model | str | no | None | Source parameter model at src/spendguard/job_fingerprint.py:76; see complete callable contract. |

Returns: `dict`

<a id="core-10eab0d40f277d653a12fc5fec0e329f40168382e68654eea0ef2caa395140da"></a>

### `spendguard:context`

Source: `src/spendguard/calls.py:258`

`with spendguard.context(intent='loinc-typing', chain='run-42'): ...` tags the calls inside.

On exit it emits a per-FLOW spend receipt (what ran · tokens · est→actual · running tally) — see receipt.py.
The receipt is verbosity-gated, goes to stderr, and is fully guarded: it NEVER raises into your code.

`batch_eligible=True` explicitly declares non-interactive, independent one-shot work; the burst guard
offers the existing Batch dry-run/approval path. Model-specific signatures keep distinct models separate.
`contract` gives a REALTIME loop the check a batch already gets: every response is validated against the
declared shape as it is recorded, and the flow reports how many parsed. The batch gate could refuse before
spending; a realtime loop cannot — the money is gone call by call — so this reports EARLY and LOUDLY rather
than gating. The failure it catches is the one that costs: call 1 parses, call 400 comes back with a
sentence before the JSON, and the loop keeps paying. See output_contract for the accepted forms.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Optional[str] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[str]", "type": "any"} | no | None | Source parameter intent at src/spendguard/calls.py:258; see complete callable contract. |
| chain | Optional[str] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[str]", "type": "any"} | no | None | Source parameter chain at src/spendguard/calls.py:258; see complete callable contract. |
| contract | Any | no | None | Source parameter contract at src/spendguard/calls.py:258; see complete callable contract. |
| batch_eligible | Any | no | False | Source parameter batch_eligible at src/spendguard/calls.py:258; see complete callable contract. |

Returns: `Any`

<a id="core-ccc87e67e727901730b31b90826a426ac896675a8f239f4c760d1e471216ea0b"></a>

### `spendguard:estimate_cost`

Source: `src/spendguard/pricing.py:788`

Pre-flight cost ($) for `n` requests of `avg_in`/`avg_out` tokens each.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | str | yes | required | Pre-flight cost ($) for `n` requests of `avg_in`/`avg_out` tokens each. |
| n | int | yes | required | Pre-flight cost ($) for `n` requests of `avg_in`/`avg_out` tokens each. |
| avg_in | int | yes | required | Pre-flight cost ($) for `n` requests of `avg_in`/`avg_out` tokens each. |
| avg_out | int | yes | required | Pre-flight cost ($) for `n` requests of `avg_in`/`avg_out` tokens each. |
| batch | bool | no | True | Pre-flight cost ($) for `n` requests of `avg_in`/`avg_out` tokens each. |

Returns: `float`

<a id="core-2949aa6b7d3fa53339f6b9bbff7a03e6ea9877343472ad6c6c8cefd28643d5e8"></a>

### `spendguard:estimate_job`

Source: `src/spendguard/bulkgate.py:998`

First-class unblock helper (ships IN spendguard so consumers adopt it, not hand-roll it): record the WORST-CASE
estimate. = record_estimate; named to read as step 1 of estimate → test → run.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| sig | Any | yes | required | Source parameter sig at src/spendguard/bulkgate.py:998; see complete callable contract. |
| model | Any | yes | required | Source parameter model at src/spendguard/bulkgate.py:998; see complete callable contract. |
| est_usd | Any | yes | required | Source parameter est_usd at src/spendguard/bulkgate.py:998; see complete callable contract. |
| est_count | Any | yes | required | Source parameter est_count at src/spendguard/bulkgate.py:998; see complete callable contract. |

Returns: `Any`

<a id="core-f808d412b970120f1f3895bf185b3b418f36529625c8f29a2bfdd4c184e42a2f"></a>

### `spendguard:feedback`

Source: `src/spendguard/calls.py:833`

Label a call's quality after the fact (judge verdict, human accept, downstream validation).
Carries a confidence (explicit=1.0, judge=0.95, used=0.6, mined=0.5) the advisor weights by.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| call_id | Optional[str] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[str]", "type": "any"} | yes | required | Source parameter call_id at src/spendguard/calls.py:833; see complete callable contract. |
| ok | bool | no | True | Source parameter ok at src/spendguard/calls.py:833; see complete callable contract. |
| source | str | no | 'explicit' | Source parameter source at src/spendguard/calls.py:833; see complete callable contract. |
| confidence | Optional[float] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "number"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[float]", "type": "any"} | no | None | Source parameter confidence at src/spendguard/calls.py:833; see complete callable contract. |

Returns: `None`

<a id="core-2ec147b5670c7c7a5c4b8ef8e79c4ddd204cf1afcabdfa4ca6a844bc38fc5ffc"></a>

### `spendguard:gate_external`

Source: `src/spendguard/gate.py:696`

FAIL-CLOSED gate for a non-token EXTERNAL call — an MCP tool or a paid non-LLM API — BEFORE it is made. Raises
SpendGateRefused when today's external spend + est_cost would breach the external cap (caps.external.daily /
GATE_EXTERNAL_DAILY): a DELIBERATE refusal that propagates like any spend-gate block, so an instrumented tool/MCP
wrapper that calls this is STOPPED from spending, not merely metered — spendguard's edge over observe-only tools.
No cap set → no-op (external gating is opt-in). GATE_ALLOW=1 or a disabled gate forces through. An internal error
fails OPEN (never break a legitimate call over a bookkeeping hiccup); only the deliberate over-cap decision raises.
Pair with budget.record_external_cost(actual_cost, …) AFTER the call to record what it actually cost.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| est_cost | Any | yes | required | Source parameter est_cost at src/spendguard/gate.py:696; see complete callable contract. |
| service | Any | no | None | Source parameter service at src/spendguard/gate.py:696; see complete callable contract. |

Returns: `Any`

<a id="core-9d11a99806d9cf1a7ffe8ee77adf124b6bbebd44492cbc28aa085416e0fefc04"></a>

### `spendguard:gated_batch`

Source: `src/spendguard/bulkgate.py:1154`

Ordered unblock wrapper so a consumer CAN'T run before estimate+test+eval:
    with bulkgate.gated_batch(sig, model) as job:
        job.note_estimate(worst_case_usd, count)
        sample = job.test(n, run_fn, contract=[...], items=[...])   # <=preview_max sample (allowed), verify shape
        job.eval(bar="what a passing output looks like")            # AGENTIC verdict on that sample (defaults to it)
        job.gated_submit(count, est_usd, submit_fn)      # check_bulk (raises if estimate/test/eval missing) → submit_fn()
a consumer's batch pool becomes a CONSUMER of this, not a reimplementation.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| sig | Any | yes | required | Source parameter sig at src/spendguard/bulkgate.py:1154; see complete callable contract. |
| model | Any | yes | required | Source parameter model at src/spendguard/bulkgate.py:1154; see complete callable contract. |

Returns: `Any`

<a id="core-6611ff5f3ed0b047fdd2a8f63a1c3d23181931dc9e7e27a015ee081b5faca667"></a>

### `spendguard:guard_resume`

Source: `src/spendguard/job_fingerprint.py:141`

Decide whether a resume may proceed. Returns the action taken; RAISES a typed SpendGateRefused on refusal.

Returns one of 'fresh', 'match', 'reuse', 'revalidated', 'rebuy'. Raises JobPlanDrift on plan drift (unless
allow_replan), and JobMethodUndecided on method drift with no handler. Both are SpendGateRefused subclasses, so a
consumer catches them with the same machinery as every other deliberate spend stop (and a CLI wrapper can turn one
into an exit code) — this library never calls sys.exit.

PLAN drift always refuses: the recorded keys describe units that no longer exist, so every one would miss and the
job would re-buy in full. METHOD drift calls `on_method_change(parts, recorded, current, rows)` when supplied; it
must return 'reuse', 'revalidate' or 'rebuy'. Without a handler it RAISES (defaulting to reuse would mix two
methods; defaulting to rebuy would discard paid work over a reworded sentence). 'rebuy' deletes nothing — it
returns the action so the caller can write to a different output and keep both results.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| path | str | yes | required | Source parameter path at src/spendguard/job_fingerprint.py:141; see complete callable contract. |
| fingerprint | dict | yes | required | Source parameter fingerprint at src/spendguard/job_fingerprint.py:141; see complete callable contract. |
| detail | dict | yes | required | Source parameter detail at src/spendguard/job_fingerprint.py:141; see complete callable contract. |
| allow_replan | bool | no | False | Source parameter allow_replan at src/spendguard/job_fingerprint.py:141; see complete callable contract. |
| on_method_change | Any | no | None | Source parameter on_method_change at src/spendguard/job_fingerprint.py:141; see complete callable contract. |

Returns: `str`

<a id="core-f8873328457c38a56be977e0ce2fad39a4f3da94e84f9385c8df1f218f97f24c"></a>

### `spendguard:install`

Source: `src/spendguard/gate.py:2732`

Arm absent SDKs and idempotently patch imported ones; preserve fail-open per-entry warnings.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| cap | 'float \| None' | no | None | Source parameter cap at src/spendguard/gate.py:2732; see complete callable contract. |

Returns: `None`

<a id="core-5b3bae2956fa557a0d37a93f909e696edc4e65bf293ab60bec8ade6ff07a39e5"></a>

### `spendguard:install_bedrock`

Source: `src/spendguard/__init__.py:78`

Capture direct AWS Bedrock (boto3) model-invocation spend. Call once, AFTER `import boto3`. Returns True if
botocore is present and now patched. (Not needed if you call Bedrock through LiteLLM — that's already covered.)

Takes no parameters.

Returns: `bool`

<a id="core-a542cdd1648c36d5a17c728f8d917e99c3fd61ab7e0de493e627f178f13913cc"></a>

### `spendguard:install_litellm`

Source: `src/spendguard/__init__.py:71`

Capture LiteLLM-routed spend (Bedrock, Vertex/Gemini, Cohere, … — anything LiteLLM normalizes) into the same
ledger as the SDK gate. Call once, AFTER `import litellm`. Returns True if litellm is present and now wired.
(The startup gate auto-wires it only if litellm is already imported, so this explicit call is the reliable path.)

Takes no parameters.

Returns: `bool`

<a id="core-b1e78e1db844c6b75f8270e1c36a3f6fb0ec24950b2c1a28c6b0a0d318a3af20"></a>

### `spendguard:install_vertex`

Source: `src/spendguard/__init__.py:84`

Capture direct Google Gemini / Vertex (google-genai) spend. Call once, AFTER importing the SDK. Returns True
if the SDK is present and now patched. (Not needed if you call Gemini through LiteLLM — already covered.)

Takes no parameters.

Returns: `bool`

<a id="core-e4f73e0b02341949055b5b7a2808cdefa24c6001630e267bfb996b97803790f0"></a>

### `spendguard:is_truncated`

Source: `src/spendguard/bulkgate.py:432`

Did the response get CUT OFF at the cap? The API says so: Anthropic stop_reason=='max_tokens', OpenAI
finish_reason=='length'. Belt-and-suspenders: out_tok hitting max_tokens exactly. A fact, not a guess.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| finish_reason | Any | yes | required | Source parameter finish_reason at src/spendguard/bulkgate.py:432; see complete callable contract. |
| out_tok | Any | no | None | Source parameter out_tok at src/spendguard/bulkgate.py:432; see complete callable contract. |
| max_tokens | Any | no | None | Source parameter max_tokens at src/spendguard/bulkgate.py:432; see complete callable contract. |

Returns: `Any`

<a id="core-f2a56626b234885ed491f5e895fbbbc6b3d9aefde96754571595ebab679eedc6"></a>

### `spendguard:maxtokens`

Source: `src/spendguard/bulkgate.py:756`

Data-driven max_tokens bound for a call-class from its OBSERVED output distribution — turns 'guess' into
'measure'. Returns {n, p50, p95, p99, max, recommend, truncations, warn}, recommend = max(p99*1.5, trunc-floor,
reasoning-floor). warn if current_max < p95 (TRUNCATION RISK) or >> p99 (cost-estimate inflation → false cap trips).
For packed calls, feed per-ITEM out_tok.
`model` (optional but important for reasoning models): gate_calls.out_tok records only the VISIBLE answer, NOT the
hidden reasoning tokens that a reasoning model also bills as output — so for such a model the measured percentiles
UNDER-count the real generation at every sample count, not only when cold. When `model` reasons_by_default, the
recommend is therefore FLOORED at the reasoning-inclusive REASONING_OUT_ESTIMATE always, so a visible-answer p99
can never clamp a reasoning reply's budget below what it needs (the gpt-5.5 miss; GUARDRAILS_reasoning_overspend).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| sig | Any | yes | required | Data-driven max_tokens bound for a call-class from its OBSERVED output distribution — turns 'guess' into<br>'measure'. Returns {n, p50, p95, p99, max, recommend, truncations, warn}, recommend = max(p99*1.5, trunc-floor,<br>reasoning-floor). warn if current_max < p95 (TRUNCATION RISK) or >> p99 (cost-estimate inflation → false cap trips).<br>For packed calls, feed per-ITEM out_tok.<br>`model` (optional but important for reasoning models): gate_calls.out_tok records only the VISIBLE answer, NOT the<br>hidden reasoning tokens that a reasoning model also bills as output — so for such a model the measured percentiles<br>UNDER-count the real generation at every sample count, not only when cold. When `model` reasons_by_default, the<br>recommend is therefore FLOORED at the reasoning-inclusive REASONING_OUT_ESTIMATE always, so a visible-answer p99<br>can never clamp a reasoning reply's budget below what it needs (the gpt-5.5 miss; GUARDRAILS_reasoning_overspend). |
| current_max | Any | no | None | Data-driven max_tokens bound for a call-class from its OBSERVED output distribution — turns 'guess' into<br>'measure'. Returns {n, p50, p95, p99, max, recommend, truncations, warn}, recommend = max(p99*1.5, trunc-floor,<br>reasoning-floor). warn if current_max < p95 (TRUNCATION RISK) or >> p99 (cost-estimate inflation → false cap trips).<br>For packed calls, feed per-ITEM out_tok.<br>`model` (optional but important for reasoning models): gate_calls.out_tok records only the VISIBLE answer, NOT the<br>hidden reasoning tokens that a reasoning model also bills as output — so for such a model the measured percentiles<br>UNDER-count the real generation at every sample count, not only when cold. When `model` reasons_by_default, the<br>recommend is therefore FLOORED at the reasoning-inclusive REASONING_OUT_ESTIMATE always, so a visible-answer p99<br>can never clamp a reasoning reply's budget below what it needs (the gpt-5.5 miss; GUARDRAILS_reasoning_overspend). |
| model | Any | no | None | Data-driven max_tokens bound for a call-class from its OBSERVED output distribution — turns 'guess' into<br>'measure'. Returns {n, p50, p95, p99, max, recommend, truncations, warn}, recommend = max(p99*1.5, trunc-floor,<br>reasoning-floor). warn if current_max < p95 (TRUNCATION RISK) or >> p99 (cost-estimate inflation → false cap trips).<br>For packed calls, feed per-ITEM out_tok.<br>`model` (optional but important for reasoning models): gate_calls.out_tok records only the VISIBLE answer, NOT the<br>hidden reasoning tokens that a reasoning model also bills as output — so for such a model the measured percentiles<br>UNDER-count the real generation at every sample count, not only when cold. When `model` reasons_by_default, the<br>recommend is therefore FLOORED at the reasoning-inclusive REASONING_OUT_ESTIMATE always, so a visible-answer p99<br>can never clamp a reasoning reply's budget below what it needs (the gpt-5.5 miss; GUARDRAILS_reasoning_overspend). |

Returns: `Any`

<a id="core-5c9201587e7a686a27675f71bdd4e5cc94218836415308eb98a030b9e8c1ee4c"></a>

### `spendguard:normalize`

Source: `src/spendguard/pricing.py:522`

Map a model id to its canonical priced base. Strips, in order: a trailing date snapshot (-YYYY-MM-DD OpenAI
or -YYYYMMDD Anthropic), a '-latest' alias, and the '-codex' coding variant. Codex variants (gpt-5.5-codex,
gpt-5-codex) bill at their base GPT's published token rates — OpenAI prices codex at the base model — so this
is a verified alias, not a guess; an explicit PRICING entry still wins (see price()).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | str | yes | required | Source parameter model at src/spendguard/pricing.py:522; see complete callable contract. |

Returns: `str`

<a id="core-e8208dfc253ba229a720bb463395c6ec7239adffb6b2fe16754df8714e19ee11"></a>

### `spendguard:note_response`

Source: `src/spendguard/bulkgate.py:533`

Record one response's output size + whether it TRUNCATED, keyed by call-class sig. Truncation → loud warning
(you paid for input + a cut-off output and got corrupt data) + a per-sig count; the sizes feed maxtokens() bounds.
The single place that sees both sides of every call → every repo protected automatically.

`n_items`: how many REQUESTS this `out_tok` covers. maxtokens() learns a PER-REQUEST distribution, so a PACKED or
BATCH call — which carries the WHOLE shard's output in one number — MUST be normalized to per-item HERE
(out_tok / n_items), or a 60-request shard teaches a ~60× inflated 'per-request' p90 that then exceeds the model
ceiling and silently degrades the batch cost estimate to the ceiling (the batch false-refusal bug). A normal
single call passes n_items=1 (unchanged); a batch recorder passes its shard's request count.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| sig | Any | yes | required | Source parameter sig at src/spendguard/bulkgate.py:533; see complete callable contract. |
| model | Any | yes | required | Source parameter model at src/spendguard/bulkgate.py:533; see complete callable contract. |
| out_tok | Any | yes | required | Source parameter out_tok at src/spendguard/bulkgate.py:533; see complete callable contract. |
| max_tokens | Any | no | None | Source parameter max_tokens at src/spendguard/bulkgate.py:533; see complete callable contract. |
| finish_reason | Any | no | None | Source parameter finish_reason at src/spendguard/bulkgate.py:533; see complete callable contract. |
| n_items | Any | no | 1 | Source parameter n_items at src/spendguard/bulkgate.py:533; see complete callable contract. |
| reasoning_tokens | Any | no | None | Source parameter reasoning_tokens at src/spendguard/bulkgate.py:533; see complete callable contract. |
| visible_tokens | Any | no | None | Source parameter visible_tokens at src/spendguard/bulkgate.py:533; see complete callable contract. |

Returns: `Any`

<a id="core-54036d81c75a01380b5bc3b3455ca3a25f749bb4c01d40ff40ea0a9825ff9728"></a>

### `spendguard:on_event`

Source: `src/spendguard/emit.py:31`

Register an in-process callback fn(event: dict). Usable as a decorator.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| fn | 'Callable[[dict], Any]' | yes | required | Source parameter fn at src/spendguard/emit.py:31; see complete callable contract. |

Returns: `'Callable[[dict], Any]'`

<a id="core-a2e565b075231747d08516f48a9169b2add159ced190a99bb2cdf7bd94dcd5c9"></a>

### `spendguard:price`

Source: `src/spendguard/pricing.py:631`

The RAW per-token RATE row for `model` — {in_, out, cached_in, batch_in, batch_out, provider, …} in DOLLARS
PER MILLION TOKENS. This is the rate TABLE, not a cost: do NOT multiply raw token counts by these values (that is
off by 1e6 — a caller printed a $258,249 quote for a $0.26 job doing exactly that). To get DOLLARS, call
`pricing.realtime_cost(model, in_tok, out_tok[, cached_in_tok])` or `pricing.batch_cost(...)` — or
`cost_or_unpriced(...)` for None instead of a raise on an unpriced model. The input-rate key is `in_` (not `in`,
which is a Python keyword). Raises KeyError for an unknown model — never guesses a price.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | str | yes | required | Source parameter model at src/spendguard/pricing.py:631; see complete callable contract. |
| provider | str | no | None | Source parameter provider at src/spendguard/pricing.py:631; see complete callable contract. |

Returns: `dict`

<a id="core-40ebb5ae4573eb761ddce76a00b6f27bfbfa508c7e7823445a20c6aa9a35d0da"></a>

### `spendguard:read_fingerprint`

Source: `src/spendguard/job_fingerprint.py:95`

(fingerprint dict or None, number of work rows) found in an output/checkpoint file.

Returns (None, n) when the file has work rows but no marker — written before this guard existed. The caller must
treat that as UNKNOWN, never as agreement. A pre-existing marker in an older single-hash format is returned as
{"plan": <hash>} so it still compares on the part it covered.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| path | str | yes | required | Source parameter path at src/spendguard/job_fingerprint.py:95; see complete callable contract. |

Returns: `Any`

<a id="core-cac3a41b9172e9a7bb62d70d1e52228abe149706d7191551360842e00f77eb45"></a>

### `spendguard:realtime_cost`

Source: `src/spendguard/pricing.py:779`

Actual/forecast cost ($) of `in_tok`+`out_tok` for a real-time call. `provider` pins the vendor when the
model id is bare and several vendors host it (see _vendor_qualified). `cache_creation_tok` (cache-write tokens,
billed ~1.25x input) is a separate class — pass it when you have it, else it's 0 and unchanged.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| model | str | yes | required | Source parameter model at src/spendguard/pricing.py:779; see complete callable contract. |
| in_tok | int | yes | required | Source parameter in_tok at src/spendguard/pricing.py:779; see complete callable contract. |
| out_tok | int | no | 0 | Source parameter out_tok at src/spendguard/pricing.py:779; see complete callable contract. |
| cached_in_tok | int | no | 0 | Source parameter cached_in_tok at src/spendguard/pricing.py:779; see complete callable contract. |
| provider | str | no | None | Source parameter provider at src/spendguard/pricing.py:779; see complete callable contract. |
| cache_creation_tok | int | no | 0 | Source parameter cache_creation_tok at src/spendguard/pricing.py:779; see complete callable contract. |

Returns: `float`

<a id="core-134f612ebf4c0d0cf2d1dfee925720c0ec1612f8bfa7254cce48c604d768641e"></a>

### `spendguard:record_estimate`

Source: `src/spendguard/bulkgate.py:222`

Record a ZERO-SPEND worst-case estimate for this call-class (sets estimated_at). WORST-CASE incl. any
escalation path — not the cheap path (the nano-only estimate that hid the $5.61 opus run is the cautionary tale).

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| sig | Any | yes | required | Source parameter sig at src/spendguard/bulkgate.py:222; see complete callable contract. |
| model | Any | yes | required | Source parameter model at src/spendguard/bulkgate.py:222; see complete callable contract. |
| est_usd | Any | yes | required | Source parameter est_usd at src/spendguard/bulkgate.py:222; see complete callable contract. |
| est_count | Any | yes | required | Source parameter est_count at src/spendguard/bulkgate.py:222; see complete callable contract. |

Returns: `Any`

<a id="core-e532948b35a88a8c55a1920a4caf8a288e4c78314dcf67497040bd0e70bb04ae"></a>

### `spendguard:record_tested`

Source: `src/spendguard/bulkgate.py:249`

Record a small-sample test AND what it proved: which output CONTRACT the sample was checked against, on
which data (`data_sig`), and how the sample actually did (`result` from output_contract.check).

`verified` used to mean only "a test ran". It now means "the output matched the declared shape", which is
the claim a bulk run is actually relying on.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| sig | Any | yes | required | Source parameter sig at src/spendguard/bulkgate.py:249; see complete callable contract. |
| test_n | Any | yes | required | Source parameter test_n at src/spendguard/bulkgate.py:249; see complete callable contract. |
| verified | Any | no | True | Source parameter verified at src/spendguard/bulkgate.py:249; see complete callable contract. |
| contract | Any | no | None | Source parameter contract at src/spendguard/bulkgate.py:249; see complete callable contract. |
| result | Any | no | None | Source parameter result at src/spendguard/bulkgate.py:249; see complete callable contract. |
| data_sig | Any | no | None | Source parameter data_sig at src/spendguard/bulkgate.py:249; see complete callable contract. |

Returns: `Any`

<a id="core-9f7136dcdfb596387709617bee917a23c22edf689422e792d0ca25f91c7873be"></a>

### `spendguard:refuse_metered`

Source: `src/spendguard/gate.py:175`

`with spendguard.gate.refuse_metered(): ...` — every metered API call ON THIS THREAD inside the block raises
MeteredCallRefused, while $0 subscription lanes still run. For whole-process coverage (all threads / worker pools),
set env SPENDGUARD_REFUSE_METERED=1 instead. Either way, guarantees no metered spend without a cap.

Takes no parameters.

Returns: `Any`

<a id="core-0ac9cd175081fc3e8d787e1be342ee88c5be12fe1613b1449351d0dc9097a94a"></a>

### `spendguard:register`

Source: `src/spendguard/gate.py:2492`

Register a new SDK surface to gate (e.g. a future provider). gate_fn(kw, args) -> None|raise.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| module_path | str | yes | required | Source parameter module_path at src/spendguard/gate.py:2492; see complete callable contract. |
| class_name | str | yes | required | Source parameter class_name at src/spendguard/gate.py:2492; see complete callable contract. |
| method | str | yes | required | Source parameter method at src/spendguard/gate.py:2492; see complete callable contract. |
| gate_fn | Any | yes | required | Source parameter gate_fn at src/spendguard/gate.py:2492; see complete callable contract. |
| is_async | bool | no | False | Source parameter is_async at src/spendguard/gate.py:2492; see complete callable contract. |

Returns: `None`

<a id="core-f0ad74cf6637a87050ae60296fe46b6f663c9988332f94e559ee7fb791fe77b2"></a>

### `spendguard:relative_escalation_gate`

Source: `src/spendguard/escalation_ladder.py:15`

Return the pure estimate-time decision for composed relative and absolute gates.

``baseline`` and ``escalation`` are estimates already produced by spendguard's pricing/expect
machinery; this function deliberately does no token pricing of its own.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| baseline | Any | yes | required | Source parameter baseline at src/spendguard/escalation_ladder.py:15; see complete callable contract. |
| escalation | Any | yes | required | Source parameter escalation at src/spendguard/escalation_ladder.py:15; see complete callable contract. |
| absolute_cap | Any | yes | required | Source parameter absolute_cap at src/spendguard/escalation_ladder.py:15; see complete callable contract. |
| quality_bar_met | Any | no | True | Source parameter quality_bar_met at src/spendguard/escalation_ladder.py:15; see complete callable contract. |
| ratio_limit | Any | no | None | Source parameter ratio_limit at src/spendguard/escalation_ladder.py:15; see complete callable contract. |
| baseline_epsilon | Any | no | None | Source parameter baseline_epsilon at src/spendguard/escalation_ladder.py:15; see complete callable contract. |

Returns: `Any`

<a id="core-e0c930d4b5eb2879addf3ad6ec6383db0ae771a080d2563fce11d39c4d431de1"></a>

### `spendguard:require`

Source: `src/spendguard/gate.py:2860`

FAIL-CLOSED guard — ensure the gate is actually ENFORCING in this interpreter, else raise. Put at the
top of any script that must not spend ungated; it refuses to run if spendguard was bypassed or disabled:

    import spendguard; spendguard.require()      # then your client.batches.create(...) / chat calls

This is the fix for the #1 bypass: running under a python/venv where the gate never auto-installed.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| cap | 'float \| None' | no | None | Source parameter cap at src/spendguard/gate.py:2860; see complete callable contract. |

Returns: `None`

<a id="core-5061da829a33afe56a2d03fea757a7084dc935de9270b871b35ef2ce16818780"></a>

### `spendguard:set_context`

Source: `src/spendguard/calls.py:112`

Merge supplied non-None values into the current thread-local call attribution and request context.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| intent | Optional[str] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[str]", "type": "any"} | no | None | Job-type attribution label. |
| chain | Optional[str] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[str]", "type": "any"} | no | None | Logical call-chain identifier. |
| who | Optional[str] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[str]", "type": "any"} | no | None | Original caller frame propagated across worker threads. |
| root_call | Optional[str] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[str]", "type": "any"} | no | None | Root ledger identifier linking retry rows to their logical call. |
| attempt | Optional[int] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "integer"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[int]", "type": "any"} | no | None | One-based attempt number recorded for retry attribution. |
| fell_from | Optional[str] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[str]", "type": "any"} | no | None | Subscription lane that preceded a metered fallback. |
| fell_reason | Optional[str] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[str]", "type": "any"} | no | None | The failed lane classified miss reason. |
| batch_expected_out | Optional[int] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "integer"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[int]", "type": "any"} | no | None | Declared expected per-request batch output used for provisional booking. |
| call_class | Optional[str] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[str]", "type": "any"} | no | None | Validated call classification from the existing allowed class set. |
| origin_session | Optional[str] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[str]", "type": "any"} | no | None | Originating assistant session identifier. |
| defer_batch_booking | Optional[bool] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "boolean"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[bool]", "type": "any"} | no | None | Defer provisional batch booking in the downstream gate. |
| requested_model | Optional[str] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[str]", "type": "any"} | no | None | Original model before routing substitution. |
| redirect_reason | Optional[str] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[str]", "type": "any"} | no | None | Reason for the model or lane redirect. |
| resolved_lane | Optional[str] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "string"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[str]", "type": "any"} | no | None | Lane chosen by routing. |
| measurement | Optional[bool] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "boolean"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[bool]", "type": "any"} | no | None | Mark the call as a measurement for explicit effort attribution. |
| gen_params | Optional[dict] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "object"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[dict]", "type": "any"} | no | None | Scoped generation controls copied into the call context. |
| batch_eligible | Optional[bool] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "boolean"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[bool]", "type": "any"} | no | None | Declare independent one-shot work eligible for existing batch handling. |
| routing_snapshot | Optional[dict] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "object"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[dict]", "type": "any"} | no | None | Routing provenance snapshot carried to recording. |
| request_schema | Any | no | None | Structured request schema carried through the gate. |
| request_max_tokens | Optional[int] {"alternatives": [{"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "integer"}, {"alternatives": [], "enum": [], "items": null, "properties": [], "python_annotation": null, "type": "null"}], "enum": [], "items": null, "properties": [], "python_annotation": "Optional[int]", "type": "any"} | no | None | Original request output argument retained as request context. |
| request_decomposition | Any | no | None | Request decomposition metadata carried through the gate. |

Returns: `None`

<a id="core-c5253852e0d5836f776d168a4330118aaaa8c9ec643a4a724ad4a9015200503d"></a>

### `spendguard:test_job`

Source: `src/spendguard/bulkgate.py:1004`

Step 2 of estimate → test → run: execute a <= preview_max SAMPLE (the gate always allows it — it IS the
test), CHECK ITS OUTPUT against the declared shape, and record what happened.

    test_job(sig, run_fn, n=5, contract=["patient_id", "findings"], items=pages[:5])

`contract` is checked against EVERY item of the sample (see output_contract) — the failure that matters is
the one at item 400, not item 1. `items` are the sample's INPUTS; their fingerprint is stored so a test on
three toy rows cannot authorize a run over the real corpus.

NO CONTRACT AND NO verify_fn → the test is recorded UNVERIFIED. It used to be recorded as verified ("None →
trust that it ran"), which authorized full batches on a sample that proved only that the API returned
something. The run is still allowed under `warn`/`off` and via GATE_FORCE — but the gate no longer claims a
verification that never happened.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| sig | Any | yes | required | Source parameter sig at src/spendguard/bulkgate.py:1004; see complete callable contract. |
| run_fn | Any | yes | required | Source parameter run_fn at src/spendguard/bulkgate.py:1004; see complete callable contract. |
| n | Any | no | None | Source parameter n at src/spendguard/bulkgate.py:1004; see complete callable contract. |
| verify_fn | Any | no | None | Source parameter verify_fn at src/spendguard/bulkgate.py:1004; see complete callable contract. |
| contract | Any | no | None | Source parameter contract at src/spendguard/bulkgate.py:1004; see complete callable contract. |
| items | Any | no | None | Source parameter items at src/spendguard/bulkgate.py:1004; see complete callable contract. |

Returns: `Any`

<a id="core-74653bbde9b62bcebc0bbc05d3d0e0662bd02ac72f72b90ab9957eba2c4dae7b"></a>

### `spendguard:write_fingerprint`

Source: `src/spendguard/job_fingerprint.py:121`

Append the fingerprint marker, unless an identical one is already there.

| Parameter | Type | Required | Default | Contract |
| --- | --- | --- | --- | --- |
| path | str | yes | required | Source parameter path at src/spendguard/job_fingerprint.py:121; see complete callable contract. |
| fingerprint | dict | yes | required | Source parameter fingerprint at src/spendguard/job_fingerprint.py:121; see complete callable contract. |
| detail | dict | yes | required | Source parameter detail at src/spendguard/job_fingerprint.py:121; see complete callable contract. |

Returns: `None`
