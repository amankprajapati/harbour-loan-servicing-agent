.PHONY: test cases eval

test:
	python -m pytest tests/ -q

cases:
	python cases/generate_cases.py

eval: cases
	python eval/runner.py
