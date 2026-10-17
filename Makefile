PYTHON ?= python3

.PHONY: check tasks prompt unit integration verify

check:
	$(PYTHON) scripts/harness.py check

tasks:
	$(PYTHON) scripts/harness.py tasks

prompt:
	$(PYTHON) scripts/harness.py prompt $(TASK)

unit:
	$(PYTHON) -m pytest tests -m 'not integration and not live'

integration:
	scripts/run_integration.sh

verify: check unit integration
