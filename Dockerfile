FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /workroom-bd

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Create a non-root user
RUN groupadd --gid 1000 workroom-bd \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash workroom-bd

COPY . .

RUN chown -R workroom-bd:workroom-bd /workroom-bd

USER workroom-bd

EXPOSE 8000

WORKDIR /workroom-bd/crm_backend

CMD ["uvicorn", "crm_backend.asgi:application", "--host", "0.0.0.0", "--port", "8000"]