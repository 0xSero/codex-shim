# Manual de Uso — Moon Bridge + codex-shim

## Arquitectura de Doble Modo

Este proyecto implementa dos modos de operación independientes para usar Codex con
diferentes proveedores de modelo:

```
┌──────────────────────────────────────────────────────────────────┐
│                    ARQUITECTURA DUAL                              │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  MODO NORMAL (ChatGPT)          MODO DEEPSEEK (BYOK)            │
│  ────────────────               ──────────────────               │
│                                                                  │
│  Codex App / CLI                Codex App / CLI                  │
│       │                              │                           │
│       ▼                              ▼                           │
│  ChatGPT API                   codex-shim (:8765)                │
│  (chatgpt.com)                      │                           │
│       │                           ▼ (Anthropic Messages)         │
│       ▼                          DeepSeek API                    │
│  GPT-5.5 / GPT-5.4 / Mini       (api.deepseek.com/anthropic)     │
│                                                                  │
│  ─── ó ───                                                      │
│                                                                  │
│  Codex CLI                                                        │
│       │                                                          │
│       ▼                                                          │
│  Moon Bridge (:38440)                                            │
│       │                                                          │
│       ▼ (Anthropic Messages)                                     │
│  DeepSeek API                                                    │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### Modo Normal

- Usa **GPT-5.5**, **GPT-5.4** o **GPT-5.4 Mini** a través de ChatGPT
  (créditos de suscripción)
- **No requiere servidores locales**
- Codex conecta directamente a `chatgpt.com`
- Apropiado para uso diario cuando hay créditos disponibles

### Modo DeepSeek — Dos Caminos

| Camino | Cliente | Proxy | Puerto | Traducción |
|--------|---------|-------|--------|------------|
| **A** | Codex CLI → | Moon Bridge | `:38440` | OpenAI Responses → Anthropic Messages |
| **B** | Codex App / VS Code → | codex-shim | `:8765` | OpenAI Responses → Anthropic Messages |

- Usa modelos **DeepSeek V4 Pro** o **DeepSeek V4 Flash**
- Requiere API key de DeepSeek
- Apropiado como modo de emergencia/caída cuando se agotan los créditos de ChatGPT

---

## Primeros Pasos — Una Sola Vez

### 1. Configurar Moon Bridge (Camino A — Codex CLI)

```bash
cd ~/Documents/gitprojects/moon-bridge

# Copiar config y editar
cp config.example.yml config.yml
# Editar config.yml: poner DEEPSEEK_API_KEY

# Iniciar servidor
make run

# Generar config para Codex CLI
make codex-config
```

### 2. Configurar codex-shim (Camino B — Codex App / VS Code)

```bash
cd ~/codex-shim

# Crear archivo de modelos (si no existe)
cat > ~/.codex-shim/models.json << 'EOF'
[
  {
    "model": "deepseek-v4-pro",
    "display_name": "DeepSeek V4 Pro",
    "provider": "anthropic",
    "base_url": "https://api.deepseek.com/anthropic",
    "api_key": "sk-tu-api-key-deepseek",
    "max_context_limit": 1000000,
    "max_output_tokens": 384000
  },
  {
    "model": "deepseek-v4-flash",
    "display_name": "DeepSeek V4 Flash",
    "provider": "anthropic",
    "base_url": "https://api.deepseek.com/anthropic",
    "api_key": "sk-tu-api-key-deepseek",
    "max_context_limit": 1000000,
    "max_output_tokens": 384000
  }
]
EOF

# Parchear Codex Desktop (una sola vez)
./bin/codex-shim patch-app

# Activar shim en config de Codex
./bin/codex-shim enable

# Verificar
./bin/codex-shim list
```

> **Nota**: El paso `patch-app` modifica el `app.asar` de Codex Desktop para
> que el selector de modelos muestre modelos de cualquier proveedor (no solo
> OpenAI). Es necesario solo la primera vez; se puede deshacer con
> `./bin/codex-shim restore-app`.

---

## Uso Diario

### Cambiar entre Modos

#### Con Makefile (recomendado)

Los siguientes objetivos están disponibles en el `Makefile` de Moon Bridge:

```bash
cd ~/Documents/gitprojects/moon-bridge

make mode-normal       # Cambia al modo ChatGPT
make mode-deepseek     # Cambia a DeepSeek V4 Flash (vía codex-shim)
make mode-deepseek-pro # Cambia a DeepSeek V4 Pro (vía codex-shim)
make mode-moonbridge   # Cambia a Moon Bridge directo (para Codex CLI)
```

#### Con codex-shim CLI

```bash
# Ver modelos disponibles
codex-shim list

# Cambiar a un modelo ChatGPT/Codex first-party
codex-shim model use gpt-5.4-mini

# Cambiar a DeepSeek V4 Flash (BYOK)
codex-shim model use deepseek-v4-flash

