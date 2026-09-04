FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# COPIA TUTTI I FILE (app.py + index.html)
COPY . .
EXPOSE 8420
CMD ["python", "app.py"]
