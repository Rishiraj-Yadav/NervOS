import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { getRun } from "../api/agentInstances";
import { ErrorState, LoadingState } from "../components/AsyncState";
import { RunItem } from "../components/RunItem";
import { RunTimeline } from "../components/RunTimeline";

export function RunPage() {
  const id = Number(useParams().runId);
  const query = useQuery({
    queryKey: ["runs", id], queryFn: () => getRun(id), enabled: Number.isSafeInteger(id) && id > 0,
    refetchInterval: (q) => q.state.data && ["created", "running"].includes(q.state.data.status) ? 1500 : false,
  });
  if (!Number.isSafeInteger(id) || id <= 0) return <p role="alert">Invalid Run.</p>;
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const run = query.data;
  return <main className="page"><Link to="/workflows">Back to workflows</Link><h1>Run #{id}</h1>
    <RunItem run={run} timeline={<RunTimeline runId={id} isTerminal={!["created", "running"].includes(run.status)} />} />
  </main>;
}
