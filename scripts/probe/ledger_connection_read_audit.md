# Part A SELECT inventory (src/spendguard ledger read paths)

Each location names columns or aggregate expressions unless marked `*`; COUNT(*) is an aggregate, not row-wide selection. Dynamic named projections are assembled in ledger.py; no hidden automatic payload selection there. Inventory grounded with symgrep and AST SQL-string extraction plus source reads. Line is SELECT start, rather than a later FROM line.

| File | SELECT lines | Projection / payload |
|---|---|---|
| advise.py | 15 | named metrics |
| brief.py | 24 | named intent |
| budget.py | 289,819 | named id |
| calibrate.py | 165,400,408,446,549,623 | named metrics |
| calls.py | 180,270,282,656,702,721,755,785,822,869 | named metrics |
| calls.py | 673 | named id, output_snip; intentionally reads output payload, LIMIT 10 |
| claudecode.py | 474,652,757,761,793,824,1215 | named metrics |
| cost_integrity.py | 80 | named metrics |
| delegate_router.py | 182,190 | named aggregates |
| effort_titration.py | 251 | named model/count |
| lane_balance.py | 53,1421 | named metrics |
| lane_economics.py | 62,380,394,454 | named metrics |
| lane_value.py | 56 | named metrics |
| ledger.py | 459,528,602,693,731,828,844,863,870,885,909,934 | named columns / dynamic named aggregates; 863 accepts explicit caller-selected columns |
| ledger.py | 477,539,626,659,679 | `SELECT * FROM spend_events`: update, correction clone, get, query, in_category; pulls every payload column |
| mcp_server.py | 336,337,338,339,379,395,397 | named metrics |
| measurement.py | 155 | named model/quality/ts |
| overage.py | 72 | named aggregates |
| prompts.py | 97 | named metrics plus prompt_snip payload, potentially all matching rows |
| realized.py | 61 | named aggregates |
| receipt.py | 276,433,436 | named metrics |
| reconcile_calls.py | 73,77 | named metrics / assembled named sum |
| reliability.py | 291 | named metrics |
| route_realized.py | 36,56,74,87,95,97,102,105,116 | named metrics |

No SELECT * on calls found in src/spendguard. Five SELECT * on spend_events, all ledger.py. Reading full rows is established; causal involvement in the observed daemon CPU remains unmeasured. Column naming alone does not prove a bounded/indexed query.


## Ground and opener audit
All locations below are in src/spendguard unless stated. CLAUDE.md:3–19 requires grounding before edits; this is diagnosis only.
config.py:938–944: “KEYED per subsystem (NOT one globally shared connection) so a nested op across subsystems can never commit/rollback another's in-flight transaction”. _LEDGER_POOL is threading.local at :945; :955–971 caches by key within thread, compares path, and runs connect+tune+ensure on each new/reopened connection. No process-level schema memo exists.
config.py:928–929 executes seven PRAGMAs: busy_timeout=5000; journal_mode=WAL; synchronous=NORMAL; temp_store=MEMORY; cache_size=-64000; mmap_size=268435456; journal_size_limit=67108864. :925 claims “a periodic wal_checkpoint(TRUNCATE) drains it”. Repository Python search finds no executable wal_checkpoint call, only this docstring; external schedules were not audited.
ledger.py:315–325 directly opens/tunes every SpendLedger instance. :341–370 executes DDL, table_info at :348 and :370, index_list :359 and index_info :360 on every instance. budget.py:43–58 caches one such instance per thread.

| Opener | Before returning / ownership |
|---|---|
| batch_tracker.py:73 | pooled batch_jobs; schema :53–70 CREATE tables/index, table_info, conditional ALTER, commit |
| best_value.py:56 | pooled bestvalue_verdict; schema :48–53 CREATE table, commit |
| budget.py:15 | one process singleton; connect+tune+commit; no explicit table read |
| bulkgate.py:78 | pooled bulkgate; schema :50–75 CREATE table, attempted ALTERs, commit |
| bulkgate.py:328 | reuses _gate_db; CREATE tables/indexes, table_info on every invocation |
| callio.py:110 | pooled callio; schema :73–107 CREATE table, table_info, migrations, indexes, commit |
| calls.py:388 | pooled calls; schema :314–385 CREATE table/indexes, table_info, migrations, commit |
| conv.py:244,704 | reuse learn pool; CREATE classification cache tables every invocation |
| effort_titration.py:69 | reuse budget singleton; CREATE table+commit |
| fleet.py:32 | fresh fleet.db; WAL/busy PRAGMAs, CREATE, table_info, migrations, commit |
| guard.py:30,151 | reuse budget singleton; CREATE tables/indexes; decisions additionally table_info/migrations; commit |
| intent_model_prior.py:48 | fresh spend.db; CREATE table; no shared tuning |
| lane_bandit.py:40 | fresh spend.db; CREATE table; no shared tuning |
| lane_queue.py:183 | fresh lane_queue.db via config.fresh_ledger_conn :1025; connect+tune+schema |
| lane_queue.py:165,176 | pooled lane_queue.db key lane_queue; schema :108–143 CREATE, attempted ALTER, indexes; no explicit data SELECT |
| learn.py:69 | pooled learn; schema :15–66 CREATE tables/indexes, table_info/migrations, commit |
| mcp_server.py:308 | fresh spend.db, bare connect before caller's reads |
| measurement.py:19 | fresh measurements.sqlite; CREATE table/indexes+commit |
| models.py:55 | reuse learn pool; CREATE model_facts+commit; returns module |
| provider_tokens.py:92 | reuse budget singleton; CREATE table, table_info/migrations+commit |
| reliability.py:195,308 | reuse budget singleton; CREATE tables+commit; health table_info/migrations |
| semcache.py:45 | pooled semcache; schema :22–42 CREATE table+unique index; legacy duplicate repair DELETE/subquery on index error; commit |
| xp_rate_window.py:112 | pooled xp_rate through ledger_op; schema :35 CREATE table+index |

There are nine production pool keys: eight spend.db (batch_jobs, bestvalue_verdict, bulkgate, callio, calls, learn, semcache, xp_rate), plus lane_queue.db. Thus nine is a potential per-thread upper bound only if every key is touched; budget's extra per-thread SpendLedger and singleton/direct openers are additional. Eight spend.db keys times T fully participating threads gives 8T pool connections, plus T budget ledgers if touched, plus singleton/direct extras. No fixed process count follows from worker count. 36 spend.db + 7 lane_queue.db is compatible with seven queue-participating threads and a subset of spend keys, but exact 36 cannot be reconstructed statically. vendor_call.py:1255 fans to len(vendors); lane_balance.py:1185 fans to n; nested hedge :1125 has two workers.
The pure-PRAGMA/no-read explanation of 36-fd/2-shm is not established: pool creation runs schema callbacks before returning, involving DDL/schema reads on all pool keys and explicit table_info on several. budget singleton and mcp bare opener are exceptions without explicit schema reads. SQLite/VFS may share a WAL-index mapping within a process; Python source does not establish per-fd mapping behavior. lsof shm descriptor count is not a read-count measurement.
36 cache budgets = 2,304,000 KiB = 2.197 GiB; 36 mmap caps = 9 GiB virtual address space. Observed 456 MB resident / 917 MB peak is process-wide and cannot yield true resident bytes per connection or CPU attribution. Caches not reaching nominal limits does not logically prove setup CPU dominance.
Repeated-read mechanisms exist: ledger.py:746–775 invalidates the running total memo using total_changes/data_version and re-sums after changes; calls.py:265–295 uses MAX(rowid) and an incremental rowid cursor. These are source findings, not attribution of the daemon burn.
