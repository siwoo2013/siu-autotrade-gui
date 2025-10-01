
# TV Webhook → Bitget Trader (FastAPI)

Minimal API server to receive TradingView webhooks and place Bitget orders.

## Endpoints
- `GET /` → health check
- `POST /tv` → receive webhook JSON

### Webhook JSON example
```json
{ "secret":"MYSECRET", "symbol":"BTCUSDT", "side":"BUY", "qty":0.001, "type":"MARKET" }
```

## Run locally
```bash
pip install -r requirements.txt
export WEBHOOK_SECRET=MYSECRET
uvicorn server:app --reload
```

## Deploy on Render
- Build: `pip install -r requirements.txt`
- Start: `uvicorn server:app --host 0.0.0.0 --port $PORT`
- Set env vars: `WEBHOOK_SECRET`, `BITGET_API_KEY`, `BITGET_API_SECRET`, `BITGET_PASSPHRASE`
