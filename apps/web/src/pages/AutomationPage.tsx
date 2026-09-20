import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { historyQuery, triggerQuery, useTriggerActions } from "../api/triggerQueries";
import type { Trigger, TriggerCreate, TriggerUpdate } from "../api/triggers";
import { ErrorState, InlineError, LoadingState } from "../components/AsyncState";

function SecretBanner({ secret, onDismiss }: { secret: string; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    await navigator.clipboard.writeText(secret);
    setCopied(true);
  }
  return (
    <aside className="secret-banner">
      <strong>Copy this secret now. It cannot be recovered after dismissing.</strong>
      <code>{secret}</code>
      <div className="button-row">
        <button type="button" onClick={() => void copy()}>{copied ? "Copied" : "Copy secret"}</button>
        <button type="button" className="secondary" onClick={onDismiss}>Dismiss</button>
      </div>
    </aside>
  );
}

function EditForm({ trigger, onDone }: { trigger: Trigger; onDone: () => void }) {
  const actions = useTriggerActions();
  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const body: TriggerUpdate = {
      expected_config_revision: trigger.config_revision,
      display_name: String(data.get("display_name")),
      input_text: String(data.get("input_text")),
    };
    actions.update.mutate({ id: trigger.id, body }, { onSuccess: onDone });
  }
  return (
    <form className="form-stack" onSubmit={submit}>
      <label>Name<input name="display_name" defaultValue={trigger.display_name} required /></label>
      <label>Input text<textarea name="input_text" defaultValue={trigger.input_text} required /></label>
      {actions.update.error ? <InlineError error={actions.update.error} /> : null}
      <button type="submit" disabled={actions.update.isPending}>{actions.update.isPending ? "Saving…" : "Save changes"}</button>
    </form>
  );
}

export function AutomationPage() {
  const id = Number(useParams().automationId);
  const query = useQuery(triggerQuery(id));
  const history = useQuery(historyQuery(id));
  const [secret, setSecret] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const actions = useTriggerActions(setSecret);
  const navigate = useNavigate();
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const trigger = query.data;
  return (
    <main className="page-shell">
      <p><Link to="/automations">← Automations</Link></p>
      <section className="panel page-card">
        <p className="eyebrow">{trigger.kind}</p>
        {editing ? <EditForm trigger={trigger} onDone={() => setEditing(false)} /> : <>
          <h1>{trigger.display_name}</h1>
          <p>{trigger.input_text}</p>
          <p>Status: {trigger.enabled ? "Enabled" : "Disabled"}</p>
          {trigger.webhook_path ? <p>Webhook path: <code>{trigger.webhook_path}</code></p> : null}
          {trigger.secret_created_at ? <p>Secret created: {trigger.secret_created_at}</p> : null}
          <div className="button-row">
            <button type="button" onClick={() => setEditing(true)}>Edit</button>
            <button type="button" onClick={() => actions.toggle.mutate({ id: trigger.id, enabled: !trigger.enabled })}>{trigger.enabled ? "Disable" : "Enable"}</button>
            {trigger.kind === "webhook" ? <button type="button" className="secondary" onClick={() => actions.rotate.mutate(trigger.id)}>Rotate secret</button> : null}
            <button type="button" className="secondary" onClick={() => { if (confirm("Delete this automation?")) actions.remove.mutate(trigger.id, { onSuccess: () => navigate("/automations") }); }}>Delete</button>
          </div>
        </>}
        {secret ? <SecretBanner secret={secret} onDismiss={() => setSecret(null)} /> : null}
      </section>
      <section className="panel page-card">
        <h2>Occurrence history</h2>
        {history.isPending ? <LoadingState /> : history.isError ? <ErrorState error={history.error} onRetry={() => void history.refetch()} /> : history.data.items.length === 0 ? <p>No runs yet.</p> : <ul className="stack">{history.data.items.map((occurrence) => <li className="list-row" key={occurrence.id}><span>{occurrence.status}</span>{occurrence.run_id ? <Link to={`/agents/${trigger.agent_instance_id}`}>Run {occurrence.run_id}</Link> : <span>{occurrence.skip_code ?? "No Run"}</span>}</li>)}</ul>}
      </section>
    </main>
  );
}

export function NewAutomationPage() {
  const [secret, setSecret] = useState<string | null>(null);
  const [created, setCreated] = useState<Trigger | null>(null);
  const [kind, setKind] = useState<TriggerCreate["kind"]>("one_time");
  const actions = useTriggerActions(setSecret);
  const navigate = useNavigate();
  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const body: TriggerCreate = { kind, agent_instance_id: Number(form.get("agent_instance_id")), display_name: String(form.get("display_name")), input_text: String(form.get("input_text")), enabled: true };
    if (kind === "interval") body.interval_seconds = Number(form.get("interval_seconds"));
    if (kind === "cron") { body.cron_expression = String(form.get("cron_expression")); body.timezone = String(form.get("timezone")); }
    if (kind === "one_time") body.run_at = String(form.get("run_at"));
    if (kind === "event") body.event_type = String(form.get("event_type"));
    actions.create.mutate(body, {
      onSuccess: (trigger) => {
        setCreated(trigger);
        if (kind !== "webhook") navigate(`/automations/${trigger.id}`);
      },
    });
  }
  return <main className="page-shell"><section className="panel page-card"><h1>Create automation</h1><form className="form-stack" onSubmit={submit}><label>Kind<select value={kind} onChange={(event) => setKind(event.target.value as TriggerCreate["kind"])}>{["one_time", "interval", "cron", "webhook", "event"].map((value) => <option key={value}>{value}</option>)}</select></label><label>Agent instance ID<input name="agent_instance_id" type="number" required /></label><label>Name<input name="display_name" required /></label><label>Input text<textarea name="input_text" required /></label>{kind === "interval" ? <label>Interval seconds<input name="interval_seconds" type="number" min="60" required /></label> : null}{kind === "cron" ? <><label>Cron expression<input name="cron_expression" required /></label><label>Timezone<input name="timezone" required /></label></> : null}{kind === "one_time" ? <label>Run at<input name="run_at" type="datetime-local" required /></label> : null}{kind === "event" ? <label>Event type<input name="event_type" required /></label> : null}<button type="submit" disabled={actions.create.isPending}>{actions.create.isPending ? "Creating…" : "Create"}</button></form>{created?.webhook_path ? <p>Webhook path: <code>{created.webhook_path}</code> · <Link to={`/automations/${created.id}`}>Open automation</Link></p> : null}{secret ? <SecretBanner secret={secret} onDismiss={() => setSecret(null)} /> : null}</section></main>;
}
