FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLET_APP_STORAGE_DATA=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY Roboto ./Roboto

RUN mkdir -p /data

EXPOSE 8080

CMD ["python", "app.py"]
