# Backend `None` Optionality Audit

## 本轮整改

已完成：删除 `run_agent_loop` 的死参数和计划编排的死参数；MCP 传输方式改为入口处恰好一个校验，并传递原始远端工具名；工具治理 PATCH 拒绝空更新；为懒加载模型和 PostgreSQL 引擎补充具体类型注解。

审计范围是当前工作树中的 `athena/**/*.py`，不包含 `tests/`、`.venv/` 和第三方依赖。扫描同时覆盖函数参数、类型注解字段、模块/实例状态和局部变量。结果：

- 297 个函数参数的默认值是 `None`，分布在 150 个函数中。
- 284 个赋值位置把 `None` 作为初始值、清空值或 Pydantic `Field(default=None)`。
- 这些数字包含抽象端口和同一契约的实现重复；不能按出现次数直接判断问题数量。

## 结论

绝大多数 `None` 是有业务语义的：查询过滤器的“未过滤”、错误/结果的“尚未产生”、数据库可空列、可插拔依赖未启用、懒加载资源未初始化，以及 TypedDict/Pydantic 部分响应。建议保留这些可空性，但统一在边界文档中说明“未提供”和“显式清空”的区别。

有 3 个参数目前没有任何运行时用途，应删除或真正接入：

1. `athena/runtime/execution_loop/runner.py:27-28` 的 `run_agent_loop(plan_id, task_id)`。调用方传入它们，但函数只保存 `parent_run_id`，没有把计划/任务 ID写入请求、元数据或执行状态；默认 `None` 只是死参数。
2. `athena/runtime/langgraph_runtime.py:278` 的 `execute_planned_orchestration(stop_signal)`。函数只调用 `start_ready_tasks(plan_id)`，没有读取停止信号；调用方传入的取消事件会被丢弃。应删除参数，或把它传给任务执行器并实现取消语义。

还有一个与可空默认值直接相关的 MCP 风险：`MCPTool.__init__(remote_name=None)` 会在 `athena/core/tools/tool_definitions.py:233` 回退到本地 `name`。但 `MCPToolAdapter` 在 `athena/core/tools/mcp/adapter.py:164` 传入的是 `tool_def.get("remote_name")`，标准 MCP 列表通常只有 `name`，结果就是 `remote_name=None`，随后使用带 `mcp_` 前缀的本地名调用远端。这里应传原始 `tool_name`，或让 MCPTool 的 `remote_name` 成为必填参数；保留 None 作为兼容回退时必须补测试。

以下是设计上合理、但应保持明确约束的重点：

- `MCPClient`/`MCPToolAdapter` 的 `server_command` 和 `server_url`（`athena/core/tools/mcp/client.py:61-62`、`athena/core/tools/mcp/adapter.py:78-80`）必须“恰好一个”；当前在 `connect()` 才报错，建议在构造或 `register_server()` 入口校验，同时拒绝两者都传。
- `FileRepository.create_attachment()` 的 `session_id`、`knowledge_base_id`（`athena/infrastructure/postgre/repositories/file_repository.py:148-150`）各自可空是正确的，因为附件必须且只能属于两者之一；现有 XOR 校验应保留并在类型契约中写明。
- `ToolCatalogService.update_governance()` 和 `ToolRepository.update()` 的三个治理字段允许全为 `None`，表示 PATCH 的“未修改”；当前全空请求会成功但不改变数据，若 API 不需要空 PATCH，应增加至少一个字段的校验。
- `ContextAcquisitionService` 的四个 provider、`TaskExecutor` 的 event/checkpointer/approval store、`FileIntelligenceRuntime` 的 token counter/reranker/graph indexer 等依赖可空，是功能降级和测试替身入口；不要改成必填，除非部署配置已强制启用对应能力。
- `PathSecurityFilter` 的目录/扩展名、检索和列表接口的过滤参数、MCP/env/metadata/locator 等容器参数的 `None` 表示“不限制/未提供”，用空容器替换会改变语义，不能机械改成 `{}` 或 `[]`。
- `run_id`、`session_id`、`message_id`、`agent_run_id` 等检索追踪字段在 provider/trace writer 中可空是合理的，因为调用既可能来自 HTTP 请求，也可能来自后台任务；若某条链路必须可追踪，应在该链路入口单独设为必填。

