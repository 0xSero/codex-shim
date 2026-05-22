@echo off
setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%;%PYTHONPATH%"
python -m codex_shim.cli %*