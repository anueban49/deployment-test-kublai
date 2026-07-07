import { Navigate, Outlet } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";

// Wrap routes that require a signed-in user (a Supabase / Google session).
export default function ProtectedRouter() {
  const { session, loading } = useAuth();

  if (loading) return <p>Loading…</p>;
  if (!session) return <Navigate to="/" replace />;

  return <Outlet />;
}
