.PHONY: install test test-all lint synth deploy-dev deploy-prod clean

# Install all project dependencies
install:
	pip install -r requirements.txt

# Run only unit tests (fast, no external dependencies)
test:
	pytest -m "unit" -v

# Run the full test suite
test-all:
	pytest -v

# Type-check the application source
lint:
	python -m mypy src/ --ignore-missing-imports

# Synthesise the CDK stack (does not deploy)
synth:
	cd infrastructure && pip install -r requirements.txt -q && cdk synth

# Deploy to the dev environment (no approval prompt)
deploy-dev:
	cd infrastructure && cdk deploy --context env=dev --require-approval never

# Deploy to production (requires explicit approval)
deploy-prod:
	cd infrastructure && cdk deploy --context env=prod

# Remove compiled bytecode and caches
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
	find . -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -name "cdk.out" -exec rm -rf {} + 2>/dev/null || true
