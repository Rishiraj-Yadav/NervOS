import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  cancelApproval,
  createAccountConnection,
  createSecret,
  decideApproval,
  disconnectAccountConnection,
  getAccountConnections,
  getAccountProviders,
  startAccountAuthorization,
  getPendingApprovals,
  getPublisherTrust,
  getSecrets,
  securityKey,
  setPublisherTrust,
  setSecretStatus,
} from "../api/security";
import { ErrorState, InlineError, LoadingState } from "../components/AsyncState";

export function SecurityPage() {
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [provider, setProvider] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [scopes, setScopes] = useState("repo:read");
  const [secretId, setSecretId] = useState("");
  const [fingerprint, setFingerprint] = useState("");
  const [trustState, setTrustState] = useState<"trusted" | "untrusted" | "revoked">("revoked");

  const secrets = useQuery({ queryKey: [...securityKey, "secrets"], queryFn: getSecrets, retry: false });
  const accounts = useQuery({ queryKey: [...securityKey, "connections"], queryFn: getAccountConnections, retry: false });
  const providers = useQuery({ queryKey: [...securityKey, "providers"], queryFn: getAccountProviders, retry: false });
  const approvals = useQuery({ queryKey: [...securityKey, "approvals"], queryFn: getPendingApprovals, retry: false, refetchInterval: 1000 });
  const trust = useQuery({ queryKey: [...securityKey, "trust"], queryFn: getPublisherTrust, retry: false });
  const act = useMutation({
    mutationFn: (action: () => Promise<unknown>) => action(),
    onSuccess: () => client.invalidateQueries({ queryKey: securityKey }),
  });

  return (
    <main className="page-shell">
      <header className="page-header">
        <p className="eyebrow">Security isolation</p>
        <h1>Secrets, connections, approvals, and publisher trust</h1>
        <p>
          Stored credentials are write-only: NervOS never displays a secret value again.
          Approvals authorize exactly one dispatch of one exact external action.
        </p>
      </header>

      <section className="panel page-card">
        <h2>Add a secret</h2>
        <form
          className="integration-form"
          onSubmit={(event) => {
            event.preventDefault();
            act.mutate(async () => {
              const saved = await createSecret({
                name: name.trim(),
                value,
                ...(provider.trim() ? { provider_hint: provider.trim() } : {}),
              });
              setValue("");
              return saved;
            });
          }}
        >
          <label>
            Name
            <input required maxLength={128} value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label>
            Value
            <input
              required
              maxLength={8192}
              type="password"
              autoComplete="off"
              value={value}
              onChange={(e) => setValue(e.target.value)}
            />
          </label>
          <label>
            Provider hint (optional)
            <input maxLength={64} value={provider} onChange={(e) => setProvider(e.target.value)} />
          </label>
          <button disabled={act.isPending}>Store encrypted secret</button>
        </form>
        <p className="muted">
          The value is encrypted with the operator's master key before it is stored. If that key
          file is missing, every secret operation is refused rather than downgraded.
        </p>
        {act.isError ? <InlineError error={act.error} /> : null}
        {secrets.isPending ? <LoadingState message="Loading secrets…" /> : null}
        {secrets.isError ? <ErrorState error={secrets.error} onRetry={() => void secrets.refetch()} /> : null}
        <ul>
          {secrets.data?.map((secret) => (
            <li key={secret.id}>
              <strong>{secret.name}</strong> · {secret.status} · key v{secret.key_version} ·{" "}
              {secret.rotation_count} rotation(s)
              <div className="integration-actions">
                <button
                  className="secondary"
                  disabled={act.isPending || secret.status === "revoked"}
                  onClick={() =>
                    act.mutate(() =>
                      setSecretStatus(secret.id, secret.status === "active" ? "disabled" : "active"),
                    )
                  }
                >
                  {secret.status === "active" ? "Disable" : "Re-enable"}
                </button>
                <button
                  className="secondary"
                  disabled={act.isPending || secret.status === "revoked"}
                  onClick={() => act.mutate(() => setSecretStatus(secret.id, "revoked"))}
                >
                  Revoke permanently
                </button>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="panel page-card">
        <h2>Account connections</h2>
        <p>Authorize an account with an operator-configured provider. Review the requested scopes on the provider’s consent page.</p>
        {providers.isError ? <InlineError error={providers.error} /> : null}
        {providers.data?.length === 0 ? <p>No OAuth provider is configured. Ask the operator to configure account OAuth first.</p> : null}
        {providers.data?.map((item) => (
          <button key={item.id} disabled={act.isPending} onClick={() => act.mutate(async () => {
            const response = await startAccountAuthorization({
              provider: item.id, display_name: displayName.trim() || item.id, scopes: item.scopes,
            });
            window.location.assign(response.authorization_url);
          })}>Authorize {item.id}</button>
        ))}
        <p className="muted">
          A connection names one provider, the secret it uses, and the scopes granted. NervOS
          brokers the credential inside its own process; an agent never receives the value.
        </p>
        <form
          className="integration-form"
          onSubmit={(event) => {
            event.preventDefault();
            act.mutate(() =>
              createAccountConnection({
                provider: provider.trim() || "provider",
                display_name: displayName.trim(),
                secret_id: Number(secretId),
                scopes: scopes
                  .split(/[\s,]+/)
                  .map((scope) => scope.trim())
                  .filter(Boolean),
              }),
            );
          }}
        >
          <label>
            Provider
            <input required maxLength={64} value={provider} onChange={(e) => setProvider(e.target.value)} />
          </label>
          <label>
            Display name
            <input
              required
              maxLength={128}
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
            />
          </label>
          <label>
            Secret id
            <input required type="number" min={1} value={secretId} onChange={(e) => setSecretId(e.target.value)} />
          </label>
          <label>
            Scopes (comma separated)
            <input required value={scopes} onChange={(e) => setScopes(e.target.value)} />
          </label>
          <button disabled={act.isPending}>Connect account</button>
        </form>
        {accounts.isError ? <ErrorState error={accounts.error} onRetry={() => void accounts.refetch()} /> : null}
        <ul>
          {accounts.data?.map((connection) => (
            <li key={connection.id}>
              <strong>{connection.display_name}</strong> · {connection.provider} · {connection.state} ·{" "}
              {connection.scopes.join(", ")}
              <div className="integration-actions">
                <button
                  className="secondary"
                  disabled={act.isPending || connection.state !== "connected"}
                  onClick={() => act.mutate(() => disconnectAccountConnection(connection.id))}
                >
                  Disconnect
                </button>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="panel page-card">
        <h2>Action approvals</h2>
        <p className="muted">
          An external tool call waits for one decision here. Approving licenses exactly one
          dispatch of that exact call; it cannot be recalled once sent to the provider.
        </p>
        {approvals.isPending ? <LoadingState message="Loading approvals…" /> : null}
        {approvals.isError ? <ErrorState error={approvals.error} onRetry={() => void approvals.refetch()} /> : null}
        {approvals.data?.length === 0 ? <p>No action is waiting for a decision.</p> : null}
        <ul>
          {approvals.data?.map((approval) => (
            <li key={approval.id}>
              <strong>{approval.upstream_name}</strong> · run {approval.run_id} · sequence{" "}
              {approval.tool_sequence} · expires {approval.expires_at}
              <details>
                <summary>Reviewed action and redacted input</summary>
                <p>Action fingerprint</p>
                <code>{approval.fingerprint}</code>
                <p>Input digest</p>
                <code>{approval.input_digest}</code>
                <pre>{JSON.stringify(approval.preview, null, 2)}</pre>
              </details>
              <div className="integration-actions">
                <button disabled={act.isPending} onClick={() => act.mutate(() => decideApproval(approval.id, true))}>
                  Approve once
                </button>
                <button
                  className="secondary"
                  disabled={act.isPending}
                  onClick={() => act.mutate(() => decideApproval(approval.id, false))}
                >
                  Deny
                </button>
                <button
                  className="secondary"
                  disabled={act.isPending}
                  onClick={() => act.mutate(() => cancelApproval(approval.id))}
                >
                  Cancel request
                </button>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="panel page-card">
        <h2>Publisher trust</h2>
        <p className="muted">
          Signature verification proves a package is unmodified, not that its publisher is
          trustworthy. Revoking a signer locally blocks new installs and new executions of that
          signer's packages; installed software and history are left intact.
        </p>
        <form
          className="integration-form"
          onSubmit={(event) => {
            event.preventDefault();
            act.mutate(() => setPublisherTrust(fingerprint.trim(), { state: trustState }));
          }}
        >
          <label>
            Signer fingerprint
            <input required maxLength={64} value={fingerprint} onChange={(e) => setFingerprint(e.target.value)} />
          </label>
          <label>
            State
            <select value={trustState} onChange={(e) => setTrustState(e.target.value as typeof trustState)}>
              <option value="trusted">trusted</option>
              <option value="untrusted">untrusted</option>
              <option value="revoked">revoked</option>
            </select>
          </label>
          <button disabled={act.isPending}>Record decision</button>
        </form>
        {trust.isError ? <ErrorState error={trust.error} onRetry={() => void trust.refetch()} /> : null}
        <ul>
          {trust.data?.map((record) => (
            <li key={record.signer_fingerprint}>
              <code>{record.signer_fingerprint}</code> · {record.state}
              {record.reason ? ` · ${record.reason}` : ""}
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
