# Usiamo l'immagine ufficiale Microsoft con Python e i Browser già pronti
FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

WORKDIR /app

# Copiamo i tuoi file nel server
COPY . /app

# Installiamo le librerie Python
RUN pip install --no-cache-dir -r requirements.txt

# Comando di avvio
CMD ["python", "main.py"]