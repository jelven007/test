COMPOSE_FILE := deploy/compose/docker-compose.yml
COMPOSE_ENV := deploy/compose/.env
COMPOSE := docker compose --env-file $(COMPOSE_ENV) -f $(COMPOSE_FILE)

.PHONY: test integration-test infra-config infra-up infra-down infra-status infra-logs infra-check

test:
	PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v

integration-test:
	BANXIA_RUN_INTEGRATION=1 .venv/bin/python -m unittest tests.test_storage_integration -v

infra-config:
	$(COMPOSE) config

infra-up:
	$(COMPOSE) up -d

infra-down:
	$(COMPOSE) down

infra-status:
	$(COMPOSE) ps

infra-logs:
	$(COMPOSE) logs --tail=200

infra-check:
	bash scripts/check_infra.sh
