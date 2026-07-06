.PHONY: dev test lint typecheck build scan kind-up kind-down deploy smoke clean

dev-up:  ## Run API + Postgres locally (local executor, no cluster needed)
	@test -f .env || { cp .env.example .env && echo "created .env from .env.example"; }
	docker compose up --build

dev-down:  ## Stop API + Postgres
	docker compose down

test:  ## Run the test suite
	python -m pytest tests/ -v

lint:  ## Lint and format-check
	ruff check .

typecheck:
	mypy agentbox --ignore-missing-imports

build:  ## Build the container image
	docker build -t agentbox:dev .

scan: build  ## Trivy-scan the image with the same gates as CI (HIGH/CRITICAL fail)
	@if command -v trivy >/dev/null 2>&1; then \
		trivy image --severity HIGH,CRITICAL --exit-code 1 \
			--ignorefile .trivyignore agentbox:dev; \
	else \
		echo "trivy not installed — running via docker (brew install trivy for a faster local setup)"; \
		docker run --rm \
			-v /var/run/docker.sock:/var/run/docker.sock \
			-v "$$(pwd)/.trivyignore:/.trivyignore:ro" \
			-v trivy-db-cache:/root/.cache \
			aquasec/trivy:0.70.0 image --severity HIGH,CRITICAL --exit-code 1 \
			--ignorefile /.trivyignore agentbox:dev; \
	fi

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
