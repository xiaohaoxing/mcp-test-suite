# MCP Test Suite Framework —— 一条命令跑完能力验收
#
# 为什么用 Makefile：验收的门槛越低，越可能被真的执行。
# 「先 cd 到某个目录、再 export 两个变量、再拼一条长命令」的门槛，结果是没人跑。
#
# 令牌一律从各仓库自己的 .env 读（--env-file），**不写进 Makefile、也不写进 YAML**。
# 每个服务的令牌变量与消费者名不同，所以逐服务显式写出（而不是猜）。

PY      ?= python3
BASE    ?=                     # make run-a BASE=http://other-host:18091 可指向别的实例
# 每个服务自己的仓库目录（默认假定与本项目同级；按需覆盖）
SVC_A_DIR ?= ../service-a
SVC_B_DIR ?= ../service-b
SVC_C_DIR ?= ../service-c
SVC_D_DIR ?= ../service-d

# 各服务的令牌变量名与消费者名：**因环境而异，用变量传，不写死**
SVC_A_TOKEN_ENV ?= SVC_A_TOKENS
SVC_A_CONSUMER  ?=
SVC_B_TOKEN_ENV ?= SVC_B_TOKENS
SVC_B_CONSUMER  ?=
SVC_ADMIN_TOKEN_ENV ?= MCP_ADMIN_TOKEN

.PHONY: help test validate-all doc-all run-all baseline-all \
        run-a run-b run-c run-d \
        baseline-a baseline-b baseline-c baseline-d

help:
	@echo "make test              # 框架自身的离线测试（假 MCP 服务，不碰网络）"
	@echo "make validate-all      # 只校验四个仓库的 YAML"
	@echo "make doc-all           # 重新渲染四个仓库的 MCP-TEST-SUITE.md"
	@echo "make run-all           # 跑四个服务的全套"
	@echo "make baseline-all      # 只跑 liveness/auth/guard/admin 四层基线"
	@echo "make run-a|run-b|run-c|run-d"
	@echo "make baseline-<服务>"
	@echo ""
	@echo "BASE=<url> 可覆盖 YAML 里的 base_url（例：make run-a BASE=http://127.0.0.1:18091）"

# --------------------------------------------------------------------------- 框架自身
test:
	$(PY) -m unittest discover -s tests -t . 

validate-all:
	$(PY) -m mcp_suite validate --suite $(SVC_A_DIR)/mcp-test-suite.yaml
	$(PY) -m mcp_suite validate --suite $(SVC_B_DIR)/mcp-test-suite.yaml
	$(PY) -m mcp_suite validate --suite $(SVC_C_DIR)/mcp-test-suite.yaml
	$(PY) -m mcp_suite validate --suite $(SVC_D_DIR)/mcp-test-suite.yaml

doc-all:
	$(PY) -m mcp_suite doc --suite $(SVC_A_DIR)/mcp-test-suite.yaml
	$(PY) -m mcp_suite doc --suite $(SVC_B_DIR)/mcp-test-suite.yaml
	$(PY) -m mcp_suite doc --suite $(SVC_C_DIR)/mcp-test-suite.yaml
	$(PY) -m mcp_suite doc --suite $(SVC_D_DIR)/mcp-test-suite.yaml

# --------------------------------------------------------------------------- 单服务
run-a:
	@mkdir -p reports
	$(PY) -m mcp_suite run --suite $(SVC_A_DIR)/mcp-test-suite.yaml \
	  --env-file $(SVC_A_DIR)/.env --token-env $(SVC_A_TOKEN_ENV) $(if $(SVC_A_CONSUMER),--consumer $(SVC_A_CONSUMER),) \
	  --admin-token-env MCP_ADMIN_TOKEN $(if $(BASE),--base-url $(BASE),) \
	  --json reports/service-a.json --md reports/service-a.md

run-b:
	@mkdir -p reports
	$(PY) -m mcp_suite run --suite $(SVC_B_DIR)/mcp-test-suite.yaml \
	  --env-file $(SVC_B_DIR)/.env $(if $(SVC_B_CONSUMER),--consumer $(SVC_B_CONSUMER),) \
	  --admin-token-env MCP_ADMIN_TOKEN $(if $(BASE),--base-url $(BASE),) \
	  --json reports/service-b.json --md reports/service-b.md

