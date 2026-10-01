# PagaCheck Telegram Backend V1.2

Backend che collega il bot Telegram al prototipo PagaCheck V1.2.

## Variabili da impostare nel servizio di hosting
- BOT_TOKEN = token di BotFather (segreto, non pubblicarlo)
- PUBLIC_URL = URL pubblico assegnato dal servizio, es. https://pagacheck-xxxx.onrender.com
- WEBHOOK_SECRET = stringa casuale lunga
- PORT = fornita automaticamente dall'hosting

## Deploy
Runtime Docker. Health check: /health

Flusso: Telegram -> webhook -> backend -> OCR/lettura -> risposta Telegram.