另有两个变量值得补强类型而不是取消 `None`：`athena/core/files/reranking.py:32` 的 `_model = None` 是异步懒加载模型，建议标注 `CrossEncoder | None`；`athena/infrastructure/postgre/engine.py:12` 的 `_engine = None` 也应标注具体 AsyncEngine 类型。它们的初始 `None` 是生命周期必需状态。

## 函数参数完整清单

下面按文件列出全部函数中“默认值为 `None`”的参数；同一行多个参数表示同一函数。括号内只列参数名，类型可直接从对应源码行读取。

### Agents, contracts, planning

- `agents/root/graph.py`: `build_runtime_root_graph(checkpointer)`; `invoke_root_graph(attachment_ids, stop_signal, resume_value)`; `build_root_graph(handle_request, validate_plan_submission, persist_plan, load_results, finalize_response, checkpointer)`。
- `agents/worker/agent.py`: `GeneralWorkerAgent.__init__(handler)`。
- `agents/worker/graph.py`: `build_worker_graph(execute_tool, execute_worker, checkpointer)`。
- `agents/worker/registry.py`: `WorkerAgentRegistry.__init__(agents)`。
- `contracts/errors.py`: `ExecutionError.from_exception(phase, category, stack)`; `from_legacy_value(phase)`。
- `contracts/ports.py`: `complete_command(result, error)`; `update_run_status(error)`; `resolve_approval_batch(expected_run_id)`。
- `contracts/tool_policy.py`: `ToolPolicy.for_worker(requested_tools)`。
- `planning/materializer.py`: `PlanMaterializer.__init__(registered_worker_types)`。
- `planning/validator.py`: `PlanValidator.__init__(max_parallelism)`。

### Core files, retrieval, sandbox, memory

