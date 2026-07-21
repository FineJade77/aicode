.PHONY: deps deps-go deps-python test test-go test-python compile-python tidy-go

GOCACHE ?= $(CURDIR)/.cache/go-build
GOMODCACHE ?= $(CURDIR)/.cache/go-mod
GOENV := GOCACHE=$(GOCACHE) GOMODCACHE=$(GOMODCACHE)

deps: deps-go deps-python

deps-go:
	$(GOENV) go work sync
	cd cli && $(GOENV) go mod tidy

deps-python:
	python3 -m pip install -e "runtime[dev]"

test: test-go test-python

test-go:
	$(GOENV) go test ./cli/...

test-python:
	python3 -m pytest -q

compile-python:
	cd runtime && python3 -m compileall app

tidy-go: deps-go
