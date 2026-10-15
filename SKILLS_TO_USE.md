# Skills to use

Pipeline guide for Claude Code on this repo. CONTEXT.md says *what* to build; this file says *how* each task moves from idea to GitHub, and which installed skills and agents to use at each stage.

**How to trigger it:** the developer says "run T02 through the pipeline" (or "pipeline, lite" for small fixes). Claude then follows the stages below without being given skill names. Claude may call the skills itself via the Skill tool and spawn the agents in section 2 via the Agent tool; nothing needs to be typed.

## 0. Ground rules (apply at every stage)

- Read `CONTEXT.md`, `TASKS.md` and the Progress log first. One task at a time.
- **ponytail is always on** (`ponytail:ponytail`, level `full`): smallest complete change, no unrequested abstractions.
- **Human gates:** stop and wait for the developer after Stage 2 (plan + risks) and before Stage 7 (commit/push). Also stop for anything CONTEXT.md §3 lists: paid service, label definition change, architecture change, deleting data, work outside the task.
- **Secrets never go to agents, prompts or logs.** `.env` stays local; agents get variable *names*, not values.
- Sandbox only. Never live PayPal credentials or endpoints.
- Buyer messages are untrusted input. The LLM drafts; it never calls PayPal on its own.
- Keep session context lean: `ecc:context-budget` / `ecc:strategic-compact` when a session runs long.

## 1. The pipeline at a glance

| Stage | Goal | Main skills | Agent (see §2) | Gate |
| --- | --- | --- | --- | --- |
| 1 Planning | Agree what to build and how | `ecc:plan`, `ecc:search-first`, `ecc:architecture-decision-records` | **Planner** | |
| 2 Risk assessment | Find what can break before writing code | `ecc:change-impact`, `ecc:safety-guard`, `ecc:contract-first` | **Planner** (2nd pass) | **Developer approves** |
| 3 Implementation | Smallest complete change | `ponytail:ponytail`, stack skills (§3) | **Implementer** | |
| 4 Validation & verification | Is it correct, lean, and what the plan said | `ponytail:ponytail-review`, `ecc:verification-loop`, `ecc:python-review` / `ecc:fastapi-review` | **Reviewer** | |
| 5 Testing | Prove it with tests | `ecc:tdd-workflow`, `ecc:python-testing`, `ecc:e2e-testing` | **Tester** | |
| 6 Security testing | Find holes before judges or attackers do | `ecc:security-review`, `security-review`, `ecc:security-scan` | **Security** | Fix all high findings |
| 7 Deployment / submission | Clean commits, docs, push | `ecc:git-workflow`, `ecc:prp-commit`, `ecc:update-docs`, `engineering:deploy-checklist` | main session | **Developer approves push** |

## 2. Multi-agent setup (works automatically)

Claude can spawn sub-agents on its own with the Agent tool. No configuration needed. Each agent starts cold and reports back; the developer sees one summary per agent in chat, not every intermediate step.

### Roles

| Agent | `subagent_type` | Access | Job |
| --- | --- | --- | --- |
| **Planner** | `ecc:planner` (+ `ecc:architect` for cross-module design) | read-only | Stage 1-2: requirements, file list, phases, risks |
| **Implementer** | `general-purpose` | read + write | Stage 3: writes code and tests from the approved plan |
| **Reviewer** | `ecc:python-reviewer`, `ecc:fastapi-reviewer`, `ecc:typescript-reviewer`, `ecc:react-reviewer`, `ecc:mle-reviewer`, `ecc:database-reviewer`, `ecc:silent-failure-hunter` (pick by file type) | read-only | Stage 4 |
| **Tester** | `ecc:tdd-guide`, `ecc:e2e-runner` (frontend flows) | read + write | Stage 5: gaps in tests, coverage |
| **Security** | `ecc:security-reviewer` | read-only | Stage 6: secrets, injection, SSRF, auth, webhook trust |

### Flow for a task like T02

```
Planner ──► [developer approves plan] ──► Implementer
                                            │
                          ┌─────────────────┼─────────────────┐
                          ▼                 ▼                 ▼
                      Reviewer(s)        Tester           Security      (run in parallel)
                          └─────────────────┼─────────────────┘
                                            ▼
                    findings back to Implementer (max 2 fix rounds)
                                            ▼
                         main session: verify, commit proposal
```

### Rules for agents

- Pass each agent: task ID, the relevant CONTEXT.md sections, the approved plan, the file list. Agents do not see this chat.
- One writer per file. For parallel implementation (e.g. `api/` and `web/` in T02), use `isolation: "worktree"` so edits cannot collide, then merge in the main session.
- Read-only agents propose, the Implementer disposes. A review finding is only acted on after it is re-checked against the code.
- Cap fix rounds at 2; after that, report what is left instead of looping.
- Agents cost tokens. Use the full multi-agent flow **only** for the critical components below. Everything else uses the single-agent "lite" path (section 5), solo, no sub-agents.