# Cambiar a DeepSeek V4 Pro (BYOK)
codex-shim model use deepseek-v4-pro
```

#### Con el selector web (recomendado para uso gráfico)

El codex-shim incluye un **picker web interactivo** en su propio servidor.
Abrelo en el navegador:

```
http://127.0.0.1:8765/picker
```

Verás una interfaz como esta:

```
┌─────────────────────────────────────────────┐
│  Model Picker                               │
│  Choose the active model for Codex Desktop  │
│                                             │
│  ┌─────────────────────────────────────┐    │
│  │ GPT-5.5                    🟢Active │    │
│  │ chatgpt · gpt-5.5                   │    │
│  └─────────────────────────────────────┘    │
│                                             │
│  ┌─────────────────────────────────────┐    │
│  │ GPT-5.4 Mini              🔄Switch │    │
│  │ chatgpt · gpt-5.4-mini             │    │
│  └─────────────────────────────────────┘    │
│                                             │
│  ┌─────────────────────────────────────┐    │
│  │ DeepSeek V4 Pro           🔄Switch │    │
│  │ anthropic · deepseek-v4-pro        │    │
│  └─────────────────────────────────────┘    │
│                                             │
│  ┌─────────────────────────────────────┐    │
│  │ DeepSeek V4 Flash         🔄Switch │    │
│  │ anthropic · deepseek-v4-flash      │    │
│  └─────────────────────────────────────┘    │
│                                             │
│  ☑ Auto-restart Codex after switching       │
│                                             │
└─────────────────────────────────────────────┘
```

**Cómo funciona:**

Cada tarjeta muestra:
- **Nombre del modelo** (GPT-5.5, GPT-5.4 Mini, DeepSeek V4 Pro, etc.)
- **Proveedor** (`chatgpt` o `anthropic`)
- **Slug** interno (`gpt-5.5`, `gpt-5.4-mini`, `deepseek-v4-pro`, etc.)
- **Estado**: 🟢 Active (modelo actual) o 🔄 Switch (cambiar a este)

**Para cambiar:**

1. Abre `http://127.0.0.1:8765/picker` en cualquier navegador
2. Haz clic en **"Switch"** en el modelo que quieras usar
3. El picker llama a `POST /api/switch { "slug": "...", "restart_codex": true }`
4. Esto reescribe `model = "..."` en `~/.codex/config.toml`
5. Si tienes marcado **"Auto-restart Codex"**, Codex Desktop se cierra y relanza solo
6. Si no, reinicia Codex Desktop manualmente

**Ventajas del picker** frente a terminal:
- Ves todos los modelos de un vistazo con su estado actual
- No necesitas recordar slugs ni comandos
- El auto-restart evita tener que reiniciar manualmente
- Funciona desde cualquier dispositivo en la red local

#### Manualmente (cambiar el modelo activo)

El archivo `~/.codex/config.toml` no funciona como una lista de
"3 modos" separados. Lo importante es dejar activo el `model = "..."` del
slug que quieras usar dentro del bloque gestionado por `codex-shim`.

1. Abre `~/.codex/config.toml` con cualquier editor
2. Busca la línea `model = "..."` que quieras dejar activa
3. Deja solo una línea `model = "..."` activa si heredaste una plantilla vieja
4. Guarda y reinicia Codex Desktop

No necesitas tocar la sección `[model_providers.codex_shim]` — ese bloque ya
apunta al shim local y sirve para todos los modelos gestionados por este flujo.

Ejemplo — pasar de ChatGPT a DeepSeek:

```diff
-model = "gpt-5.5"
+model = "deepseek-v4-flash"
```

Si prefieres no editarlo a mano, `codex-shim model use <slug>` y
`codex-shim app` hacen ese cambio por ti.

---

## Gestión de Servidores

### codex-shim (Camino B)

```bash
# Arrancar servidor (genera catálogo + inicia)
cd ~/codex-shim
./bin/codex-shim start          # Solo inicia
./bin/codex-shim enable         # Inicia + instala config en Codex

# Ver estado
./bin/codex-shim status

# Listar modelos disponibles
./bin/codex-shim list

# Detener servidor
./bin/codex-shim stop           # Solo detiene
./bin/codex-shim disable        # Detiene + restaura config original

# Reiniciar
./bin/codex-shim restart

# Regenerar catálogo (sin reiniciar)
./bin/codex-shim generate

# Abrir Codex Desktop con shim activo
./bin/codex-shim app
# o con un modelo específico:
./bin/codex-shim app -m deepseek-v4-pro

# Ejecutar Codex CLI con shim
./bin/codex-shim codex [argumentos de codex ...]

# Parchear/restaurar Codex Desktop (una sola vez)
./bin/codex-shim patch-app
./bin/codex-shim restore-app
```

### Moon Bridge (Camino A)

```bash
cd ~/Documents/gitprojects/moon-bridge

# Iniciar servidor
make run
# o: go run ./cmd/moonbridge --config config.yml

# Detener servidor
make stop
# o: pkill -f moonbridge

# Verificar que está corriendo
curl http://127.0.0.1:38440/health

# Generar config para Codex CLI
make codex-config
```

---

## Flujo de Datos Detallado

### Modo Normal (ChatGPT — modelos first-party)

