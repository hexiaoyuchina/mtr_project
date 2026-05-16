package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"bgp_agent/pkg/rx"
	"bgp_agent/pkg/tx"
	"bgp_agent/pkg/processor"
	"bgp_agent/pkg/storage"
)

var (
	rrAddr     = flag.String("rr", "139.159.43.249", "RR地址")
	rrAs       = flag.Uint("rr-as", 63199, "RR的AS号")
	localAs    = flag.Uint("local-as", 63199, "本地AS号")
	routerId   = flag.String("router-id", "101.89.68.109", "BGP Router ID")
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

	// 启动GoBGP TX（向下游通告路由）
	txAgent, err := tx.NewTxAgent(&tx.Config{
		LocalAS:  uint32(*localAs),
		RouterID: *routerId,
	}, store)
	if err != nil {
		log.Fatalf("创建TX Agent失败: %v", err)
	}
	if err := txAgent.Start(ctx); err != nil {
		log.Fatalf("启动TX Agent失败: %v", err)
	}

	// 启动管理API
	apiServer := NewAPIServer(*apiAddr, proc, rxAgent, txAgent, store)
	go func() {
		if err := apiServer.Start(); err != nil {
			log.Printf("API服务错误: %v", err)
		}
	}()

	log.Printf("BGP Agent运行中，API监听: %s", *apiAddr)

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
