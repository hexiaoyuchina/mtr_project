# 本地/CI 编译 linux/amd64 bgp_agent（无需 WSL）
FROM golang:1.21-bookworm
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    librocksdb-dev libsnappy-dev zlib1g-dev libbz2-dev liblz4-dev libzstd-dev \
    gcc g++ pkg-config \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY . .
ARG GOOS=linux
ARG GOARCH=amd64
ENV CGO_ENABLED=1 GOOS=${GOOS} GOARCH=${GOARCH}
ENV GOPROXY=https://goproxy.cn,direct
ENV GOSUMDB=sum.golang.google.cn
RUN go build -o /out/bgp_agent -ldflags="-s -w" .
