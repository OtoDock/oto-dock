@echo off
setlocal
rem otodock — launch an interactive Claude/Codex session as your otodock agent in
rem the current folder. Windows twin of bin/otodock: a thin
rem wrapper around `python -m satellite.terminal.otodock_cli` using the satellite's venv.
rem
rem   otodock claude <agent>        :: interactive Claude TUI as <agent>, here
rem   otodock codex  <agent>        :: interactive Codex TUI
rem   otodock claude <agent> --folder C:\path\to\project
rem
rem Install: the satellite copies this onto your PATH automatically
rem (<otodock_dir>\bin on the user PATH); see __main__._ensure_otodock_on_path.

rem %~dp0 = this script's dir (...\satellite\bin\), with a trailing backslash.
rem SAT_DIR = parent (...\satellite, the package); ROOT_DIR = its parent (on PYTHONPATH).
for %%I in ("%~dp0..") do set "SAT_DIR=%%~fI"
for %%I in ("%~dp0..\..") do set "ROOT_DIR=%%~fI"

rem The venv lives at <pkg>\venv in the dev repo but at <root>\venv in an installed
rem satellite. Try both, then fall back to python on PATH.
set "PY="
if exist "%SAT_DIR%\venv\Scripts\python.exe" set "PY=%SAT_DIR%\venv\Scripts\python.exe"
if not defined PY if exist "%ROOT_DIR%\venv\Scripts\python.exe" set "PY=%ROOT_DIR%\venv\Scripts\python.exe"
if not defined PY set "PY=python"

rem PYTHONPATH must contain the dir HOLDING the `satellite` package — which differs
rem by layout: installed = one up from bin (<root>\satellite\), dev = two up (<repo>\).
rem Put BOTH on the path; `import satellite` resolves from whichever holds it.
set "PYTHONPATH=%SAT_DIR%;%ROOT_DIR%;%PYTHONPATH%"
"%PY%" -m satellite.terminal.otodock_cli %*
exit /b %ERRORLEVEL%
