.PHONY: dev test lint typecheck build kind-up kind-down deploy smoke clean

dev:  ## Run API + Postgres locally (local executor, no cluster needed)
	docker compose up --build

test:  ## Run the test suite
	python -m pytest tests/ -v

lint:  ## Lint and format-check
	ruff check .

typecheck:
	mypy agentbox --ignore-missing-imports

build:  ## Build the container image
	docker build -t agentbox:dev .

kind-up:  ## Create a local kind cluster and deploy agentbox into it
	./scripts/kind-up.sh

kind-down:
	kind delete cluster --name agentbox

deploy:  ## Deploy/upgrade via Helm into the current kube context
	helm upgrade --install agentbox deploy/helm/agentbox \
		--namespace agentbox --create-namespace

smoke:  ## Submit a test session against a running instance
	./scripts/smoke.sh

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__
