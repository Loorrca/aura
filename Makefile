.PHONY: init preflight migrate downgrade

init:
	python3 scripts/aura_init.py

preflight:
	cd ops && ./.venv/bin/python -V >/dev/null 2>&1 || true
	cd ops && ./demo.sh preflight

migrate:
	cd ops && ./demo.sh migrate

downgrade:
	cd ops && ./demo.sh downgrade
