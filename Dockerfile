FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m billright && mkdir -p /data/uploads && chown billright /data/uploads
USER billright
EXPOSE 8000
# web: python -m billright.cli serve ; scheduler (G3 daily run): python -m billright.cli scheduler ; one-off: python -m billright.cli migrate
CMD ["python", "-m", "billright.cli", "serve", "--port", "8000"]
