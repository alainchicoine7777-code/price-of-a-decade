# The Price of a Decade — Production Web App

Production-ready starter for `PriceOfADecade.com` and the permanent calculator route `PriceOfADecade.com/calculator`.

## Features
- Book-matched deep navy / black / gold / warm-white visual system
- 2026 selectable Canadian province/territory tax estimate
- Direct disposable-income path
- Human Time Value and economic-cost-to-time translation
- Public-spending translator
- National live feed from Statistics Canada, Bank of Canada, and Finance Canada
- Render blueprint and health endpoint

## Local run
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app:app --reload
```
Open http://127.0.0.1:8000/calculator

## Render
Create a GitHub repository containing this folder. In Render, create a Blueprint from the repo and Render will read `render.yaml`.

After deployment, add `priceofadecade.com` as the custom domain in Render and point the GoDaddy DNS records to the values Render provides. Set `www.priceofadecade.com` as an alias if desired. Keep `/calculator` unchanged because it is the address used in the book/QR code.

## Tax model
The gross-income path is a simplified 2026 employment-income estimator. It uses federal and provincial/territorial progressive brackets, basic personal amounts, CPP/QPP, EI, QPIP in Quebec, the Quebec federal abatement, and Ontario surtax. It intentionally remains an estimator rather than a tax-return engine. The direct disposable-income path bypasses tax estimation.
