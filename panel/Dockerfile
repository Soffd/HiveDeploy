FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app/ ./app/
COPY templates/ ./templates/

# static 目录（可为空）
RUN mkdir -p /app/static
COPY static/ ./static/

# 创建数据库目录
RUN mkdir -p /app/db

EXPOSE 3000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000", "--reload"]
