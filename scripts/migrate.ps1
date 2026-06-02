param(
	[switch]$Local
)

$ErrorActionPreference = 'Stop'

if ($Local) {
	Write-Host 'Running local migration via project venv (python -m alembic).'
	Write-Host 'Using database_url from config/base.jsonc via Alembic config loader.'

	$pythonExe = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
	if (-not (Test-Path $pythonExe)) {
		throw "Python executable not found: $pythonExe"
	}

	& $pythonExe -m alembic upgrade head
	exit $LASTEXITCODE
}

Write-Host 'Running migration inside Docker api container.'
Write-Host 'Using database_url from config/base.jsonc via Alembic config loader.'

Push-Location (Join-Path $PSScriptRoot '..')
try {
	docker compose -f deploy/docker-compose.yml up -d postgres redis
	docker compose -f deploy/docker-compose.yml run --rm api sh -lc "pip install -e . >/tmp/pip.log 2>&1 && python -m alembic upgrade head"
}
finally {
	Pop-Location
}