```
Usuario → Codex App/CLI
  → POST /v1/responses {model: "gpt-5.4-mini", ...}
  → Si está usando codex-shim:
      → codex-shim detecta un modelo passthrough de ChatGPT/Codex
      → Reenvía a chatgpt.com/backend-api/codex/responses con token de auth
      → ChatGPT devuelve respuesta nativa OpenAI Responses
      → codex-shim devuelve respuesta a Codex sin traducción
  → Si está usando Moon Bridge:
      → Moon Bridge no tiene modelos ChatGPT/Codex first-party; no aplica
```

### Modo DeepSeek (BYOK — vía codex-shim)

```
Usuario → Codex App/CLI
  → POST /v1/responses {model: "deepseek-v4-flash", ...}
  → codex-shim recibe la petición
  → Busca el modelo en ~/.codex-shim/models.json
  → Encuentra: provider="anthropic", base_url="https://api.deepseek.com/anthropic"
  → Traduce OpenAI Responses → Anthropic Messages:
      - input/instructions → system + messages
      - tools → tool_use tool_spec
      - thinking config → thinking block
      - stream → SSE con content_block_start/delta/stop
  → Envía POST /v1/messages a api.deepseek.com
  → Recibe respuesta Anthropic
  → Traduce Anthropic Messages → OpenAI Responses:
      - content blocks → output items
      - thinking blocks → reasoning blocks
      - tool_use → function_call
      - usage → usage
  → Devuelve respuesta a Codex
```

### Modo DeepSeek (BYOK — vía Moon Bridge directo)

```
Usuario → Codex CLI
  → POST /v1/responses {model: "moonbridge-flash", ...}
  → Moon Bridge recibe la petición
  → Consulta config.yml: route "moonbridge-flash" → deepseek-v4-flash
  → Busca provider "deepseek" → base_url + api_key
  → Traduce OpenAI Responses → Anthropic Messages
  → Envía a api.deepseek.com/anthropic/v1/messages
  → Traduce respuesta de vuelta a OpenAI Responses
  → Devuelve respuesta a Codex CLI
```

---

## Archivos Clave

| Archivo | Propósito |
|---------|-----------|
| `~/.codex/config.toml` | Configuración principal de Codex |
| `~/.codex/auth.json` | Token de acceso a ChatGPT (para modo normal) |
| `~/.codex-shim/models.json` | Fuente de verdad de modelos BYOK |
| `~/codex-shim/.codex-shim/custom_model_catalog.json` | Catálogo generado para Codex |
| `~/codex-shim/.codex-shim/shim.pid` | PID del servidor codex-shim |
| `~/codex-shim/.codex-shim/shim.log` | Logs del servidor codex-shim |
| `~/Documents/gitprojects/moon-bridge/config.yml` | Configuración de Moon Bridge |

---

## Resolución de Problemas

### "El selector solo muestra modelos ChatGPT"

```bash
# Asegúrate de haber ejecutado el parche (una sola vez):
cd ~/codex-shim && ./bin/codex-shim patch-app

# Verifica que el catálogo contiene todos los modelos:
cat ~/codex-shim/.codex-shim/custom_model_catalog.json | python3 -m json.tool

# Verifica que el shim está corriendo:
./bin/codex-shim status

# Reinstala la config en Codex:
./bin/codex-shim enable

# Reinicia Codex Desktop completamente
```

### "No tengo créditos de ChatGPT, quiero solo DeepSeek"

```bash
cd ~/codex-shim

# Detener shim (para desactivar ChatGPT passthrough)
./bin/codex-shim stop

# Desactivar ChatGPT passthrough
export CODEX_SHIM_DISABLE_CHATGPT=1

# Editar ~/.codex-shim/models.json — solo DeepSeek models

# Iniciar shim
./bin/codex-shim start

# Verificar
./bin/codex-shim list
```

### "codex-shim no arranca"

```bash
# Ver logs
cat ~/codex-shim/.codex-shim/shim.log | tail -50

# Verificar que el puerto no está ocupado
lsof -i :8765

# Verificar models.json tiene JSON válido
python3 -m json.tool ~/.codex-shim/models.json
```

### "Moon Bridge no arranca"

```bash
cd ~/Documents/gitprojects/moon-bridge

# Verificar config.yml
cat config.yml

# Verificar que el puerto no está ocupado
lsof -i :38440

# Compilar e iniciar con logs detallados
make run
```

---

## Makefile — Objetivos Disponibles

```bash
cd ~/Documents/gitprojects/moon-bridge

make build           # Compilar
make test            # Ejecutar tests
make run             # Iniciar Moon Bridge
make stop            # Detener Moon Bridge
make codex-config    # Generar config para Codex CLI
make mode-normal     # Cambiar a ChatGPT/Codex
make mode-deepseek   # Cambiar a DeepSeek V4 Flash (codex-shim)
make mode-deepseek-pro # Cambiar a DeepSeek V4 Pro (codex-shim)
make mode-moonbridge # Cambiar a Moon Bridge directo
make shim-start      # Iniciar codex-shim
make shim-stop       # Detener codex-shim
make shim-status     # Estado de codex-shim
make shim-list       # Listar modelos en codex-shim
```
