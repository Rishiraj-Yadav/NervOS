import { NavLink } from "react-router-dom";

const links = [["/", "Dashboard"], ["/agents", "Agents"], ["/packages", "Packages"], ["/marketplace", "Marketplace"], ["/conversations", "Conversations"], ["/memories", "Memory"], ["/workflows", "Workflows"], ["/connections", "Connections"], ["/security", "Security"], ["/automations", "Automations"], ["/runtime", "Runtime health"]];
export function AppNavigation() {
  return <nav aria-label="Main navigation" className="app-navigation">{links.map(([path, label]) => <NavLink key={path} to={path} end={path === "/"} aria-label={`Navigate to ${label.toLowerCase()}`}>{label}</NavLink>)}</nav>;
}
