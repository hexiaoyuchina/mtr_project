package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"bgp_agent/pkg/rx"
	"bgp_agent/pkg/tx"
	"bgp_agent/pkg/processor"
	"bgp_agent/pkg/storage"
)

var (
	rrAddr     = flag.String("rr", "", "RR地址（留空则由 OP 通过 API 创建）")
	rrAs       = flag.Uint("rr-as", 0, "RR的AS号（与 -rr 同时使用）")
	localAs    = flag.Uint("local-as", 63199, "本地AS号")
	routerId   = flag.String("router-id", "139.159.43.207", "BGP Router ID（与 RR 直连本端地址）")
	redisAddr  = flag.String("redis", "localhost:6379", "Redis地址")
	rocksPath  = flag.String("rocksdb", "/var/lib/bgp_agent/rocksdb", "RocksDB路径")
	apiAddr    = flag.String("api", ":9179", "管理API监听地址")
)

func main() {
	flag.Parse()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	log.Printf("BGP Agent启动: RR=%s AS=%d LocalAS=%d RouterID=%s",
		*rrAddr, *rrAs, *localAs, *routerId)
	if *rrAddr == "" {
		log.Printf("RR 未在启动参数中配置，等待 OP 通过 /api/rr/config 或 BGP 管理页创建")
	}

	// 初始化存储层
	store, err := storage.NewStorage(*redisAddr, *rocksPath)
	if err != nil {
		log.Fatalf("初始化存储失败: %v", err)
	}
	defer store.Close()

	// 启动Route Processor
	proc := processor.NewProcessor(store)
	if err := proc.Start(ctx); err != nil {
		log.Fatalf("启动Route Processor失败: %v", err)
	}

	// 启动GoBGP RX（从RR接收路由）
	rxAgent, err := rx.NewRxAgent(&rx.Config{
		LocalAS:  uint32(*localAs),
		RouterID: *routerId,
		RRAddr:   *rrAddr,
		RRAS:     uint32(*rrAs),
	}, proc)
	if err != nil {
		log.Fatalf("创建RX Agent失败: %v", err)
	}
	if err := rxAgent.Start(ctx); err != nil {
		log.Fatalf("启动RX Agent失败: %v", err)
	}

	// TX 池：按 VRF 懒创建（卫星 VRF 多会话，对应原 FRR 多实例）
	txPool := tx.NewPool(&tx.Config{
		LocalAS:  uint32(*localAs),
		RouterID: *routerId,
	}, store, 1790, proc)
	if _, err := txPool.GetOrCreateDefault(ctx); err != nil {
		log.Printf("默认 TX 实例启动: %v", err)
	}

	// 启动管理API
	apiServer := NewAPIServer(*apiAddr, proc, rxAgent, txPool, store)
	go func() {
		if err := apiServer.Start(); err != nil {
			log.Printf("API服务错误: %v", err)
		}
	}()

	watchSec := 15
	if v := os.Getenv("MTR_BGP_PEER_WATCH_SEC"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			watchSec = n
		}
	}
	go apiServer.RunPeerWatch(ctx, time.Duration(watchSec)*time.Second)

	log.Printf("BGP Agent运行中，API监听: %s (peer_watch=%ds)", *apiAddr, watchSec)

	// 等待信号
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh

	log.Println("收到停止信号，开始优雅关闭...")
	cancel()

	// 等待组件关闭
	time.Sleep(3 * time.Second)
	log.Println("BGP Agent已停止")
}
