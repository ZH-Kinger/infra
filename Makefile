.PHONY: test compile e2e

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

compile:
	PYTHONPYCACHEPREFIX=/tmp/dataset-sink-pycache PYTHONPATH=src python3 -m compileall -q src tests

e2e:
	./scripts/local-e2e.sh
