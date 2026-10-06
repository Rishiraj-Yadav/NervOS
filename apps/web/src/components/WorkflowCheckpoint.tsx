import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "../api/client";
export function WorkflowCheckpoint({workflowId, revision}: {workflowId: number; revision: number}) {
  const [open, setOpen] = useState(false);
  const query = useQuery({queryKey: ["workflows", workflowId, "checkpoint", revision],
    enabled: open, queryFn: () => apiRequest<Record<string, unknown>>(
      `/workflows/${workflowId}/checkpoints/${revision}`,
      (value): value is Record<string, unknown> => value !== null && typeof value === "object"),
    staleTime: Infinity});
  return <div><button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
    {open ? "Hide" : "Inspect"} checkpoint {revision}</button>
    {open && query.isPending && <p role="status">Loading private state…</p>}
    {open && query.isError && <p role="alert">Could not load checkpoint.</p>}
    {open && query.data && <pre aria-label={`Private checkpoint ${revision}`}>
      {JSON.stringify(query.data.state, null, 2)}</pre>}
  </div>;
}
