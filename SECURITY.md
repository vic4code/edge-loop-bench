# Security policy

EdgeLoopBench is a research scaffold that will eventually execute model-proposed code. Version 0.1 does not provide a sandbox and must not be used to run untrusted patches.

## Current guidance

- Keep Ollama, vLLM-Metal, and MLX-LM development servers bound to localhost.
- Set `OLLAMA_HOST=127.0.0.1:11434` and pass `--host 127.0.0.1` to `vllm serve`; do not rely on runtime defaults.
- Do not expose local model endpoints to an untrusted network.
- Never place credentials, private source code, or hidden evaluator data in model-visible prompts.
- Never place secrets in experiment commands or environment tables. The manifest validator rejects obvious secret-bearing variable names and command flags, but cannot prove that an innocently named value is safe.
- Treat model output and benchmark task repositories as untrusted input.
- Do not run future task commands outside an isolated worktree and explicit allowlist.
- Review model and task licenses before redistribution.

## Future controller requirements

Before arbitrary task execution is enabled, the controller must enforce process timeouts, path containment, output limits, network denial, environment-variable filtering, resource limits, and hidden-test isolation. These are release blockers, not optional hardening.

Please report vulnerabilities privately to the repository owner once a public remote and security contact exist.
