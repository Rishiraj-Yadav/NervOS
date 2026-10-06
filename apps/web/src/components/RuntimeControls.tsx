import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { bindTool, decideSuggestion, getAgentTools, getPolicy, getRunContext, getSuggestions, getTools, integrationKey, permission, unbindTool, updatePolicy, type MemoryPolicy } from "../api/runtimeIntegration";
import { InlineError } from "./AsyncState";

export function AgentRuntimeControls({ instanceId }: { instanceId: number }) {
  const [toolsOpen, setToolsOpen] = useState(false);
  const [toolBefore, setToolBefore] = useState<number | undefined>();
  const tools = useQuery({queryKey: [...integrationKey, "agent-tools", instanceId], queryFn: () => getAgentTools(instanceId), enabled: toolsOpen, retry: false});
  const available = useQuery({queryKey: [...integrationKey, "tools", toolBefore], queryFn: () => getTools(undefined, toolBefore), enabled: toolsOpen, retry: false});
  const client = useQueryClient();
  const mutation = useMutation({mutationFn: (action: () => Promise<unknown>) => action(), onSuccess: () => client.invalidateQueries({queryKey: integrationKey})});
  const [selection, setSelection] = useState<Record<string, string>>({});
  return <section className="integration-section" aria-label="Agent runtime controls">
    <MemoryPolicyControl instanceId={instanceId} />
    <details className="panel page-card" onToggle={e => setToolsOpen(e.currentTarget.open)}><summary>Tools & permissions</summary><p>Bind a declared tool to one connection, then explicitly grant access. Revocation blocks future dispatch; an already dispatched action cannot be recalled.</p>
      {toolsOpen && (tools.isPending || available.isPending) ? <ControlLoading message="Loading tools…" /> : null}
      {tools.isError ? <ControlError error={tools.error} onRetry={() => void tools.refetch()} /> : null}
      {available.isError ? <ControlError error={available.error} onRetry={() => void available.refetch()} /> : null}
      {mutation.isError ? <InlineError error={mutation.error} /> : null}
      {tools.data && available.data ? <>
        {tools.data.requirements.length === 0 ? <p>No portable tool requirements declared by this package.</p> : tools.data.requirements.map(r => { const binding = tools.data.bindings.find(b => b.alias === r.alias); const matching = binding && available.data.items.find(t => t.id === binding.definition_id && t.fingerprint === binding.fingerprint && t.status === "available"); return <div className="integration-row" key={r.alias}><label>{r.alias} ({r.required ? "required" : "optional"})<span className="muted">{matching ? "Binding available" : binding ? "Binding needs review or refresh" : r.required ? "Required tool is not bound" : "Optional tool is not bound"}</span><select value={selection[r.alias] ?? String(binding?.definition_id ?? "")} onChange={e => setSelection({...selection, [r.alias]: e.target.value})}><option value="">Select matching tool</option>{available.data.items.filter(t => (t.source_kind === "mcp" || (t.source_kind === "builtin" && ["gmail.users.messages.list", "gmail.users.messages.get"].includes(t.name))) && t.name === r.upstream_name && t.input_schema_sha256 === r.input_schema_sha256 && t.status === "available").map(t => <option key={t.id} value={t.id}>{t.display_name}{t.connection_id ? ` · connection ${t.connection_id}` : " · native connector"}</option>)}</select></label><button disabled={mutation.isPending || !selection[r.alias]} onClick={() => mutation.mutate(() => bindTool(instanceId, r.alias, Number(selection[r.alias])))}>Save binding</button>{binding ? <button className="secondary" disabled={mutation.isPending} onClick={() => mutation.mutate(() => unbindTool(instanceId, r.alias))}>Unbind</button> : null}</div>; })}
        <div className="table-scroll"><table className="integration-table"><thead><tr><th>Tool / source</th><th>Status</th><th>Access</th><th>Actions</th></tr></thead><tbody>{available.data.items.map(t => {const grant = tools.data.grants.find(g => g.definition_id === t.id); const drift = grant && grant.fingerprint !== t.fingerprint; return <tr key={t.id}><td>{t.display_name}<small>{t.source_kind}{t.connection_id ? ` · connection ${t.connection_id}` : ""}</small></td><td>{t.status}</td><td>{drift ? "Changed — review required" : grant ? "Granted" : "Not granted"}</td><td>{grant ? <>{drift ? <button className="secondary" disabled={mutation.isPending || t.status !== "available"} onClick={() => mutation.mutate(() => permission(instanceId, t.id, "reconfirm"))}>Reconfirm</button> : null}<button className="secondary" disabled={mutation.isPending} onClick={() => mutation.mutate(() => permission(instanceId, t.id, "revoke"))}>Revoke</button></> : <button className="secondary" disabled={mutation.isPending || t.status !== "available"} onClick={() => mutation.mutate(() => permission(instanceId, t.id, "grant"))}>Grant</button>}</td></tr>;})}</tbody></table></div>
        {available.data.next_before_id ? <button className="secondary" onClick={() => setToolBefore(available.data!.next_before_id!)}>Older available tools</button> : null}
        {toolBefore ? <button className="secondary" onClick={() => setToolBefore(undefined)}>Newest available tools</button> : null}
        <h3>Recent tool invocations</h3>{tools.data.history.length ? <ul>{tools.data.history.map(h => <li key={h.id}>Run #{h.run_id} · {h.name} · {h.status}{h.error_code ? ` · ${h.error_code}` : ""}</li>)}</ul> : <p>No tool invocations yet.</p>}
      </> : null}
    </details>
    <MemorySuggestions instanceId={instanceId} />
  </section>;
}

export function MemoryPolicyControl({instanceId}: {instanceId: number}) {
  const [open, setOpen] = useState(false);
  const policy = useQuery({queryKey: [...integrationKey, "policy", instanceId], queryFn: () => getPolicy(instanceId), enabled: open, retry: false});
  const [draft, setDraft] = useState<MemoryPolicy["mode"] | null>(null);
  const [extract, setExtract] = useState<boolean | null>(null);
  const client = useQueryClient();
  const save = useMutation({mutationFn: () => updatePolicy(instanceId, {mode: draft ?? policy.data!.mode, extraction_enabled: (draft ?? policy.data!.mode) === "manual" ? false : extract ?? policy.data!.extraction_enabled, expected_revision: policy.data!.revision}), onSuccess: () => { setDraft(null); setExtract(null); return client.invalidateQueries({queryKey: integrationKey}); }});
  return <details className="panel page-card" onToggle={e => setOpen(e.currentTarget.open)}><summary>Memory policy</summary><p>Choose what this agent may retain for later runs. User-profile facts always require review. Existing run snapshots do not change.</p>
    {open && policy.isPending ? <ControlLoading message="Loading memory policy…" /> : null}{policy.isError ? <ControlError error={policy.error} onRetry={() => void policy.refetch()} /> : null}
    {policy.data ? <form onSubmit={e => {e.preventDefault(); save.mutate();}}><label>Memory saving<select value={draft ?? policy.data.mode} onChange={e => setDraft(e.target.value as MemoryPolicy["mode"])}><option value="manual">Manual only</option><option value="review">Review agent suggestions</option><option value="automatic_private">Automatically save private agent facts</option></select></label>
      <label className="checkbox-label"><input type="checkbox" checked={(draft ?? policy.data.mode) !== "manual" && (extract ?? policy.data.extraction_enabled)} disabled={(draft ?? policy.data.mode) === "manual"} onChange={e => setExtract(e.target.checked)} />Extract facts after successful runs</label><p className="muted">Extraction creates an additional queued model run and may incur provider costs. It uses bounded excerpts; saving is best effort and does not guarantee factual accuracy.</p>{(draft ?? policy.data.mode) === "automatic_private" ? <p>Saving this policy explicitly authorizes this agent to add private facts. You can inspect, edit or delete them in Memory.</p> : null}<button disabled={save.isPending}>{save.isPending ? "Saving…" : "Save memory policy"}</button>{save.isError ? <InlineError error={save.error} /> : null}{save.isSuccess ? <p role="status">Memory policy saved.</p> : null}</form> : null}
  </details>;
}

export function MemorySuggestions({instanceId}: {instanceId?: number}) {
  const [open, setOpen] = useState(false);
  const [before, setBefore] = useState<number | undefined>();
  const query = useQuery({queryKey: [...integrationKey, "suggestions", instanceId ?? "all", before], queryFn: () => getSuggestions(instanceId, before), enabled: open, refetchInterval: open ? 5000 : false, retry: false});
  const client = useQueryClient();
  const decision = useMutation({mutationFn: ({id, approve}: {id: number; approve: boolean}) => decideSuggestion(id, approve), onSuccess: async () => {await client.invalidateQueries({queryKey: integrationKey}); await client.invalidateQueries({queryKey: ["memories"]});}});
  return <details className="panel page-card" onToggle={e => setOpen(e.currentTarget.open)}><summary>Memory suggestions & saved facts</summary><p>Suggestions show their source run and retention outcome. Approve useful facts or dismiss them.</p>{query.isPending && open ? <ControlLoading message="Loading suggestions…" /> : null}{query.isError ? <ControlError error={query.error} onRetry={() => void query.refetch()} /> : null}{decision.isError ? <InlineError error={decision.error} /> : null}
    {query.data?.items.length === 0 ? <p>No memory suggestions yet.</p> : null}
    {query.data?.items.map(s => <article className="memory-suggestion" key={s.id}><p className="muted">Agent #{s.agent_instance_id} · Run #{s.source_run_id} · {s.scope} · {s.state}</p><p>{s.content}</p>{s.state === "pending" ? <div className="integration-actions"><button disabled={decision.isPending} onClick={() => decision.mutate({id: s.id, approve: true})}>Approve fact</button><button className="secondary" disabled={decision.isPending} onClick={() => decision.mutate({id: s.id, approve: false})}>Dismiss</button></div> : null}</article>)}
    <div className="integration-actions">{before ? <button className="secondary" onClick={() => setBefore(undefined)}>Newest suggestions</button> : null}{query.data?.next_before_id ? <button className="secondary" onClick={() => setBefore(query.data!.next_before_id!)}>Older suggestions</button> : null}</div>
  </details>;
}

export function RunContextPanel({runId}: {runId: number}) {
  const [open, setOpen] = useState(false);
  return <details className="run-context-panel" onToggle={e => setOpen(e.currentTarget.open)}><summary>Context & memory used</summary>{open ? <RunContextDetails runId={runId} /> : null}</details>;
}

function RunContextDetails({runId}: {runId: number}) {
  const open = true;
  const context = useQuery({queryKey: [...integrationKey, "context", runId], queryFn: () => getRunContext(runId), enabled: open, retry: false, refetchInterval: open ? 5000 : false});
  return <div>{open && context.isPending ? <ControlLoading message="Loading run context…" /> : null}{context.isError ? <ControlError error={context.error} onRetry={() => void context.refetch()} /> : null}{context.data ? <><p className="muted">Memory outcome: {context.data.memory_state}. Context: {context.data.context_bytes}/{context.data.context_limit_bytes} bytes.</p>{context.data.available ? <><h4>Selected memory versions</h4>{context.data.memories.length ? <ul>{context.data.memories.map(m => <li key={m.id}>#{m.id} v{m.version} · {m.scope}: {m.content}</li>)}</ul> : <p>No memory selected for this run.</p>}<h4>Exact retained context</h4><pre>{context.data.rendered_context}</pre></> : <p>This historical run has no retained context snapshot.</p>}</> : null}</div>;
}

function ControlLoading({message}: {message: string}) { return <p role="status" aria-busy="true">{message}</p>; }
function ControlError({error, onRetry}: {error: unknown; onRetry: () => void}) { return <div><InlineError error={error} /><button className="secondary" onClick={onRetry}>Try again</button></div>; }
