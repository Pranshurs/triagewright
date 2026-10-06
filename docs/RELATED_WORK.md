# Related work

Triagewright sits between three groups of existing work. Descriptions below reflect
each project's public repository as of October 2026.

**Agent frameworks and demos.** Orchestration frameworks such as
[LangGraph](https://pypi.org/project/langgraph/) (including its customer-support
tutorial with confirmation interrupts), and vendor customer-service agent demos, show
how to build and orchestrate tool-using agents. Triagewright is not a framework. It is a complete operational case runtime
with its own environment, scored scenarios and failure injection, and it runs without
a model.

**Agent benchmarks.** [tau2-bench](https://github.com/sierra-research/tau2-bench)
evaluates policy-following conversational agents against simulated users;
[AppWorld](https://github.com/StonyBrookNLP/appworld) grades agents on application
state, including collateral damage; [WorkArena](https://github.com/ServiceNow/WorkArena)
and [CRMArena](https://github.com/SalesforceAIResearch/CRMArena) target enterprise
platforms. Those projects measure models. Triagewright measures the *system around*
the model: whether consequential actions wait for an operator, whether a lost response
can cause a duplicate effect, and whether "resolved" is true in the systems of record.
Its scorer works without a model, and its scripted adversarial agents are part of the
test suite.

**Operations and SRE agents.** [HolmesGPT](https://github.com/HolmesGPT/holmesgpt),
[ITBench](https://github.com/itbench-hub/ITBench) and [AIOpsLab](https://github.com/microsoft/AIOpsLab) investigate infrastructure
incidents, usually against live clusters. Triagewright covers business operations,
such as billing, entitlements, provisioning and webhooks, where the actions are
refunds and credits rather than pod restarts. Its environment is a hermetic simulation.

## What Triagewright adds

1. **Hermetic and deterministic.** Clone, run, score: no API key, no cluster, no hosted
   instance.
2. **Faults that matter for writes.** Timeouts after an effect has applied, crashes
   mid-dispatch, transient upstream errors. Recovery uses the operation's idempotency
   key and is scored on the upstream effect log.
3. **Approvals bound to exact operations.** An approval covers one case, account,
   operation and set of arguments. Edits void it; rejections block re-proposal of the
   same action.
4. **Scoring independent of the runtime.** The scorer reads what the systems recorded,
   not what the agent or runtime claimed, and separates blocked attempts from harm. A
   policy-allowed action can still score as harmful.
5. **One authority path.** CLI, HTTP, MCP and the operator console all reach the same
   runner; parity tests hold them to identical decisions and effects.

## Not competing on

Model leaderboards, realism of real third-party infrastructure, general-purpose agent
frameworks, or a commercial support product.
