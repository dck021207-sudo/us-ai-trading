
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

RUN ls -la /app
RUN find /app -maxdepth 3 -type f

RUN mkdir -p /app/data

ENV DB_PATH=/app/data/trading.sqlite3
ENV PORT=8080

EXPOSE 8080

CMD ["python", "app.py"]

