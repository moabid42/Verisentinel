FROM python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

LABEL org.opencontainers.image.title="Verisentinel execution capsule"
LABEL org.opencontainers.image.description="Fixed typed-operation capsule for a private mock endpoint"
LABEL org.opencontainers.image.vendor="Verisentinel"
LABEL org.opencontainers.image.version="1"

RUN groupadd --gid 65532 capsule \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin capsule \
    && install --directory --owner 65532 --group 65532 /run/verisentinel /opt/verisentinel

COPY --chown=65532:65532 entrypoint.py /opt/verisentinel/entrypoint.py

USER 65532:65532
ENTRYPOINT ["/usr/local/bin/python", "/opt/verisentinel/entrypoint.py"]
