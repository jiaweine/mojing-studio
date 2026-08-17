.PHONY: check run

check:
	python -m compileall -q app tests
	pytest -q
	node --check app/static/app.js

run:
	python run.py
