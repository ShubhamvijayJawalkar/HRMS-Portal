FROM python:3.13-slim AS base

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS production

COPY . .

EXPOSE 5000

ENV FLASK_DEBUG=0
ENV PYTHONUNBUFFERED=1

CMD ["python", "app.py"]