run-c:
	@mkdir -p reports
	$(PY) -m mcp_suite run --suite $(SVC_C_DIR)/mcp-test-suite.yaml \
	  --env-file $(SVC_C_DIR)/.env --consumer demo-readonly \
	  --admin-token-env MCP_ADMIN_TOKEN $(if $(BASE),--base-url $(BASE),) \
	  --json reports/service-c.json --md reports/service-c.md

run-d:
	@mkdir -p reports
	$(PY) -m mcp_suite run --suite $(SVC_D_DIR)/mcp-test-suite.yaml \
	  --env-file $(SVC_D_DIR)/.env --consumer localdev \
	  --admin-token-env MCP_ADMIN_TOKEN $(if $(BASE),--base-url $(BASE),) \
	  --json reports/service-d.json --md reports/service-d.md

# --------------------------------------------------------------------------- 基线四层
# 基线 = liveness / auth / guard / admin。domain 要按服务逐个确认，故意不放进基线：
# 一条猜出来的 domain 断言会让整轮「全绿」变得不可信。
BASELINE_LAYERS := liveness,auth,guard,admin

baseline-a:
	@mkdir -p reports
	$(PY) -m mcp_suite run --suite $(SVC_A_DIR)/mcp-test-suite.yaml --layer $(BASELINE_LAYERS) \
	  --env-file $(SVC_A_DIR)/.env --token-env $(SVC_A_TOKEN_ENV) $(if $(SVC_A_CONSUMER),--consumer $(SVC_A_CONSUMER),) \
	  --admin-token-env MCP_ADMIN_TOKEN $(if $(BASE),--base-url $(BASE),) \
	  --json reports/service-a-baseline.json --md reports/service-a-baseline.md

baseline-b:
	@mkdir -p reports
	$(PY) -m mcp_suite run --suite $(SVC_B_DIR)/mcp-test-suite.yaml --layer $(BASELINE_LAYERS) \
	  --env-file $(SVC_B_DIR)/.env $(if $(SVC_B_CONSUMER),--consumer $(SVC_B_CONSUMER),) \
	  --admin-token-env MCP_ADMIN_TOKEN $(if $(BASE),--base-url $(BASE),) \
	  --json reports/service-b-baseline.json --md reports/service-b-baseline.md

baseline-c:
	@mkdir -p reports
	$(PY) -m mcp_suite run --suite $(SVC_C_DIR)/mcp-test-suite.yaml --layer $(BASELINE_LAYERS) \
	  --env-file $(SVC_C_DIR)/.env --consumer demo-readonly \
	  --admin-token-env MCP_ADMIN_TOKEN $(if $(BASE),--base-url $(BASE),) \
	  --json reports/service-c-baseline.json --md reports/service-c-baseline.md

baseline-d:
	@mkdir -p reports
	$(PY) -m mcp_suite run --suite $(SVC_D_DIR)/mcp-test-suite.yaml --layer $(BASELINE_LAYERS) \
	  --env-file $(SVC_D_DIR)/.env --consumer localdev \
	  --admin-token-env MCP_ADMIN_TOKEN $(if $(BASE),--base-url $(BASE),) \
	  --json reports/service-d-baseline.json --md reports/service-d-baseline.md

# --------------------------------------------------------------------------- 全部
# 逐个跑完再汇总退出码：某个服务红了不影响其它服务的报告产出（验收要看全貌）。
run-all:
	@fail=0; \
	for t in run-a run-b run-c run-d; do \
	  echo "=== $$t"; $(MAKE) --no-print-directory $$t || fail=1; \
	done; \
	echo ""; echo "run-all 汇总退出码：$$fail（0=全过 / 1=有失败）"; exit $$fail

baseline-all:
	@fail=0; \
	for t in baseline-a baseline-b baseline-c baseline-d; do \
	  echo "=== $$t"; $(MAKE) --no-print-directory $$t || fail=1; \
	done; \
	echo ""; echo "baseline-all 汇总退出码：$$fail（0=全过 / 1=有失败）"; exit $$fail