- `core/compression/compressor.py`: `compress(session_id)`。
- `core/files/adapters/pdf.py`: `_extract(settings)`。
- `core/files/ingestion.py`: `FileIngestionService.__init__(graph_indexer)`; `parse_attachment(run_id)`。
- `core/files/ports.py`: `FileVectorStore.query(where)`。
- `core/files/reranking.py`: `CrossEncoderFileReranker.__init__(device)`。
- `core/files/retrieval_pipeline.py`: `RetrievalTraceRecorder.complete(error_message)`; `record(error_message)`; `HybridCandidatePipeline.__init__(reranker)`; `fuse(source_title, document_version)`。
- `core/files/runtime.py`: `FileIntelligenceRuntime.__init__(file_token_counter, reranker, graph_indexer)`; `_complete_retrieval_trace(error_message)`; `parse_attachment(run_id)`; `read_file(locator)`; `search_file(agent_run_id, message_id)`; `search_knowledge(knowledge_base_ids, session_id, agent_run_id, message_id)`; `_fuse_file_results(source_title, document_version)`; `emit_attachment(run_id)`。
- `core/graph/ports.py`: `GraphStore.delete_document_graph(document_version)`。
- `core/harness/error_handler.py`: `CircuitBreaker.reset(tool_name)`; `FallbackRoute.__init__(param_transform, alternate_tool)`; `ToolErrorHandler.__init__(circuit_breaker)`; `register_fallback(param_transform, alternate_tool)`。
- `core/harness/execution_support.py`: `ExecutionSupport.__init__(error_handler, tool_timeout, llm_timeout)`; `_execute_tool_calls(approval_decisions)`; `_execute_single_tool(approval_id, approval_decision)`; `_emit_llm_call_end(tool_calls)`。
- `core/harness/turn_executor.py`: `_llm_outcome(tool_calls, error, error_detail, plan_request, retry_feedback)`; `run_llm_turn(parent_run_id, tool_names, retry_feedback)`; `_compress_messages(retry_feedback)`; `_failed_llm_outcome(failure_detail, failure_exception, business_reason, feedback)`; `execute_tool_call(parent_run_id, tool_names, approval_decisions)`; `finish_execution(error_detail, parent_run_id)`。
- `core/llm/provider.py`: `LLMProvider.__init__(retry_manager)`; `_invoke_with_retry(fallback_invoke)`; `from_config(retry_settings)`。
- `core/llm/retry.py`: `retry_with_backoff(retry_config, error_category, on_retry)`; `LLMRetryManager.__init__(retry_config)`; `execute_with_retry(on_retry, fallback_invoke)`。
- `core/memory/candidate_resolver.py`: `resolve(related_memories)`。
- `core/memory/long_term_memory.py`: `search(where)`; `keyword_search(where)`; `list_memories(session_id)`; `revise_memory(metadata_overrides, operation_key)`; `set_memory_validity(valid_until)`。
- `core/memory/ports.py`: `MemoryGraphStore.add_relation(metadata)`; `MemoryVectorStore.update(content, metadata)`。
- `core/memory/retrieval.py`: `MemoryRetrievalService.__init__(trace_writer, reranker)`; `_complete_trace(error_message)`; `retrieve(filter_params, session_id, agent_run_id, message_id)`。
- `core/memory/write_workflow.py`: `FactMemoryWriteWorkflow.__init__(trigger)`。
- `core/retrieval/ports.py`: `RetrievalTraceWriter.complete_run(duration_ms, error_message)`; `RetrievalTraceReader.list_runs(scope, status, session_id, agent_run_id, query)`。
- `core/sandbox/ports.py`: `SandboxRunner.build_mcp_process(env)`。
- `core/security/path_filter.py`: `PathSecurityFilter.__init__(allowed_dirs, blocked_dirs, allowed_extensions)`。
- `core/tools/builtin/shell_tools.py`: `exec_shell(cwd)`。
- `core/tools/providers/files.py`: generated `read_file(locator)` tool。

### Tools and gateway

- `core/tools/catalog.py`: `ToolConfigRepository.upsert(server_name, remote_name, parameters)`; `update(risk_level, require_approval, enabled)`; `ToolCatalogService.reconcile(names, registrations)`; `update_governance(risk_level, require_approval, enabled)`。
- `core/tools/manager.py`: `UnifiedToolManager.__init__(tool_runtime)`; `update_tool_config(risk_level, require_approval, enabled)`; `call_tool(plan_id, task_id, worker_run_id, execution_timeout, approval_decision, on_execution_start)`; `get_langchain_tools(names)`。
- `core/tools/mcp/adapter.py`: `MCPToolAdapter.__init__(sandbox_runner, sandbox_workspace_root, sandbox_image)`; `register_server(server_command, server_url, env, sandbox_image)`。
- `core/tools/mcp/client.py`: `MCPClient.__init__(server_command, server_url, env, sandbox_runner, sandbox_image, sandbox_workspace)`。
- `core/tools/tool_definitions.py`: `NativeTool.__init__(parameters)`; `MCPTool.__init__(remote_name)`。
- `gateway/approval.py`: `request_approval(plan_id, task_id, worker_thread_id, approval_id, approval_batch_id)`。
- `gateway/routes/approval.py`: `list_pending_approvals(session_id)`; `list_approval_logs(session_id)`。
- `gateway/routes/events.py`: `_format_sse_event(event_type, session_seq)`。
- `gateway/routes/memory.py`: `list_memories(session_id)`。
- `gateway/routes/retrieval.py`: `list_retrieval_runs(scope, status, session_id, agent_run_id, q)`。
- `gateway/routes/sessions.py`: `list_session_messages(limit)`; `cancel_run(run_id)`。

### Infrastructure, observability, runtime

