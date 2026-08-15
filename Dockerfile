FROM PYTHON:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /workroom-bd

COPY requirement.txt .

RUN pip install --no-cache-dir --upgrade pip && pip install -r requirement.txt

COPY . .

EXPOSE 8000

CMD [ "python","manage.py", "runserver" ]