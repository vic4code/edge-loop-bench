.PHONY: check compile smoke test validate

PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3)
PYTHONPATH := src

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -v

compile:
	$(PYTHON) -m compileall -q src tests

validate:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m edgeloopbench validate configs/experiments/smoke.toml
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m edgeloopbench validate configs/experiments/serving-smoke.toml
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m edgeloopbench validate configs/experiments/vllm-metal-serving.example.toml

smoke:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m edgeloopbench summarize examples/results/sample-runs.jsonl --manifest examples/results/sample-plan.toml

check: test compile validate smoke
