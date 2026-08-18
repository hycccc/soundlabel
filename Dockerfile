FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml LICENSE README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[llm]"

# the label workspace lives on a volume so the catalog survives the container
VOLUME /label
ENTRYPOINT ["soundlabel", "-w", "/label"]
CMD ["demo"]
