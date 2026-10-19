SHELL := /bin/bash
.DEFAULT_GOAL := help
DRY_RUN ?= 0
# Local ISP tools, independent of office sites. Never pass credentials through make arguments.
PYTHON ?= python3
PROFILE ?= Provider
TARGET_GROUP ?= Claude
ISP_CONFIG ?= $(HOME)/.config/surge-isp/isp.json
.PHONY: isp isp-import isp-apply isp-status isp-check isp-sync isp-watch isp-restore test-isp

isp: ## Manage ISP nodes, hidden credentials, default exit and custom SSIDs
	@$(PYTHON) scripts/surge-isp.py --config "$(ISP_CONFIG)" configure

isp-import: ## Import legacy Claude-ISP.dconf once; do not enable it or print credentials
	@$(PYTHON) scripts/surge-isp.py --config "$(ISP_CONFIG)" import-current --profile "$(PROFILE)"

isp-apply: ## Attach, install auto-sync and verify Claude exit; DRY_RUN=1 previews, LEGACY=1 migrates
	@$(PYTHON) scripts/surge-isp.py --config "$(ISP_CONFIG)" setup --profile "$(PROFILE)" --target-group "$(TARGET_GROUP)" $(if $(filter 1,$(DRY_RUN)),--dry-run) $(if $(filter 1,$(LEGACY)),--migrate-legacy)

isp-status: ## Show configured ISP defaults and attached profiles without credentials
	@$(PYTHON) scripts/surge-isp.py --config "$(ISP_CONFIG)" status

isp-check: ## Show the active Claude/global exit IP and API reachability without changing mode
	@$(PYTHON) scripts/surge-isp.py --config "$(ISP_CONFIG)" check

isp-sync: ## Run one sync pass manually; launchd normally handles this automatically
	@$(PYTHON) scripts/surge-isp.py --config "$(ISP_CONFIG)" sync

isp-watch: ## Reinstall the user sync agent after moving the repo or changing Python
	@$(PYTHON) scripts/surge-isp.py --config "$(ISP_CONFIG)" watch-install

isp-restore: ## Restore a backup and pause that profile's auto-sync; requires BACKUP=path
	@test -n "$(BACKUP)" || { echo '[isp] ERROR: BACKUP=directory is required' >&2; exit 1; }
	@$(PYTHON) scripts/surge-isp.py --config "$(ISP_CONFIG)" restore --backup "$(BACKUP)"

test-isp: ## Run offline ISP regression tests without real Surge access or credentials
	@$(PYTHON) -m unittest discover -s tests -p 'surge_isp*_test.py' -v

help: ## List ISP commands
	@printf 'surge-custom-isp — make isp-auto (Proxy-Cheap wizard); make isp (manual)\n'
	@awk 'BEGIN{FS=":.*##"} /^[a-z][a-zA-Z_-]*:.*##/ {printf "  %-20s %s\n",$$1,$$2}' $(MAKEFILE_LIST)

.PHONY: isp-auto
isp-auto: ## Guided Proxy-Cheap API import, SOCKS5, Surge activation and exit check
	@$(PYTHON) scripts/proxy-cheap-setup.py --config "$(ISP_CONFIG)" $(if $(filter 1,$(DRY_RUN)),--dry-run)
