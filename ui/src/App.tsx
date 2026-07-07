import { Routes, Route } from "react-router-dom";
import Landing from "./pages/Landing";
import AuthCallback from "./pages/AuthCallback";
import Dashboard from "./pages/Dashboard";
// Payment flow temporarily disabled (WIP) — keep the page, just don't route to it.
// import Payment from "./pages/Payment";
import ProtectedRouter from "./components/protectedrouter";

function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/auth/callback" element={<AuthCallback />} />
      <Route element={<ProtectedRouter />}>
        <Route path="/dashboard" element={<Dashboard />} />
        {/* <Route path="/payment" element={<Payment />} /> */}
      </Route>
    </Routes>
  );
}

export default App;
