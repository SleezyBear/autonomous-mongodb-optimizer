import Mark from "./ui/Mark";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { api, logout } from "./api/client";
import { readSession } from "./lib/session";
import Approvals from "./pages/Approvals";
import Ledger from "./pages/Ledger";
import Login from "./pages/Login";
import NewRun from "./pages/NewRun";
import Overview from "./pages/Overview";
import Runs from "./pages/Runs";
import System from "./pages/System";
import TargetDetail from "./pages/TargetDetail";

// The run evidence view carries the charting library; load it on demand.
const RunDetail = lazy(() => import("./pages/RunDetail"));
import Targets from "./pages/Targets";

const NAV: readonly { to: string; label: string; icon: ReactNode }[] = [
  { to: "/", label: "Overview", icon: <path d="M3 10.5 10 4l7 6.5V17h-4.5v-4.5h-5V17H3z" /> },
  { to: "/targets", label: "Targets", icon: <><ellipse cx="10" cy="5" rx="6" ry="2.4" /><path d="M4 5v10c0 1.3 2.7 2.4 6 2.4s6-1.1 6-2.4V5M4 10c0 1.3 2.7 2.4 6 2.4s6-1.1 6-2.4" /></> },
  { to: "/runs", label: "Runs", icon: <path d="M3 10h3l2-5 4 10 2-5h3" /> },
  { to: "/approvals", label: "Approvals", icon: <path d="M4 10.5 8 14l8-8" /> },
  { to: "/ledger", label: "Ledger", icon: <path d="M5 3h10v14H5zM8 7h4M8 10h4M8 13h2" /> },
  { to: "/system", label: "System", icon: <><circle cx="10" cy="10" r="2.6" /><path d="M10 2.5v2.2M10 15.3v2.2M2.5 10h2.2M15.3 10h2.2M4.7 4.7l1.6 1.6M13.7 13.7l1.6 1.6M4.7 15.3l1.6-1.6M13.7 6.3l1.6-1.6" /></> },
];


function Shell({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me, staleTime: 60_000 });
  const inbox = useQuery({ queryKey: ["inbox"], queryFn: api.inbox, refetchInterval: 15_000, enabled: me.isSuccess });
  async function signOut() {
    await logout();
    client.clear();
    navigate("/login");
  }
  return (
    <div className="min-h-screen md:grid md:grid-cols-[232px_1fr] md:bg-[linear-gradient(to_right,var(--ink-1)_231px,var(--line-soft)_231px,var(--line-soft)_232px,transparent_232px)]">
      <aside className="border-b border-[var(--line-soft)] bg-[var(--ink-1)] md:sticky md:top-0 md:flex md:h-screen md:flex-col md:border-b-0 md:border-r">
        <div className="flex items-center gap-2.5 px-4 py-4">
          <Mark />
          <div className="leading-tight">
            <p className="font-semibold">NoSQL Optimizer</p>
            <p className="text-xs faint">MongoDB control plane</p>
          </div>
        </div>
        <nav aria-label="Primary" className="flex flex-wrap gap-1 px-3 pb-3 md:grid md:pb-0">
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.to === "/"} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
              <svg viewBox="0 0 20 20" className="h-[18px] w-[18px] shrink-0" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>{item.icon}</svg>
              <span className="flex-1">{item.label}</span>
              {item.to === "/approvals" && (inbox.data?.pending ?? 0) > 0 && <span className="rounded-full bg-[var(--warn)] px-1.5 text-xs font-semibold text-[#2b1a00]" aria-label={`${inbox.data?.pending} pending`}>{inbox.data?.pending}</span>}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-[var(--line-soft)] px-4 py-3 md:mt-auto">
          {me.data ? (
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate text-sm" title={me.data.email}>{me.data.email || "Signed in"}</p>
                <p className="text-xs faint">{me.data.role.charAt(0) + me.data.role.slice(1).toLowerCase()}</p>
              </div>
              <button className="btn btn-ghost h-8 px-2 text-sm" onClick={signOut} type="button">Sign out</button>
            </div>
          ) : <NavLink to="/login" className="text-sm">Sign in</NavLink>}
        </div>
      </aside>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function RequireSession({ children }: { children: ReactNode }) {
  const location = useLocation();
  const { accessToken, refreshToken } = readSession();
  if (!accessToken && !refreshToken) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="*" element={
        <RequireSession>
          <Shell>
            <Routes>
              <Route path="/" element={<Overview />} />
              <Route path="/targets" element={<Targets />} />
              <Route path="/targets/:id" element={<TargetDetail />} />
              <Route path="/runs" element={<Runs />} />
              <Route path="/runs/new" element={<NewRun />} />
              <Route path="/runs/:id" element={<Suspense fallback={<p className="p-8 muted">Loading run…</p>}><RunDetail /></Suspense>} />
              <Route path="/approvals" element={<Approvals />} />
              <Route path="/ledger" element={<Ledger />} />
              <Route path="/system" element={<System />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Shell>
        </RequireSession>
      } />
    </Routes>
  );
}
