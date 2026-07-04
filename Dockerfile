FROM python:3.12-slim

WORKDIR /app

# Обновляем pip и ставим зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

# Railway задаёт PORT для web-сервисов, но polling нам порт не нужен.
# Просто запускаем main.py как процесс.
CMD ["python", "main.py"]
