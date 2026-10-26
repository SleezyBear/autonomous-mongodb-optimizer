import { useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { login } from "../api/client";
import Mark from "../ui/Mark";

export default function Login() {
  const navigate = useNavigate();
  const location = useLocation();
  const client = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      await login(email, password);
      client.clear();
      const from = (location.state as { from?: string } | null)?.from;
      navigate(from && from !== "/login" ? from : "/");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="grid min-h-screen place-items-center px-5">
      <div className="w-full max-w-[380px]">
        <div className="mb-8 flex items-center gap-2.5">
          <Mark />
          <div className="leading-tight">
            <p className="font-semibold">NoSQL Optimizer</p>
            <p className="text-xs faint">Safe, proven index changes for MongoDB</p>
          </div>
        </div>
        <h1>Sign in</h1>
        <p className="mt-1.5 muted">Finds the indexes your database is missing, proves them on a copy, and applies them with approval.</p>
        <form className="panel mt-6 grid gap-4 p-5" onSubmit={submit}>
          <label className="field">Email
            <input aria-label="Email" autoComplete="username" className="input" onChange={(event) => setEmail(event.target.value)} required type="email" value={email} />
          </label>
          <label className="field">Password
            <input aria-label="Password" autoComplete="current-password" className="input" minLength={12} onChange={(event) => setPassword(event.target.value)} required type="password" value={password} />
          </label>
          <button className="btn btn-primary mt-1" disabled={busy} type="submit">{busy ? "Signing in…" : "Sign in"}</button>
          {error && <p className="text-sm tone-bad" role="alert">{error}</p>}
        </form>
        <p className="mt-4 text-sm faint">No account? An administrator can create one with <code>make user</code>.</p>
      </div>
    </main>
  );
}