- `infrastructure/neo4j/graph_store.py`: `add_relation(metadata)`; `delete_document_graph(document_version)`。
- `infrastructure/pgvector/file_vector_store.py`: `query(where)`。
- `infrastructure/pgvector/memory_vector_store.py`: `update(content, metadata)`。
- `infrastructure/postgre/repositories/agent_store.py`: `AgentStore.__init__(transport, command_notifier)`; `update_run_status(error)`; `complete_command(result, error)`; `list_events_between(upto)`; `create_approval(plan_id, task_id, worker_thread_id, approval_batch_id)`; `resolve_approval_batch(expected_run_id)`; `list_pending_approvals(session_id)`; `list_approval_history(session_id)`。
- `infrastructure/postgre/repositories/file_repository.py`: `create_attachment(session_id, knowledge_base_id, message_id, logical_document_id)`; `get_attachment(session_id)`; `get_chunks_by_ids(knowledge_base_ids)`; `put_artifact(metadata)`。
- `infrastructure/postgre/repositories/knowledge_document_job_repository.py`: `_mark(result, error, available_at)`。
- `infrastructure/postgre/repositories/message_repository.py`: `get_by_session(limit)`。
- `infrastructure/postgre/repositories/orchestration_repository.py`: `_finish_task(output, error)`; `finish_task(output, error)`。
- `infrastructure/postgre/repositories/retrieval_repository.py`: `complete_run(duration_ms, error_message)`; `list_runs(scope, status, session_id, agent_run_id, query)`; `ranked_source_ids(limit)`。
- `infrastructure/postgre/repositories/session_repository.py`: `update(status, run_id, title)`。
- `infrastructure/postgre/repositories/tool_call_repository.py`: `query(status)`。
- `infrastructure/postgre/repositories/tool_repository.py`: `upsert(server_name, remote_name, parameters)`; `update(risk_level, require_approval, enabled)`。
- `infrastructure/sandbox/docker_runner.py`: `build_mcp_process(env)`; `_docker_env(extra)`; `_result(exit_code, error)`。
- `observability/langsmith.py`: `_new_run(run_id, parent, metadata, tags)`; `start_trace(metadata, tags)`; `trace_span(inputs, run_id, metadata, tags)`; `finish_span(outputs, error)`。
- `runtime/cancellation.py`: `CancellationRegistry.unregister(event)`。
- `runtime/command_consumer.py`: `CommandConsumer.__init__(notifier, cancellation_registry, sandbox_runner, task_executor, orchestration_repository)`。
- `runtime/context/providers/file.py`: `acquire(agent_run_id, message_id)`。
- `runtime/context/providers/graph.py`: `GraphContextProvider.__init__(trace_writer)`; `acquire(agent_run_id, message_id)`。
- `runtime/context/providers/knowledge.py`: `acquire(agent_run_id, message_id)`。
- `runtime/context/providers/memory.py`: `acquire(agent_run_id, message_id)`。
- `runtime/context/service.py`: `_ContextProvider.acquire(agent_run_id, message_id)`; `ContextAcquisitionService.__init__(memory_provider, knowledge_provider, file_provider, graph_provider, token_counter, trace_writer)`; `acquire(agent_run_id, message_id)`。
- `runtime/execution_loop/graph.py`: `build_agent_loop(checkpointer)`。
- `runtime/execution_loop/runner.py`: `run_agent_loop(stop_signal, parent_run_id, plan_id, task_id)`。
- `runtime/langgraph_runtime.py`: `LangGraphRuntime.__init__(checkpointer)`; `execute_planned_orchestration(stop_signal)`; `build_system_prompt(task_spec, context_bundle)`。
- `runtime/node_events.py`: `_publish_node_event(duration_ms, error_detail)`。
- `runtime/processor_lifecycle.py`: `run_with_lifecycle(inspect, pre_process, post_process)`。
- `runtime/services/agent_execution_service.py`: `_build_system_prompt(task_spec, context_bundle)`。
- `runtime/state.py`: `derive_execution_state(derived)`。
- `runtime/stream_coalescer.py`: `StreamCoalescer.__init__(message_id)`。
- `runtime/task_executor.py`: `TaskExecutor.__init__(context_assembler, event_manager, checkpointer, approval_store)`。
- `runtime/task_understanding/service.py`: `understand(history, attachment_refs, knowledge_bases)`。
- `utils/logging.py`: `get_logger(name)`。
- `utils/message_conversion.py`: `format_messages_brief(role_labels)`。

