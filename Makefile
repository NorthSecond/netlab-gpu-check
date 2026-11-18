# Convenience entry points. Run `make help` for the list.
.PHONY: help test test-static test-relay test-exporter test-proxy test-install embed check-embedded

help:
	@echo "make test            - run every suite (see tests/run-all.sh)"
	@echo "make test-static     - config/shell syntax, embedded sync, secret scan"
	@echo "make test-relay      - Grafana -> Feishu relay behaviour (no network)"
	@echo "make test-exporter   - exporter behaviour against mock NVML (no GPU)"
	@echo "make test-proxy      - nginx front door: anonymous blocked, login allowed"
	@echo "make test-install    - client/install.sh end-to-end in a container"
	@echo "make embed           - regenerate remote-install.sh from client/"
	@echo "make check-embedded  - fail if remote-install.sh is stale"

test:
	./tests/run-all.sh

test-static:
	./tests/run-all.sh static

test-relay:
	./tests/run-all.sh relay

test-exporter:
	./tests/run-all.sh exporter

test-proxy:
	./tests/run-all.sh proxy

test-install:
	./tests/run-all.sh install

embed:
	./scripts/embed-client.sh

check-embedded:
	./scripts/check-embedded.sh