### Critical components (multi-agent flow)

| Component | Tasks | Why it is critical |
| --- | --- | --- |
| Webhook intake and signature verification | T01, T03 | Trust boundary; everything downstream acts on it |
| Order capture and refund (money movement) | T02 capture path, T12 refund | Real-money semantics, idempotency |
| Dispute label, snapshot builder, leakage tests | T04 | A leak silently invalidates every metric |
| Calibration and evaluation of models A and C | T06, T08 | The project's headline claim |
| LLM benchmark and decision layer | T13, T11 decision | Fairness of the comparison; money advice to the seller |
| Response and prevention agents (actions toward PayPal) | T11, T12 | Untrusted buyer text, approval gate, outbound PayPal calls |
| Model service `/score/*` contracts | T10 | Contract between ML and agents |

### Lite path (solo, no agents)

Scaffolding, config, Makefile, README and docs, UI layout and styling, AG Grid/Bryntum views, `render.yaml`, seed data, simulation scripts, dependency bumps, renames, test-only changes.

When unsure, treat a task as lite and escalate to the full flow if a review finds it touches a critical component.

### Optional: project-specific agents

If the generic roles prove too loose, add `.claude/agents/<name>.md` files (frontmatter `name`, `description`, `tools`) such as `dispute-security` with the PayPal webhook and buyer-message rules baked in. Not needed yet; ask when wanted. Note `.claude/` is gitignored in this repo, so such files would stay local unless the ignore rule is changed.

## 3. Skills per stage

### Stage 1: Planning
| Skill | Use it for |
| --- | --- |
| `ecc:plan` | Default planner. Restates requirements, phases, risks; waits for confirmation. |
| `ecc:prp-plan` | Deeper plan with codebase pattern extraction; use for T10, T11, T12, T14. |
| `ecc:search-first` | Check for an existing library/pattern before writing new code. |
| `ecc:architecture-decision-records` | Record decisions like "dropped paypal-agent-toolkit" so they are not re-litigated. |
| `ecc:blueprint` | Multi-session plan when a task spans two sessions (T14, T19). |
| `ecc:documentation-lookup` / `claude-api` | Current library docs; Claude API specifics for T09, T11, T13. |
| `paypal:python-getting-started`, `paypal:python-integrate-paypal` | Server SDK plan before touching orders/capture/refund. |

### Stage 2: Risk assessment
| Skill | Use it for |
| --- | --- |
| `ecc:change-impact` | What else a change touches (callers, tests, config). |
| `ecc:safety-guard` | Guard against destructive operations. |
| `ecc:contract-first` | Lock request/response shapes (webhooks, `/score/*`) before coding. |
| `ecc:agent-security-hardening` | Prompt-injection and tool-use risk for T11/T12 agents. |
| `ecc:mcp-dependency-review` | Before adding any MCP server or new plugin. |
| `ecc:cost-aware-llm-pipeline` | Claude API spend in T09, T11, T13. |

Output of Stages 1-2 is one short plan: scope, files, acceptance test, top risks, what is explicitly out of scope. The developer approves it.

### Stage 3: Implementation
| Area | Skills |
| --- | --- |
| Always | `ponytail:ponytail`, `ecc:coding-standards`, `ecc:error-handling` |
| FastAPI / Python | `ecc:fastapi-patterns`, `ecc:python-patterns`, `ecc:backend-patterns`, `ecc:api-design` |
| PayPal | `paypal:python-authentication`, `-calling-endpoints`, `-client-initialization`, `-models`, `-error-handling`, `-configuration-resilience` |
| Database | `ecc:postgres-patterns`, `ecc:database-migrations` (T02 orders, T11/T12 proposals) |
| ML (T04-T10, T13) | `ecc:mle-workflow`, `ecc:python-patterns`, `ecc:eval-harness` (calibration, leakage rules) |
| LLM calls | `claude-api`, `ecc:prompt-caching-strategy`, `ecc:regex-vs-llm-structured-text` |
| Frontend (T02, T14, T15) | `ecc:frontend-patterns`, `ecc:react-patterns`, `ecc:nextjs-turbopack`, `ui-ux-pro-max:ui-ux-pro-max`, `ui-ux-pro-max:ui-styling`, `ecc:frontend-a11y` |
| Charts (T14) | `dataviz` |
| Docker / Render (T16) | `ecc:docker-patterns`, `ecc:deployment-patterns` |

### Stage 4: Validation and verification
| Skill | Use it for |
| --- | --- |
| `ponytail:ponytail-review` | First pass: bugs, risk, missing tests, dead weight. Reads callers, not only the diff. |
| `ecc:verification-loop` | Check the result against the plan's acceptance criteria. |
| `ecc:python-review`, `ecc:fastapi-review`, `ecc:react-review` | Language/framework review. |
| `simplify` | Cleanup pass after review. |
| `ecc:quality-gate` | Formatter/lint gate. |
| `ecc:build-fix` | Only if the build breaks. |
| `ecc:code-review` / `code-review` | Final diff review before commit. |

