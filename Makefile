.PHONY: build install test-install-e2e test-local-provider-smoke deps deps-go deps-python test test-go test-python lint lint-python eval-smoke compile-python tidy-go lock-python

GOCACHE ?= $(CURDIR)/.cache/go-build
GOMODCACHE ?= $(CURDIR)/.cache/go-mod
GOENV := GOCACHE=$(GOCACHE) GOMODCACHE=$(GOMODCACHE)
BINDIR ?= $(CURDIR)/bin
AICODE_BIN ?= $(BINDIR)/aicode
AICODE_VERSION ?= $(shell tr -d '[:space:]' < $(CURDIR)/VERSION)
GO_LDFLAGS ?= -X github.com/FineJade77/aicode/cli/internal/version.Value=$(AICODE_VERSION)
INSTALL_PREFIX ?= $(HOME)/.local
INSTALL_PYTHON ?= python3
INSTALL_SYSTEM_SITE_PACKAGES ?= 0
INSTALL_SKIP_DEPS ?= 0
INSTALL_FLAGS := $(if $(filter 1,$(INSTALL_SYSTEM_SITE_PACKAGES)),--system-site-packages,) $(if $(filter 1,$(INSTALL_SKIP_DEPS)),--skip-deps,)
EVAL_OUTPUT_ROOT ?= $(CURDIR)/.artifacts/evals

build:
	mkdir -p $(BINDIR)
	$(GOENV) go build -ldflags "$(GO_LDFLAGS)" -o $(AICODE_BIN) ./cli

install: build
	"$(INSTALL_PYTHON)" scripts/install.py \
		--source-root "$(CURDIR)" \
		--prefix "$(INSTALL_PREFIX)" \
		--version "$(AICODE_VERSION)" \
		--cli "$(AICODE_BIN)" \
		--python "$(INSTALL_PYTHON)" \
		$(INSTALL_FLAGS)

test-install-e2e:
	bash scripts/test-install-e2e.sh

deps: deps-go deps-python

deps-go:
	$(GOENV) go work sync
	cd cli && $(GOENV) go mod tidy

deps-python:
	python3 -m pip install -r runtime/requirements-dev.lock.txt
	python3 -m pip install -e runtime --no-deps

# Regenerate the pinned dependency lock files from runtime/pyproject.toml.
# Requires uv (https://docs.astral.sh/uv/); run this after changing
# runtime/pyproject.toml dependencies and commit the resulting lock files.
lock-python:
	cd runtime && uv pip compile pyproject.toml --universal --python-version 3.11 -o requirements.lock.txt
	cd runtime && uv pip compile pyproject.toml --universal --python-version 3.11 --extra dev -o requirements-dev.lock.txt

lint: lint-python

lint-python:
	python3 -m ruff check .

test: lint-python test-go test-python

test-go:
	$(GOENV) go test ./cli/...

test-python:
	python3 -m pytest -q

test-local-provider-smoke:
	PYTHONPATH="$(CURDIR)/runtime" python3 -m pytest -q \
		runtime/tests/test_local_provider_profile.py::test_no_auth_localhost_profile_probe_and_full_edit_flow

eval-smoke:
	PYTHONPATH="$(CURDIR)/runtime:$(CURDIR)" python3 -m evals.runner \
		--suite smoke \
		--output-root "$(EVAL_OUTPUT_ROOT)" \
		--baseline "$(CURDIR)/evals/baselines/deterministic-smoke.v1.json"

compile-python:
	python3 -m compileall -x 'evals/fixtures' runtime/app evals

tidy-go: deps-go
