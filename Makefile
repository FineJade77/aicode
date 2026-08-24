.PHONY: build install test-install-e2e test-local-provider-smoke deps deps-go deps-python test test-go test-python lint lint-python lint-go eval-smoke compile-python tidy-go lock-python

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

lint: lint-python lint-go

lint-python:
	python3 -m ruff check .

# The Go gate, as one target CI also calls. Previously CI ran gofmt and vet as
# its own inline steps and `make lint` covered only Python, so the repo's own
# lint entry point passed on code CI would reject, and the only way to reproduce
# the Go gate locally was to remember two commands that existed nowhere in the
# Makefile. A shared target is the anti-drift mechanism; duplicating the steps
# in CI is what let them diverge.
#
# `go vet` matters more here than it looks: its stdversion analyzer is what
# reports a standard-library symbol newer than the `go` directive allows — the
# one class of mistake that compiles on a developer's newer toolchain and fails
# on CI's older one.
lint-go:
	@fmt_out=$$(gofmt -l cli/); \
	if [ -n "$$fmt_out" ]; then \
		echo "The following files are not gofmt'ed:"; \
		echo "$$fmt_out"; \
		exit 1; \
	fi
	$(GOENV) go vet ./cli/...

test: lint test-go test-python

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

# The scale tier. `live_hard` established that indirection is free for a model
# that reads the whole repo quickly; these make reading everything expensive
# instead — dozens of near-identical modules where grepping the symptom returns
# equally plausible hits. If this tier discriminates where `live_hard` did not,
# that is the evidence T-047 (repo map) is waiting on.
eval-live-scale:
	PYTHONPATH="$(CURDIR)/runtime:$(CURDIR)" python3 -m evals.runner \
		--suite live_scale \
		--repetitions $(REPETITIONS) \
		$(if $(LIVE_MODEL),--live-model "$(LIVE_MODEL)",) \
		$(if $(LIVE_PROFILE),--live-profile '$(LIVE_PROFILE)',) \
		--output-root "$(EVAL_OUTPUT_ROOT)"

# One defect at four repository sizes. Every tier so far scores a functional
# pass@1 of 1.000, so the verdict has stopped carrying information; this measures
# retrieval cost against size instead. Read the shape with
# `python3 scripts/eval_curve.py <report-dir>` — whether the curve bends upward
# or stays flat is what decides T-047.
eval-live-scale-curve:
	PYTHONPATH="$(CURDIR)/runtime:$(CURDIR)" python3 -m evals.runner \
		--suite live_scale_curve \
		--repetitions $(REPETITIONS) \
		$(if $(LIVE_MODEL),--live-model "$(LIVE_MODEL)",) \
		$(if $(LIVE_PROFILE),--live-profile '$(LIVE_PROFILE)',) \
		--output-root "$(EVAL_OUTPUT_ROOT)"

# The coordinated tier. `live_hard` showed that distance between cause and effect
# is free for a model that reads the whole repo quickly, and `live_scale_curve`
# showed the same for repository size. This changes what is being asked instead
# of how far away it is: a correct answer has to land in several files at once,
# and each of them needs a *different* edit. Guards pin that a partial change
# still fails and that find-and-replace is insufficient.
eval-live-coordinated:
	PYTHONPATH="$(CURDIR)/runtime:$(CURDIR)" python3 -m evals.runner \
		--suite live_coordinated \
		--repetitions $(REPETITIONS) \
		$(if $(LIVE_MODEL),--live-model "$(LIVE_MODEL)",) \
		$(if $(LIVE_PROFILE),--live-profile '$(LIVE_PROFILE)',) \
		--output-root "$(EVAL_OUTPUT_ROOT)"

# The ambiguity tier. The only one where the right behaviour is not "produce the
# change": both readings compile and pass their own reading, so the graded
# property is whether the Agent asked before it chose. The harness answers with
# the task's fixed reply, and the branch assertions live in the task JSON so the
# workspace never contains the answer.
eval-live-ambiguous:
	PYTHONPATH="$(CURDIR)/runtime:$(CURDIR)" python3 -m evals.runner \
		--suite live_ambiguous \
		--repetitions $(REPETITIONS) \
		$(if $(LIVE_MODEL),--live-model "$(LIVE_MODEL)",) \
		$(if $(LIVE_PROFILE),--live-profile '$(LIVE_PROFILE)',) \
		--output-root "$(EVAL_OUTPUT_ROOT)"

# The long-horizon tier. Every other tier is one decision carried out; these are
# a dozen or more of them in sequence. Collapsing the sequence into fewer edits
# is *allowed* — a deterministic chain is always collapsible by a capable agent,
# and engineering that away would take artificial opacity. What is measured is
# completion: whether all the steps land, or the run declares itself done at
# nine of fourteen.
eval-live-longhorizon:
	PYTHONPATH="$(CURDIR)/runtime:$(CURDIR)" python3 -m evals.runner \
		--suite live_longhorizon \
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

eval-live-scale-openai-compatible:
	$(OC_ENV) $(MAKE) eval-live-scale REPETITIONS=$(REPETITIONS) LIVE_PROFILE='$(OC_LIVE_PROFILE)'

eval-live-scale-curve-openai-compatible:
	$(OC_ENV) $(MAKE) eval-live-scale-curve REPETITIONS=$(REPETITIONS) LIVE_PROFILE='$(OC_LIVE_PROFILE)'

eval-live-coordinated-openai-compatible:
	$(OC_ENV) $(MAKE) eval-live-coordinated REPETITIONS=$(REPETITIONS) LIVE_PROFILE='$(OC_LIVE_PROFILE)'

eval-live-ambiguous-openai-compatible:
	$(OC_ENV) $(MAKE) eval-live-ambiguous REPETITIONS=$(REPETITIONS) LIVE_PROFILE='$(OC_LIVE_PROFILE)'

eval-live-longhorizon-openai-compatible:
	$(OC_ENV) $(MAKE) eval-live-longhorizon REPETITIONS=$(REPETITIONS) LIVE_PROFILE='$(OC_LIVE_PROFILE)'

compile-python:
	python3 -m compileall -x 'evals/fixtures' runtime/app evals

tidy-go: deps-go
