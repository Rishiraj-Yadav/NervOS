import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { connectionAction, createConnection, getConnections, getTools, integrationKey } from "../api/runtimeIntegration";
import { ErrorState, InlineError, LoadingState } from "../components/AsyncState";

export function ConnectionsPage() {
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<"stdio" | "http">("stdio");
  const [target, setTarget] = useState("");
  const [alias, setAlias] = useState("");
  const [before, setBefore] = useState<number | undefined>();
  const [toolBefore, setToolBefore] = useState<number | undefined>();
  const connections = useQuery({queryKey: [...integrationKey, "connections", before], queryFn: () => getConnections(before), retry: false});
  const tools = useQuery({queryKey: [...integrationKey, "tools", toolBefore], queryFn: () => getTools(undefined, toolBefore), retry: false});
  const actions = useMutation({mutationFn: (action: () => Promise<unknown>) => action(), onSuccess: () => client.invalidateQueries({queryKey: integrationKey})});
  return <main className="page-shell"><header className="page-header"><p className="eyebrow">Controlled external access</p><h1>MCP connections</h1><p>Discover tools from servers approved by your operator. Connection setup does not grant an agent access.</p></header>
    <section className="panel page-card"><h2>Add a connection</h2><form className="integration-form" onSubmit={e => {e.preventDefault(); actions.mutate(() => createConnection({display_name: name.trim(), transport, ...(transport === "stdio" ? {server_key: target.trim()} : {endpoint: target.trim()}), ...(alias.trim() ? {credential_ref: alias.trim()} : {})}));}}>
      <label>Display name<input required maxLength={100} value={name} onChange={e => setName(e.target.value)} /></label><label>Transport<select value={transport} onChange={e => {setTransport(e.target.value as "stdio" | "http"); setTarget("");}}><option value="stdio">Operator-approved local server</option><option value="http">Approved HTTPS server</option></select></label>
      <label>{transport === "stdio" ? "Operator server key" : "HTTPS endpoint"}<input required maxLength={transport === "stdio" ? 128 : 512} type={transport === "http" ? "url" : "text"} value={target} onChange={e => setTarget(e.target.value)} /></label><label>Credential alias (optional)<input maxLength={128} value={alias} onChange={e => setAlias(e.target.value)} /></label><p className="muted">Use the configured server key or approved public HTTPS target. Credentials are resolved on the host through an operator alias. Never paste a token here. OAuth account connections will be added in Stage H.</p><button disabled={actions.isPending}>Add connection</button>
    </form>{actions.isError ? <InlineError error={actions.error} /> : null}</section>
    <section className="panel page-card"><div className="section-heading"><h2>Your connections</h2><button className="secondary" onClick={() => void connections.refetch()}>Refresh list</button></div>{connections.isPending ? <LoadingState message="Loading connections…" /> : null}{connections.isError ? <ErrorState error={connections.error} onRetry={() => void connections.refetch()} /> : null}{tools.isError ? <ErrorState error={tools.error} onRetry={() => void tools.refetch()} /> : null}
      {connections.data?.items.length === 0 ? <p>No connections configured. Add one using an operator-approved target, then discover its tools.</p> : null}
      {connections.data?.items.map(c => <article key={c.id} className="connection-card"><div className="section-heading"><div><h3>{c.display_name}</h3><p className="muted">{c.transport} · {c.catalog_status} · {c.enabled ? "enabled" : "disabled"}</p></div><div className="integration-actions"><button disabled={actions.isPending || !c.enabled} onClick={() => actions.mutate(() => connectionAction(c.id, "refresh"))}>Discover / refresh tools</button><button className="secondary" disabled={actions.isPending} onClick={() => actions.mutate(() => connectionAction(c.id, c.enabled ? "disable" : "enable"))}>{c.enabled ? "Disable" : "Enable"}</button></div></div>{c.last_error_code ? <p role="status">Last discovery outcome: {c.last_error_code}</p> : null}
        <ul>{tools.data?.items.filter(t => t.connection_id === c.id).map(t => <li key={t.id}><strong>{t.display_name}</strong> · {t.status}<details><summary>Tool identity and schema digest</summary><p>{t.name}</p><code>{t.input_schema_sha256}</code></details></li>)}</ul><p className="muted">Manage bindings and permissions in an agent’s Tools & permissions section.</p>
      </article>)}
      <div className="integration-actions">{tools.data?.next_before_id ? <button className="secondary" onClick={() => setToolBefore(tools.data!.next_before_id!)}>Older tools</button> : null}{toolBefore ? <button className="secondary" onClick={() => setToolBefore(undefined)}>Newest tools</button> : null}{before ? <button className="secondary" onClick={() => setBefore(undefined)}>Newest connections</button> : null}{connections.data?.next_before_id ? <button className="secondary" onClick={() => setBefore(connections.data!.next_before_id!)}>Older connections</button> : null}</div>
    </section>
  </main>;
}