## 变量/字段清单及必要性

### 应保留的持久化/API/领域可空字段

- `models/file.py`, `models/message.py`, `models/session.py`, `models/tool.py`, `models/mcp.py`：附件归属、删除时间、工具调用输出/错误/完成时间、会话运行与压缩游标、MCP 删除时间等数据库可空列；`None` 表示未关联、未完成或未删除。
- `models/json_models.py`：`JsonSchema`、`FileLocator`、`FileMetadata` 的大量定位和统计字段是按文件类型/解析器部分产生的可选信息；不能用零值替代，否则会把“未知”误当成真实页码/行号。
- `contracts/events.py`, `contracts/target.py`, `contracts/commands.py`, `contracts/errors.py`：事件、计划任务、命令和结构化错误是分阶段构建的，响应/错误/审批/父运行等字段天然部分存在。
- `core/files/contracts.py`, `core/retrieval/contracts.py`, `core/memory/contracts.py`, `core/memory/retrieval.py`, `planning/dag.py`, `planning/models.py`, `runtime/execution_loop/results.py`, `runtime/processor_lifecycle.py`, `runtime/task_understanding/contracts.py`：候选评分、排名、过滤原因、失败详情、澄清问题和生命周期结果只在对应阶段产生，保留 `None` 能表达尚未计算。
- `gateway/routes/schemas.py`, `gateway/routes/mcp.py`, `gateway/routes/providers.py`, `gateway/routes/tools.py`, `gateway/routes/memory.py`：API 响应和 PATCH/过滤输入的可选字段对应 HTTP 的部分响应与未筛选状态。

### 应保留的生命周期/懒加载变量

- `infrastructure/postgre/engine.py`: `_engine`, `_session_factory` 在连接前和关闭后为 `None`，`get_session()` 已有初始化保护。
- `core/tools/mcp/client.py`: `_session_task`, `_session`, `_ready`, `_stop`, `_connect_error` 随连接生命周期创建和清理。
- `core/llm/tokens.py`, `infrastructure/embedding/provider.py`, `core/files/reranking.py`: tokenizer、embedding function、reranker model 懒加载前为空。
- `core/security/path_filter.py`: `_ALLOWED_EXTENSIONS` 和 `_default_filter` 表示未配置全局扩展限制/尚未创建单例。
- `core/harness/execution_support.py`, `core/harness/turn_executor.py`, `core/llm/retry.py`, `core/tools/manager.py`, `runtime/command_consumer.py`, `runtime/task_executor.py`, `runtime/langgraph_runtime.py`, `core/files/knowledge_document_worker.py`, `core/memory/write_job_worker.py`：停止事件、流、token、异常、后台任务和批次只在相应阶段存在。

### 不应机械改成非空的组合约束

`MCP server_command/server_url`、附件 `session_id/knowledge_base_id`、工具治理 PATCH 字段、检索 `where/filter_params`、沙箱 env、追踪 metadata/tags、上下文 provider 和 checkpointer 是“可选能力”或“部分更新”接口。应通过 XOR、至少一个字段、或能力启用时必填等组合校验表达约束，而不是把每个成员都改成必填。

### 赋值位置逐项索引

以下是扫描到的全部 `= None`/`Field(default=None)` 赋值位置。重复出现的同名变量表示初始化和后续清理，不是多个独立字段。

