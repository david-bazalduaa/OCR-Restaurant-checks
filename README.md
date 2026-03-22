# Castillo Telegram Bot

MVP:
- recibir fotos desde Telegram
- webhook en Modal
- luego OCR + parser + Google Sheets

## Desarrollo local
1. Crear entorno virtual
2. Instalar dependencias
3. Configurar secrets en Modal
4. Ejecutar:
   modal serve src/main.py

## Deploy
modal deploy -m src.main