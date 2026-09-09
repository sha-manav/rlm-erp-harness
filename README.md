# ERP-Harness

A domain-specific agent harness for [ERP-Bench](https://github.com/agentic-labs/erp-bench)
(300 verifiable Odoo 19 procurement and manufacturing tasks), measured against the stock
`pi` coding-agent harness with the same model. No fine-tuning.

## Result

GLM-5.1 (fp8-pinned), one seed, 100 held-out tasks. The harness column is tag
`harness-v1-eval100`, a build without delegation.

| eval100 | pi (stock) | this harness |
|---|---|---|
| pass@1 | 39/100 | **72/100** |
| mean reward | 54.7 | **88.9** |
| paired difference | | +33 pts, 95% CI [+22, +43], p = 1e-7 |
| cost per task (one price basis) | $0.48 | $0.64 |

The harness is RLM-structured (a root agent holding the ERP in a persistent Python REPL,
snapshots for dry runs, read-only sub-agents), but the recursive part is unmeasured: the
evaluated build had no `delegate`, and where it exists the model has not used it.

![pass@1 versus cost per task, same model, harness varies](analysis/fig1_pareto.png)

![pass@1 by harness on the dev set and on eval](analysis/fig2_harness_groups.png)

Full account, how each piece was arrived at, ablation and caveats: [`analysis/writeup.md`](analysis/writeup.md).

## How it works

The model works in a Python kernel inside the task container, preloaded with a library:

- `harness/lib/erp.py` — typed Odoo client; write helpers refuse infeasible dates, origins and quantities
- `harness/lib/check.py` — 16 end-state invariants from ERP practice; `finish.py` refuses while one fails
- `harness/lib/state.py` — database snapshots and dry runs
- `harness/loop.py`, `agent.py`, `llm.py` — the loop, the Harbor entry point, the OpenRouter client
- `harness/prompts/` — the contract and playbook the model is given

Configs (`configs/configs.yaml`) differ only in which tools, prompts and library modules
the model gets: `A_pi` (stock pi), `B_bash` (loop only), `C_full`, and ablations.

## Run

```bash
make setup                  # harbor, vendored erp-bench, guard hook
make devbox && make test    # live tests against the dev task container
export DOCKER_BUILDKIT=0    # Docker Desktop may fail BuildKit metadata fetches
python3 scripts/run.py --config C_full --model big --set dev5 --background
python3 scripts/ingest_harbor.py runs/<run>
make aggregate stats cost figs
```

Needs an OpenRouter key in `.env` (`OPENROUTER_API_KEY`) with the account restricted to
fp8 upstreams of `z-ai/glm-5.1`. Every launch runs a balance preflight.

## Layout

```
harness/    the harness (loop, agent, kernel, lib/, prompts/)
configs/    task sets and the config/model tables
scripts/    run, ingest, aggregate, stats, cost, figures, release
tests/      unit and live tests
analysis/   writeup.md, results.csv (every trial), the merged eval tables, two figures
```
