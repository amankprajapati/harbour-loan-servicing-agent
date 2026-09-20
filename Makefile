.PHONY: test cases eval gateway-eval detect reproduce

test:
	python -m pytest tests/ -q

cases:
	python cases/generate_cases.py

eval: cases
	python eval/runner.py

gateway-eval: cases
	python eval/gateway_regression_runner.py

detect: cases
	python detectors/run_detectors.py

reproduce:
	bash scripts/reproduce.sh
