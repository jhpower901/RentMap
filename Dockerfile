FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV TZ=Asia/Seoul

WORKDIR /app

# Install system dependencies (optional, but good for stability)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . .

# Expose the port the server runs on
EXPOSE 8000

# Run the web server by default
CMD ["python", "scripts/server.py"]
