# One command to set up, one to run the demo. On Windows without make, run the commands after each colon.
setup:
	python -m pip install -e ".[dev]"

demo:
	finops-agent demo --open

test:
	ruff check . && ruff format --check . && mypy && python -m pytest -q

eval:
	finops-agent eval

.PHONY: setup demo test eval