- `agents/root/graph.py`: `snapshot` L124、L130。
- `config/settings.py`: `file_rerank_device` L201。
- `container.py`: `sandbox_runner` L49、`workspace_manager` L50。
- `contracts/commands.py`: `run_id` L29。
- `contracts/errors.py`: `phase` L32、`category` L33、`stack` L34。
- `contracts/events.py`: `session_seq` L71、`run_id` L76、`message_id` L77、`attachment_id` L78、`stream_id` L79、`stream_type` L80、`chunk_id` L81、`parent_run_id` L83、`transition_id` L84。
- `contracts/target.py`: `plan_id` L62、`task_id` L63、`worker_thread_id` L64、`approval_id` L65、`decision` L66、`wait_generation` L67、`run_id` L71、`plan_id` L72、`task_id` L73、`status` L74、`worker_type` L75、`execution_generation` L76、`error` L77、`user_id` L83、`response` L86、`error` L87、`root_wait_checkpoint_id` L94、`root_wait_armed_at` L95。
- `core/compression/compressor.py`: `current_run_id` L271。
- `core/files/adapters/word.py`: `width` L44、`height` L45。
- `core/files/contracts.py`: `source_title` L17、`document_version` L18、`keyword_score` L21、`vector_score` L22、`fused_score` L23、`rerank_score` L24、`keyword_rank` L25、`vector_rank` L26、`fused_rank` L27、`rerank_rank` L28、`filter_reason` L29、`retrieval_run_id` L31。
- `core/files/extraction.py`: `block_id` L62。
- `core/files/knowledge_document_worker.py`: `self._task` L48、L84。
- `core/files/reranking.py`: `self._model` L32。
- `core/files/runtime.py`: `vector_error` L673。
- `core/harness/execution_support.py`: `self._stop_signal` L65、`self._allowed_tool_names` L66、`self._parent_run_id` L67、`self._message_id` L68、`self._answer_stream_id` L69、`self._answer_stream` L70、`self._plan_id` L73、`self._task_id` L74、`execution_started_at` L373、`error_message` L459/L463、`raw_output` L464/L479、`last_error` L513、`last_stack` L514。
- `core/harness/turn_executor.py`: `error` L59、`error_detail` L60、`retry_feedback` L63、`plan_request` L72。
- `core/llm/retry.py`: `result` L195、`error` L196、`last_error` L263、`trace` L269。
- `core/llm/tokens.py`: `self._encoding` L140、L161、L165。
- `core/memory/contracts.py`: `user_message_id` L23、`assistant_message_id` L24、`target_memory_id` L64、`final_content` L65、`relation_type` L67、`session_id` L85、`agent_run_id` L86、`message_id` L87。
- `core/memory/retrieval.py`: `native_score` L41、`fused_score` L42、`rerank_score` L43、`native_rank` L44、`fused_rank` L45、`retrieval_run_id` L46、`filter_reason` L47、`run_id` L151。
- `core/memory/write_job_worker.py`: `self._task` L24、L40。
- `core/retrieval/contracts.py`: `index_generation` L16、`session_id` L17、`agent_run_id` L18、`message_id` L19、`native_rank` L30、`native_score` L31、`fused_rank` L32、`fused_score` L33、`rerank_rank` L34、`rerank_score` L35、`logical_source_id` L36、`revision_id` L37、`filter_reason` L38、`content_preview` L41、`source_title` L42。
- `core/retrieval/evaluation.py`: `first_rank` L69。
- `core/sandbox/models.py`: `error_code` L50。
- `core/security/path_filter.py`: `_ALLOWED_EXTENSIONS` L45、`_default_filter` L193。
- `core/tools/manager.py`: `token` L288、`runtime_token` L289、`trace` L309。
- `core/tools/mcp/client.py`: `self._session_task` L101、L189、L256、L274；`self._session` L102、L255；`self._ready` L103；`self._stop` L104；`self._connect_error` L105、L180、L194。
- `core/tools/spec.py`: `plan_id` L41、`task_id` L42、`worker_run_id` L43、`_current_context` L56、`_current_runtime` L59、`parameters` L135。
- `gateway/approval.py`: `record` L75。
- `gateway/routes/mcp.py`: `error` L36、L46。
- `gateway/routes/memory.py`: `where` L26。
- `gateway/routes/providers.py`: `latency_ms` L46、`error` L47。
- `gateway/routes/schemas.py`: `error` L27、`run_id` L36、L47、L56、`result` L60、`error` L61。
- `gateway/routes/sessions.py`: `error_detail` L326。
- `gateway/routes/tools.py`: `last_called_at` L32、`enabled` L38、`risk_level` L39、`require_approval` L40。
- `infrastructure/embedding/provider.py`: `self._function` L56。
- `infrastructure/neo4j/graph_store.py`: `self._driver` L120。
- `infrastructure/postgre/engine.py`: `_engine` L12、L149；`_session_factory` L13、L150。
- `infrastructure/postgre/repositories/memory_repository.py`: `row.deleted_time` L571。
- `infrastructure/postgre/repositories/orchestration_repository.py`: `plan` L654。
- `models/file.py`: `logical_document_id` L80、`session_id` L82、`knowledge_base_id` L83、`message_id` L84、`adapter_name` L90、`adapter_version` L91、`error_message` L95、`deleted_time` L98、`native_score` L159。
- `models/json_models.py`: `message` L27、`message_id` L28、`content` L30、`metadata` L31、`plan_id` L32、`task_id` L33、`worker_run_id` L34、`worker_thread_id` L35、`approval_id` L36、`decision` L37、`wait_generation` L38、`approval_batch_id` L39、`decisions` L40、`type` L64、`title` L65、`description` L66、`items` L69、`enum` L70、`default` L71、`additional_properties` L72、`path` L80、`page` L81、`page_start` L82、`page_end` L83、`block_start` L84、`block_end` L85、`paragraph` L86、`sheet` L87、`row` L88、`column` L89、`start_line` L90、`end_line` L91、`char_start` L92、`char_end` L93、`image` L94、`kind` L100、`language` L101、`line_count` L103、`lines` L104、`row_count` L105、`pages` L107、`paragraphs` L108、`width` L109、`height` L110、`files` L111、`chunk_count` L112、`symbol_count` L113、`dependency_count` L114、`content` L122、`storage_key` L123。
- `models/mcp.py`: `image_id` L22、`deleted_time` L33。
- `models/message.py`: `tool_call_id` L30、`run_id` L31、`tool_call_record_id` L32、`tool_name` L33、`message_type` L34。
- `models/session.py`: `run_id` L36、`compression_summary` L40、`last_compressed_message_id` L41、`last_summarized_message_id` L42。
- `models/tool.py`: `output` L43、`error` L44、`step_id` L65、`run_id` L66、`approval_id` L68、`raw_output` L72、`completed_at` L75、`error_message` L77、`error_stack` L78、`server_name` L89、`remote_name` L90。
- `observability/langsmith.py`: `_current_run` L23。
- `planning/dag.py`: `output` L18、`error` L19。
- `planning/models.py`: `error` L47。
- `runtime/command_consumer.py`: `self._task` L72、L112、`plan` L219、`plan_id` L403。
- `runtime/context/providers/graph.py`: `run_id` L53。
- `runtime/execution_loop/results.py`: `error` L17、`error_detail` L18。
- `runtime/langgraph_runtime.py`: `self._fact_memory_write_workflow` L180。
- `runtime/node_events.py`: `trace` L58。
- `runtime/processor_lifecycle.py`: `result` L32、L42、`resume_from` L33、`reason` L34、`error` L43。
- `runtime/task_executor.py`: `batch` L245。
- `runtime/task_understanding/contracts.py`: `topic` L29、`duration_minutes` L30、`difficulty` L31、`item_count` L32、`audience` L33、`content_type` L41、`memory` L49、`knowledge` L50、`graph` L51、`file` L52、`clarification_question` L68、`memory_query` L90、`knowledge_query` L91、`graph_query` L92、`file_query` L93、`source_id` L110、`retrieval_run_id` L111、`title` L112、`score` L114、`error_message` L127。

