package gobgp_path

import (
	"context"
	"fmt"
	"net"
	"strings"
	"time"

	api "github.com/osrg/gobgp/v3/api"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/types/known/anypb"
)

const defaultStreamChunk = 2000

// BuildIPv4UnicastPath 构造 IPv4 单播 LOCAL path（供 AddPathStream 批量写入）。
func BuildIPv4UnicastPath(prefix, nexthop string, withdraw bool) (*api.Path, error) {
	prefix = strings.TrimSpace(prefix)
	if prefix == "" {
		return nil, fmt.Errorf("empty prefix")
	}
	_, ipNet, err := net.ParseCIDR(prefix)
	if err != nil {
		return nil, err
	}
	pl, _ := ipNet.Mask.Size()
	nlri, err := anypb.New(&api.IPAddressPrefix{
		Prefix:    ipNet.IP.String(),
		PrefixLen: uint32(pl),
	})
	if err != nil {
		return nil, err
	}
	if withdraw {
		return &api.Path{
			Nlri:       nlri,
			Family:     &api.Family{Afi: api.Family_AFI_IP, Safi: api.Family_SAFI_UNICAST},
			IsWithdraw: true,
		}, nil
	}
	nh := strings.TrimSpace(nexthop)
	if nh == "" {
		nh = "0.0.0.0"
	}
	originAttr, _ := anypb.New(&api.OriginAttribute{Origin: 0})
	nhAttr, _ := anypb.New(&api.NextHopAttribute{NextHop: nh})
	return &api.Path{
		Nlri:   nlri,
		Pattrs: []*anypb.Any{originAttr, nhAttr},
		Family: &api.Family{Afi: api.Family_AFI_IP, Safi: api.Family_SAFI_UNICAST},
	}, nil
}

// AddPathStreamBatch 经 gRPC AddPathStream 批量写入 GLOBAL RIB（单次 propagateUpdate）。
func AddPathStreamBatch(ctx context.Context, grpcAddr string, paths []*api.Path, chunkSize int) error {
	if len(paths) == 0 {
		return nil
	}
	if chunkSize <= 0 {
		chunkSize = defaultStreamChunk
	}
	dctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	conn, err := grpc.DialContext(dctx, grpcAddr, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithBlock())
	if err != nil {
		return err
	}
	defer conn.Close()

	client := api.NewGobgpApiClient(conn)
	stream, err := client.AddPathStream(ctx)
	if err != nil {
		return err
	}
	for i := 0; i < len(paths); i += chunkSize {
		end := i + chunkSize
		if end > len(paths) {
			end = len(paths)
		}
		if err := stream.Send(&api.AddPathStreamRequest{
			TableType: api.TableType_GLOBAL,
			Paths:     paths[i:end],
		}); err != nil {
			return err
		}
	}
	_, err = stream.CloseAndRecv()
	return err
}
