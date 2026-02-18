FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
ENV PYTHONUNBUFFERED=1

CMD ["bash","-lc","uvicorn main_darbottarolo:app --host 0.0.0.0 --port ${PORT} --workers 1"]
