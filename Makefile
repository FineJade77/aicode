.PHONY: build install deps deps-go deps-python test test-go test-python compile-python tidy-go lock-python

GOCACHE ?= $(CURDIR)/.cache/go-build
GOMODCACHE ?= $(CURDIR)/.cache/go-mod
GOENV := GOCACHE=$(GOCACHE) GOMODCACHE=$(GOMODCACHE)
BINDIR ?= $(CURDIR)/bin
AICODE_BIN ?= $(BINDIR)/aicode
INSTALL_BINDIR ?= $(HOME)/.local/bin

build:
	mkdir -p $(BINDIR)
	$(GOENV) go build -o $(AICODE_BIN) ./cli

install: build
	mkdir -p $(INSTALL_BINDIR)
	install -m 0755 $(AICODE_BIN) $(INSTALL_BINDIR)/aicode

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

test: test-go test-python

test-go:
	$(GOENV) go test ./cli/...

test-python:
	python3 -m pytest -q

compile-python:
	cd runtime && python3 -m compileall app

tidy-go: deps-go
