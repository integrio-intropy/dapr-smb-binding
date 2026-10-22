# Thin wrappers over the commands CI runs, so the two cannot drift apart.
# Unit tests need only Python; `integration` needs Docker.

PYTHON   ?= python3
VENV     ?= .venv
BIN      := $(VENV)/bin
VERSION  ?= dev
COMPOSE  := docker compose -f tests/integration/docker-compose.yaml
PROTO_OUT := src/dapr_smb_binding/_proto

.PHONY: all venv proto test lint fmt integration integration-up integration-down e2e image clean

all: lint test

venv: $(BIN)/activate
$(BIN)/activate: pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e '.[dev]'
	touch $@

# The generated stubs are committed so the package installs without
# grpcio-tools. Regenerate after updating the vendored protos in proto/.
# protoc emits imports for the proto package path (dapr.proto.components.v1),
# which would collide with the Dapr Python SDK's `dapr` package, so they are
# rewritten to point inside this package. The proto *package* is unchanged, so
# the gRPC service names the sidecar looks for are too.
proto: venv
	rm -f $(PROTO_OUT)/*_pb2*.py $(PROTO_OUT)/*_pb2*.pyi
	$(BIN)/python -m grpc_tools.protoc -Iproto \
		--python_out=$(PROTO_OUT) --pyi_out=$(PROTO_OUT) --grpc_python_out=$(PROTO_OUT) \
		proto/dapr/proto/components/v1/common.proto proto/dapr/proto/components/v1/bindings.proto
	mv $(PROTO_OUT)/dapr/proto/components/v1/* $(PROTO_OUT)/
	rm -rf $(PROTO_OUT)/dapr
	sed -i.bak 's/^from dapr\.proto\.components\.v1 import/from dapr_smb_binding._proto import/' $(PROTO_OUT)/*.py $(PROTO_OUT)/*.pyi
	rm -f $(PROTO_OUT)/*.bak

test: venv
	$(BIN)/pytest

lint: venv
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

fmt: venv
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

# The suite runs inside the compose network: Kerberos needs the DC's real
# name, and the runner image has the same GSSAPI stack as production.
integration: integration-up
	$(COMPOSE) run --rm --build tests

integration-up:
	$(COMPOSE) up -d --build --wait dc

# The production image as a pluggable component under a real daprd sidecar.
# `up`, not `run`: daprd reaches the app by its service name, which a `run`
# container does not get. Then SMB_BINDING_EXIT_WHEN_IDLE: once daprd is gone, the
# component must exit by itself, with 0, so a Job's pod can complete.
e2e: integration-up
	$(COMPOSE) --profile dapr up -d --build --force-recreate component daprd e2e
	@$(COMPOSE) --profile dapr wait e2e >/dev/null; code=$$?; \
	  $(COMPOSE) --profile dapr logs --no-log-prefix e2e | tail -3; test $$code = 0
	$(COMPOSE) --profile dapr stop daprd
	@for i in $$(seq 30); do \
	  state=$$($(COMPOSE) --profile dapr ps -a --format '{{.State}} {{.ExitCode}}' component); \
	  case "$$state" in exited*) echo "component $$state after daprd stopped"; test "$$state" = "exited 0"; exit;; esac; \
	  sleep 1; \
	done; echo "component still running 30s after daprd stopped"; exit 1

integration-down:
	$(COMPOSE) --profile dapr --profile tests down -v

image:
	docker build --build-arg VERSION=$(VERSION) -t dapr-smb-binding:$(VERSION) .

clean:
	rm -rf dist build .pytest_cache .ruff_cache
