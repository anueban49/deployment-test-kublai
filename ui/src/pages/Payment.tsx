import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import "../App.css";
import { useAuth } from "../hooks/useAuth";
import { supabase } from "../lib/supabaseClient";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

type PaymentOrder = {
  id: string;
  status: "PENDING" | "PAID";
  amount: number;
  currency: string;
  qr_text: string | null;
  qr_image: string | null;
};

function qrImageSrc(qrImage: string | null): string | null {
  if (!qrImage) return null;
  if (qrImage.startsWith("data:image")) return qrImage;
  return `data:image/png;base64,${qrImage}`;
}

function money(amount: number, currency: string): string {
  return `${new Intl.NumberFormat("mn-MN").format(amount)} ${currency}`;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, init);
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(body?.detail ?? `Request failed (${res.status})`);
  }
  return body as T;
}

const BENEFITS = [
  "Хязгааргүй хайлт (Basic: 7 хоногт 3 удаа)",
  "Өглөө бүр 07:00-д шинэ зарууд автоматаар",
  "AI ангилал (худалдагч/худалдан авагч)",
];

type LinkStatus = {
  linked: boolean;
  telegram_username: string | null;
};

// The backend identifies the caller by verifying this JWT server-side —
// no user id travels in the request body.
async function authHeaders(): Promise<Record<string, string>> {
  const {
    data: { session },
  } = await supabase.auth.getSession();
  return { Authorization: `Bearer ${session?.access_token ?? ""}` };
}

// Web account <-> Telegram bot linking: mint a one-time t.me deep link,
// then poll until the bot reports the accounts bound.
function TelegramLink() {
  const [status, setStatus] = useState<LinkStatus | null>(null);
  const [linkUrl, setLinkUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const fetchStatus = useCallback(async () => {
    try {
      const s = await api<LinkStatus>("/telegram/link-status", {
        headers: await authHeaders(),
      });
      setStatus(s);
      return s;
    } catch {
      return null; // polling: stay quiet, try again next tick
    }
  }, []);

  useEffect(() => {
    void fetchStatus();
  }, [fetchStatus]);

  // After the deep link is shown, poll until the bot binds the account.
  useEffect(() => {
    if (!linkUrl || status?.linked) return;
    const id = window.setInterval(() => void fetchStatus(), 4000);
    return () => window.clearInterval(id);
  }, [fetchStatus, linkUrl, status?.linked]);

  async function createLink() {
    setBusy(true);
    setError("");
    try {
      const response = await api<{ url: string }>("/telegram/link-url", {
        method: "POST",
        headers: await authHeaders(),
      });
      setLinkUrl(response.url);
      window.open(response.url, "_blank", "noreferrer");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Холбоос үүсгэж чадсангүй.");
    } finally {
      setBusy(false);
    }
  }

  if (status?.linked) {
    return (
      <p className="tg-linked">
        ✅ Telegram холбогдсон
        {status.telegram_username ? ` (@${status.telegram_username})` : ""}
      </p>
    );
  }

  return (
    <div className="tg-link">
      <p className="muted">
        Telegram bot-оо холбовол төлбөр баталгаажмагц Telegram-аар мэдэгдэнэ.
      </p>
      {linkUrl ? (
        <p className="muted">
          <a className="link" href={linkUrl} target="_blank" rel="noreferrer">
            Telegram нээх →
          </a>{" "}
          Bot дээр Start дарсны дараа энд автоматаар шинэчлэгдэнэ…
        </p>
      ) : (
        <button className="secondary" onClick={createLink} disabled={busy}>
          {busy ? "Үүсгэж байна…" : "Telegram холбох"}
        </button>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

export default function Payment() {
  // ProtectedRouter guarantees a signed-in session on this route.
  const { session } = useAuth();
  const userId = session?.user.id ?? null;
  const [order, setOrder] = useState<PaymentOrder | null>(null);
  const [creating, setCreating] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");

  const qrSrc = qrImageSrc(order?.qr_image ?? null);
  const paid = order?.status === "PAID";

  async function createInvoice() {
    if (!userId) return;
    setCreating(true);
    setError("");
    try {
      const response = await api<{ order: PaymentOrder }>("/payments/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId }),
      });
      setOrder(response.order);
    } catch (err) {
      setError(err instanceof Error ? err.message : "QPay invoice үүсгэж чадсангүй.");
    } finally {
      setCreating(false);
    }
  }

  const checkStatus = useCallback(async (orderId: string) => {
    setChecking(true);
    setError("");
    try {
      const response = await api<{ order: PaymentOrder; paid: boolean }>(
        `/payments/${orderId}/status`,
      );
      setOrder(response.order);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Төлбөрийн төлөв шалгаж чадсангүй.");
    } finally {
      setChecking(false);
    }
  }, []);

  // Poll while an invoice is waiting to be paid.
  useEffect(() => {
    if (!order || order.status === "PAID") return;
    const id = window.setInterval(() => {
      void checkStatus(order.id);
    }, 5000);
    return () => window.clearInterval(id);
  }, [checkStatus, order]);

  return (
    <main className="container payment">
      <p className="kicker">KublAI Essentials</p>
      <h1>Essentials эрх авах</h1>
      <p className="muted">
        QPay invoice үүсгээд QR-р төлнө. Төлбөр баталгаажмагц Essentials эрх
        идэвхжинэ.
      </p>

      <ul className="benefits">
        {BENEFITS.map((item) => (
          <li key={item}>✓ {item}</li>
        ))}
      </ul>

      {session && <TelegramLink />}

      {paid ? (
        <div className="panel success">
          <h2>Essentials эрх идэвхтэй 🎉</h2>
          <p className="muted">Төлбөр баталгаажсан.</p>
          <Link className="link" to="/">
            Үргэлжлүүлэх →
          </Link>
        </div>
      ) : order ? (
        <div className="panel">
          <p className="muted">Төлөх дүн</p>
          <p className="amount">{money(order.amount, order.currency)}</p>

          {qrSrc ? (
            <img className="qr" src={qrSrc} alt="QPay QR" />
          ) : (
            <p className="muted">QR код олдсонгүй — qr_text: {order.qr_text}</p>
          )}

          <button
            className="secondary"
            onClick={() => void checkStatus(order.id)}
            disabled={checking}
          >
            {checking ? "Шалгаж байна…" : "Төлөв шалгах"}
          </button>
        </div>
      ) : (
        <button className="primary" onClick={createInvoice} disabled={creating}>
          {creating ? "Үүсгэж байна…" : "QPay-р төлөх"}
        </button>
      )}

      {error && <p className="error">{error}</p>}
    </main>
  );
}
