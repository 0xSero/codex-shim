@echo off
setlocal
set "ROOT=%~dp0.."
if "%1"=="" goto list
if "%1"=="list" goto list
call "%ROOT%\bin\codex-shim.cmd" model use %1
goto :eof
:list
call "%ROOT%\bin\codex-shim.cmd" model list