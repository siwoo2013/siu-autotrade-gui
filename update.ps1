Param([switch]$Headless = $false)

function Has-Py310 {
  try { py -3.10 -V *> $null; return $LASTEXITCODE -eq 0 } catch { return $false }
}

Write-Host "[update.ps1] Pulling latest..."
if (Get-Command git -ErrorAction SilentlyContinue) {
  try { git pull --ff-only } catch { Write-Warning "git pull failed: $_" }
} else {
  Write-Warning "git not found; skipping auto-update."
}

# venv (Python 3.10 우선)
if (-not (Test-Path .venv)) {
  if (Get-Command py -ErrorAction SilentlyContinue -and (Has-Py310)) { py -3.10 -m venv .venv }
  else { python -m venv .venv }
}

. ".\.venv\Scripts\Activate.ps1"
pip install -r requirements.txt
if ($Headless) { streamlit run app.py --server.headless true }
else { streamlit run app.py }
