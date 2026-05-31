.PHONY: run stop start restart status list enable disable patch unpick

# ──────────────────────────────────────────────
# codex-shim — gestión del servidor
# ──────────────────────────────────────────────

run:
	@echo "🚀 Iniciando codex-shim en http://127.0.0.1:8765 ..."
	@./bin/codex-shim start
	@echo "   Logs: .codex-shim/shim.log"

stop:
	@echo "🛑 Deteniendo codex-shim..."
	@./bin/codex-shim stop

restart:
	@echo "🔄 Reiniciando codex-shim..."
	@./bin/codex-shim restart

status:
	@./bin/codex-shim status

list:
	@./bin/codex-shim list

enable:
	@echo "🔌 Activando codex-shim en config de Codex..."
	@./bin/codex-shim enable

disable:
	@./bin/codex-shim disable

patch:
	@echo "🔧 Parcheando Codex Desktop para mostrar modelos BYOK..."
	@./bin/codex-shim patch-app

unpatch:
	@echo "🔧 Restaurando Codex Desktop original..."
	@./bin/codex-shim restore-app
