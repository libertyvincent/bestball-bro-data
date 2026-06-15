@echo off
REM Double-click entry point for the field-corpus refresh.
REM Runs the Python orchestrator (select -> strip -> validate -> privacy gate ->
REM sim-side derive -> stage on a branch). It STOPS before push; it never opens
REM a PR, never touches the extension table, never deletes the raw export.
REM
REM Pass --check for the reproduction/gate mode (strips to a scratch dir and
REM diffs against the committed boards_<date>.json; stages nothing).
setlocal
python "%~dp0refresh_corpus.py" %*
echo.
echo (refresh_corpus finished -- review the report above; nothing was pushed)
pause