### 必须传参但类型仍允许 `None`

这 62 个参数没有 `= None` 默认值，调用方必须显式传入，但类型仍允许 `None`。它们是端口契约、内部阶段结果或需要保留“无值”语义的参数，不能误归入“可省略参数”。

- `core/compression/compressor.py`: `_get_incremental_messages(session_id)`、`_update_last_compressed_id(session_id)`。
- `core/files/retrieval_pipeline.py`: `RetrievalTraceRecorder.__init__(writer)`、`complete(run_id)`。
- `core/files/runtime.py`: `_complete_retrieval_trace(run_id)`。
- `core/graph/ports.py`: `GraphStore.search_paths(knowledge_base_ids)`。
- `core/harness/execution_support.py`: `_emit_tool_call_end(output, error)`。
- `core/harness/turn_executor.py`: `_configure_context(message_id, parent_run_id, tool_names)`、`_configure_tool_context(message_id, parent_run_id, tool_names)`、`_get_llm_tools(tool_names, parent_run_id)`、`_collect_llm_response(tool_names)`、`execute_tool_call(message_id)`、`finish_execution(message_id, error)`。
- `core/memory/ports.py`: `MemoryRepository.keyword_search(where)`、`set_validity(valid_until, last_observed_at)`、`MemoryVectorStore.query(where)`。
- `core/memory/retrieval.py`: `_complete_trace(run_id)`、`_vector_search(filter_params)`、`_keyword_search(filter_params)`。
- `core/memory/write_job_worker.py`: `MemoryWriteJobWorker.__init__(workflow)`。
- `core/sandbox/workspace.py`: `WorkspaceManager.resolve_cwd(cwd)`。
- `core/tools/spec.py`: `reset_tool_context(token)`、`reset_tool_runtime(token)`。
- `infrastructure/neo4j/graph_store.py`: `Neo4jGraphStore.search_paths(knowledge_base_ids)`。
- `infrastructure/pgvector/memory_vector_store.py`: `MemoryPgVectorStore.query(where)`。
- `infrastructure/postgre/repositories/memory_job_repository.py`: `_mark(error, available)`。
- `infrastructure/postgre/repositories/memory_repository.py`: `keyword_search(where)`、`set_validity(valid_until, last_observed_at)`。
- `infrastructure/postgre/repositories/orchestration_repository.py`: `_load(value)`。
- `infrastructure/postgre/repositories/repository_utils.py`: `_json_loads(value)`、`_json_loads_model(value)`。
- `infrastructure/postgre/repositories/session_repository.py`: `update(compression_summary, last_compressed_message_id, last_summarized_message_id)`。
- `observability/langsmith.py`: `_metadata(metadata)`、`finish_span(run)`。
- `runtime/context/providers/graph.py`: `_to_context_item(run_id)`、`_record_trace(run_id, error_message)`。
- `runtime/execution_loop/finish.py`: `finish_execution(message_id, error, error_detail)`。
- `runtime/execution_loop/tool_call.py`: `tool_call(message_id)`。
- `runtime/node_events.py`: `instrument_graph_node(event_publisher)`、`_invoke_node(config)`、`_resolve_execution_identity(config)`、`_publish_node_event(run_id, parent_run_id)`。
- `runtime/state.py`: `split_execution_state(state)`、`derive_execution_state(recoverable)`、`merge_execution_state(current, update)`。

## 建议整改顺序

1. 先删除 `run_agent_loop` 的 `plan_id/task_id`，并决定 `execute_planned_orchestration.stop_signal` 是删除还是实现取消传播；为这两个行为补调用链测试。
2. 在 MCP 注册入口加入 `server_command`/`server_url` 的恰好一个校验；在工具治理 API 对空 PATCH 明确返回 400 或保留幂等成功并写文档。
3. 给 `_engine`、`CrossEncoderFileReranker._model` 和其他惰性资源补具体类型注解，保留初始化 `None`。
4. 继续保留过滤、错误、状态和数据库字段的 `None`；对“未提供”和“清空”的 API 语义需要区分时，引入显式哨兵或 PATCH 模型，而不是依赖 `None` 猜测。
