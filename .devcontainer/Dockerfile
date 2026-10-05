# syntax=docker/dockerfile:1
FROM mcr.microsoft.com/devcontainers/base:debian
ENV DEBIAN_FRONTEND=noninteractive
ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8

# OS-level packages. The container user is provided by the base image
# (devcontainers/base ships `vscode` at UID 1000). git, gh, fish and Claude Code
# come from devcontainer features; the Python environment comes from uv.
# dosfstools/exfatprogs/exfat-fuse let the container mount an SD card of either
# filesystem; udev and usbutils help when the card reader needs inspecting.
RUN apt update && \
    apt install -y --no-install-recommends \
    ca-certificates \
    curl \
    dosfstools \
    exfat-fuse \
    exfatprogs \
    udev \
    usbutils && \
    apt clean && \
    rm -rf /var/lib/apt/lists/*

# uv reads .python-version and uv.lock, so Python itself is not installed here.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

CMD ["/bin/bash"]
