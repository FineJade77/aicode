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

# Calls a real model and costs real money, so it is deliberately absent from
# `test` and from PR CI. The scripted smoke suite above stays the CI gate: its
# value is zero-cost, zero-jitter regression detection, which a live suite
# cannot provide. Override REPETITIONS or LIVE_MODEL from the command line.
REPETITIONS ?= 3
eval-live:
	PYTHONPATH="$(CURDIR)/runtime:$(CURDIR)" python3 -m evals.runner \
		--suite live \
		--repetitions $(REPETITIONS) \
		$(if $(LIVE_MODEL),--live-model "$(LIVE_MODEL)",) \
		$(if $(LIVE_PROFILE),--live-profile '$(LIVE_PROFILE)',) \
		--output-root "$(EVAL_OUTPUT_ROOT)"

# The harder tier. Separate from `eval-live` rather than replacing it: the easy
# suite is a known reference point (84/84) and a cheap regression floor, while
# this one is the only tier that can produce failure attribution. Run both to
# get a difficulty gradient; run this one alone when you want the signal.
eval-live-hard:
	PYTHONPATH="$(CURDIR)/runtime:$(CURDIR)" python3 -m evals.runner \
		--suite live_hard \
		--repetitions $(REPETITIONS) \
		$(if $(LIVE_MODEL),--live-model "$(LIVE_MODEL)",) \
		$(if $(LIVE_PROFILE),--live-profile '$(LIVE_PROFILE)',) \
		--output-root "$(EVAL_OUTPUT_ROOT)"

# --- OpenAI-compatible retargeting -------------------------------------------
#
# The tasks pin Anthropic as their reference profile. These targets retarget the
# same task sets at an OpenAI-compatible endpoint, overriding provider, context
# window and price *together*: keeping the Anthropic 200k window and per-token
# prices would misreport both compaction and cost.
#
# Every value is overridable because none of them is knowable from here. The
# defaults describe DeepSeek's public `deepseek-chat`; a different model needs
# at least OC_MODEL, OC_CONTEXT_WINDOW and the two prices, or the report's cost
# column will confidently describe a price that was never charged.
#
# The key goes in whichever variable AICODE_OPENAI_API_KEY_ENV names
# (OPENAI_API_KEY by default) — it is never read from the CLI's config.toml.
OC_BASE_URL ?= https://api.deepseek.com/v1
OC_MODEL ?= deepseek-chat
OC_CONTEXT_WINDOW ?= 65536
OC_MAX_OUTPUT_TOKENS ?= 8192
OC_INPUT_PER_1M ?= 0.28
OC_OUTPUT_PER_1M ?= 0.42

# One definition both suites use, so the two can never drift into describing
# different models while claiming the same prices.
OC_LIVE_PROFILE = {"provider":"openai_compatible","model":"$(OC_MODEL)","context_window":$(OC_CONTEXT_WINDOW),"max_output_tokens":$(OC_MAX_OUTPUT_TOKENS),"input_per_1m":$(OC_INPUT_PER_1M),"output_per_1m":$(OC_OUTPUT_PER_1M)}
OC_ENV = AICODE_PROVIDER_TYPE=openai_compatible AICODE_OPENAI_BASE_URL="$(OC_BASE_URL)"

eval-live-openai-compatible:
	$(OC_ENV) $(MAKE) eval-live REPETITIONS=$(REPETITIONS) LIVE_PROFILE='$(OC_LIVE_PROFILE)'

eval-live-hard-openai-compatible:
	$(OC_ENV) $(MAKE) eval-live-hard REPETITIONS=$(REPETITIONS) LIVE_PROFILE='$(OC_LIVE_PROFILE)'

compile-python:
	python3 -m compileall -x 'evals/fixtures' runtime/app evals

tidy-go: deps-go