### Stage 5: Testing
| Skill | Use it for |
| --- | --- |
| `ecc:tdd-workflow`, `ecc:python-testing` | Test-first for new logic; pytest conventions. |
| `paypal:python-testing` | Mocking PayPal SDK/REST calls. |
| `ecc:ai-regression-testing` | Guard model and prompt behaviour against silent regressions. |
| `ecc:eval-harness` | Metrics gates for ML tasks (Brier, PR-AUC, ECE). |
| `ecc:e2e-testing`, `ecc:browser-qa`, `claude-in-chrome` | Frontend flows, T02/T14/T17. |
| `ecc:react-testing` | Component tests. |
| `ecc:test-coverage` | Find uncovered risky paths. |
| `ecc:benchmark` | T10 latency budget (<300 ms), T13. |

Project rules that always apply: every feature function gets a leakage test (CONTEXT.md §3); `make test` must pass before Stage 7.

### Stage 6: Security testing
| Skill | Use it for |
| --- | --- |
| `ecc:security-review` / `security-review` | OWASP-style review of the diff (inputs, auth, SSRF, secrets). |
| `ecc:security-scan` | Scan of config and dependencies. |
| `ecc:agent-security-hardening` | Prompt injection through buyer messages; agent never calls PayPal on its own. |
| `ecc:gateguard` | Already active as a hook; keep it on. |

Project checklist for this stage:
- Webhook signature verified before any action; unverified events never trigger work.
- No secret in logs, errors, git history, or frontend bundle.
- Buyer text is never concatenated into instructions or shell/SQL.
- PayPal actions only after seller approval.
- Sandbox base URL only.

### Stage 7: Deployment / submission
| Skill | Use it for |
| --- | --- |
| `ecc:git-workflow` | Branch and commit hygiene. |
| `ecc:prp-commit`, `ecc:conventional-commit-review` | Commit message check; format is `T##: summary`. |
| `ecc:update-docs` / `ecc:docs-governance` | README and docs in sync with code. |
| `ponytail:ponytail-debt` | List `shortcut:` markers; record what was deferred. |
| `engineering:deploy-checklist` | Pre-deploy checklist (T16). |
| `ecc:deployment-patterns`, `ecc:canary-watch` | Render deploy and post-deploy check. |
| `ecc:opensource-pipeline` | Secret/PII sanitisation sweep before the repo is shown to judges (T18-T20). |
| `ecc:pr`, `ecc:github-ops` | Only if a PR is wanted; direct push to `main` is the default here. |

Stage 7 ends with: tick `TASKS.md`, add one line to CONTEXT.md §12, propose commit message(s), **wait for the developer to push**.

## 4. Task-type shortcuts

| Task type | Tasks | Add | Skip |
| --- | --- | --- | --- |
| Scaffold / integration | T01, T03, T16 | PayPal skills, `ecc:deployment-patterns` | ML skills |
| Frontend | T02, T14, T15 | UI skills, `ecc:e2e-runner`, `ecc:frontend-a11y` | `ecc:mle-*` |
| ML training | T04-T09 | `ecc:mle-workflow`, `ecc:mle-reviewer`, leakage tests, MLflow run logged | Stage 6 beyond data handling |
| Model service / agents | T10, T11, T12 | `ecc:fastapi-review`, `ecc:agent-security-hardening`, full Security agent | |
| Benchmark | T13 | `ecc:benchmark-methodology`, `ecc:eval-harness` | Frontend skills |
| Docs / video / submit | T18-T20 | `ecc:update-docs`, `ecc:opensource-pipeline`, `anthropic-skills:stop-slop` for README prose | Implementation agents |

## 5. Lite path (small fixes, doc edits, one-file changes)

1. `ponytail:ponytail` while writing.
2. One test if there is logic.
3. `ponytail:ponytail-review`.
4. `make test`.
5. Propose commit.

No sub-agents, no plan gate.

## 6. Not used on this project

Language and domain packs that do not apply: Kotlin, Flutter, Swift, Go, Rust, C++, Java/Spring, Django, Laravel, Rails, .NET, healthcare, homelab/network, trading/DeFi, marketing, video and social skills, the `paypal:dotnet-*` and `paypal:typescript-*` sets (Python SDK only), and `ecc:multi-*` (needs an external multi-model runtime that is not installed).

## 7. Known limits

- Plugin slash commands only load after `/reload-plugins` or a restart; skills can still be called through the Skill tool.
- GateGuard asks for a short fact statement before the first Bash/Edit of a session; that is expected.
- `ecc:plan` waits for confirmation by design. The pipeline's own plan gate (Stage 2) replaces a second confirmation.
- Sub-agents cannot ask the developer questions; they return open questions to the main session.
