FROM python:3.11-slim
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 CASS_DRIVER_NO_EXTENSIONS=1 CASS_DRIVER_NO_CYTHON=1
RUN pip install --no-cache-dir "pymongo==4.13.2" "psycopg[binary]==3.2.3" "cassandra-driver==3.29.2" "neo4j==5.27.0" "redis==5.2.1" "numpy==1.26.4" "scipy==1.12.0" "docker==7.1.0"
WORKDIR /bench
