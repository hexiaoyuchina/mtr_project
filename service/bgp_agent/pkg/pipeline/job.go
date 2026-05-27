package pipeline

import "time"

type JobKind string

const (
	JobKindIngest  JobKind = "rib_ingest"
	JobKindFib     JobKind = "fib_recompute"
	JobKindRepair  JobKind = "repair"
	JobKindKernel  JobKind = "kernel_reconcile"
)

type JobStatus string

const (
	JobPending JobStatus = "pending"
	JobRunning JobStatus = "running"
	JobDone    JobStatus = "done"
	JobError   JobStatus = "error"
)

// Job 可追踪的后台任务（ingest / FIB recompute / repair）。
type Job struct {
	ID         string    `json:"job_id"`
	Kind       JobKind   `json:"kind"`
	Status     JobStatus `json:"status"`
	Window     string    `json:"window,omitempty"`
	VRF        string    `json:"vrf,omitempty"`
	NeighborIP string    `json:"neighbor_ip,omitempty"`
	SourceIP   string    `json:"source_ip,omitempty"`
	Message    string    `json:"message,omitempty"`
	Processed  int64     `json:"processed"`
	Total      int64     `json:"total"`
	Ingested   int       `json:"ingested,omitempty"`
	Removed    int       `json:"removed,omitempty"`
	StartedAt  time.Time `json:"started_at,omitempty"`
	FinishedAt time.Time `json:"finished_at,omitempty"`
}

func (j *Job) snapshot() Job {
	if j == nil {
		return Job{}
	}
	cp := *j
	return cp
}
