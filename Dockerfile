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

# Standalone-deployment default (no docker-compose). docker-compose.yml
# overrides this with `uvicorn --reload` for local hot-reload dev; this is
# what runs if the image is deployed on its own (e.g. a PaaS without
# compose). Deliberately does NOT run migrate/collectstatic here -- those
# are a separate release step, see DEPLOYMENT.md.
CMD ["gunicorn", "crm_backend.asgi:application", "-k", "uvicorn_worker.UvicornWorker", \
     "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]