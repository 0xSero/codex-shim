@echo off
setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%;%PYTHONPATH%"
python -c "import aiohttp" 2>nul
if %ERRORLEVEL%==0 (
  python -m codex_shim.cli %*
  exit /b %ERRORLEVEL%
)
py -3.12 -m codex_shim.cli %*
exit /b %ERRORLEVEL%