# Terminal-Bench 2.1 — Experiment Data & Scaffold

Artifact repository accompanying our IEEE paper. It contains a self-built
Docker harness that evaluates agents on the **Terminal-Bench 2.1** benchmark
(**89 tasks**, `tasks/`), together with all raw trajectories and verifier
outputs produced by the runs.

- Model: `deepseek-v4-flash-vision-exp` for all agents.
- Metric: official accuracy — a separate verifier container writes
  `/logs/verifier/reward.txt`; `reward == 1` counts as PASS.

## Branches

The scaffold and its results live on two branches:

| Branch | Experiment | Agents | Trajectory data |
| --- | --- | --- | --- |
| `main` | Main experiment | logorythia, swe-agent, autogen, agentflow, claude-code | `traj 1/`, `traj 2/`, `traj 3/` — three independent runs over all 89 tasks |
| `ablation` | Ablation | logorythia1–logorythia4 (four Logorythia variants) | `traj B/`, `traj2×21/`, `traj2×22/`, `traj2×23/` — runs over a 30-task sample |

Each `traj*/<agent>/<task>/` directory holds the agent's raw trajectory and
the verifier output for that task; `traj*/<agent>/eval_results.json` is the
per-run accuracy summary. (On `main`, trajectories are committed for
logorythia, swe-agent, autogen and claude-code.)

## Usage

Windows / PowerShell:

```powershell
.\setup_venv.ps1
.\.venv\Scripts\Activate.ps1

# optional: local package cache for Docker builds; pre-pull base images
.\.cache\start_dnf_cacher.ps1
python scripts/pull_tb2_base_images.py
```

### Main experiment (`main` branch)

```powershell
python run_bench.py 1 --logorythia --swe --auto --agentflow --claude
```

```text
python run_bench.py <start> [--end M] [--logorythia] [--swe] [--auto] [--agentflow] [--claude] [--redo] [--n-concurrent N]
```

| Argument | Meaning |
| --- | --- |
| `<start>` / `--end M` | Task range (1-based, tasks sorted by name) |
| `--logorythia` … `--claude` | Which agents to run (pick at least one) |
| `--redo` | Re-run tasks already present in `eval_results.json` |
| `--n-concurrent N` | Agents per task in parallel (`1` = serial) |

### Ablation (`ablation` branch)

```powershell
git checkout ablation
.\setup_venv.ps1
.\.venv\Scripts\Activate.ps1

python run_bench.py                     # all 4 variants on the 30-task sample
python run_bench.py 1 --end 8 --sy1     # subset of tasks / variants
```

Each run samples 30 tasks with a fixed `seed=42` (`harness/config.py`), so
the sample is reproducible. Flags `--sy1`…`--sy4` select variants; omitting
all of them runs all four.
