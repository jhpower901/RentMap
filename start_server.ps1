Write-Host "Installing/Updating requirements..." -ForegroundColor Cyan
pip install -r requirements.txt

Write-Host "Starting RentMap Server..." -ForegroundColor Green
python scripts/server.py
