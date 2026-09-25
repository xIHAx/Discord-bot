FROM python:3.12-slim

# Print straight to Cloud Run's logs instead of buffering, and skip .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, so code-only changes reuse this cached layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Don't run as root.
RUN useradd --create-home bot
USER bot

CMD ["python", "main.py"]
