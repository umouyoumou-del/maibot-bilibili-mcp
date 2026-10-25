param(
    [ValidateSet("setup", "test", "lint", "format", "check", "smoke", "smoke-firefox", "upgrade")]
    [string]$Task = "check"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

switch ($Task) {
    "setup" {
        uv sync
    }
    "test" {
        uv run pytest -q
    }
    "lint" {
        uv run ruff check .
        uv run ruff format --check .
    }
    "format" {
        uv run ruff check --fix .
        uv run ruff format .
    }
    "check" {
        uv run ruff check .
        uv run ruff format --check .
        uv run pytest -q
        uv run python scripts/check_env.py
    }
    "smoke" {
        uv run python scripts/smoke_mcp.py
    }
    "smoke-firefox" {
        uv run python scripts/smoke_firefox_mcp.py
    }
    "upgrade" {
        uv lock --upgrade
        uv sync
    }
}
