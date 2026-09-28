# Runs the full test suite (unit + end-to-end with a simulated GPU; the real GPU is never touched).
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
uv run --group dev pytest -v @args
