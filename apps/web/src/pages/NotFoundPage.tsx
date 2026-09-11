import { Brand } from "../components/Brand";
import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <main className="centered-page">
      <section className="panel status-panel">
        <Brand />
        <p className="eyebrow">404</p>
        <h1>Page not found</h1>
        <p>The page you requested does not exist in this dashboard.</p>
        <Link className="button-link" to="/">Return to NervOS</Link>
      </section>
    </main>
  );
}
