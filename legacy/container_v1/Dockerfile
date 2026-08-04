FROM debian:12-slim

LABEL org.opencontainers.image.source="https://github.com/Burdantes/scamper-cloud"
LABEL org.opencontainers.image.description="Containerized scamper measurement workload"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 scamper tini \
    && rm -rf /var/lib/apt/lists/*

COPY legacy/container_v1/run_scamper.py /usr/local/bin/run-scamper

RUN chmod 0755 /usr/local/bin/run-scamper \
    && mkdir -p /experiment /results

ENV TARGET_FILE=/experiment/targets.txt
ENV OUTPUT_DIR=/results
ENV SCAMPER_ARGS='-c "trace -m 20 -g 8 -w 3 -q 2 -P ICMP" -p 10000'

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/run-scamper"]
