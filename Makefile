# Re-create generated outputs, screenshots and the demo recording. Needs Python 3.10+ and Chrome or Chromium.
PY ?= .venv/bin/python

.PHONY: test run eval screenshots demo
test:
	$(PY) -m pytest -q
run:
	$(PY) -m creditmemo run | tee docs/demo_run_output.txt
eval:
	$(PY) -m creditmemo eval
screenshots: run eval
	$(PY) scripts/capture.py shots
demo: run eval
	$(PY) scripts/capture.py demo
