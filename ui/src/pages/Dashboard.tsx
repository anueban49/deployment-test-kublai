import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import "../App.css";
import { useAuth } from "../hooks/useAuth";
import { supabase } from "../lib/supabaseClient";

const API_URL = import.meta.env.VITE_API_URL!;

async function authedFetch(path: string, init: RequestInit = {}) {
  const {
    data: { session },
  } = await supabase.auth.getSession();
  const headers: Record<string, string> = {
    ...((init.headers as Record<string, string>) ?? {}),
    Authorization: `Bearer ${session?.access_token ?? ""}`,
  };
  if (init.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  return fetch(`${API_URL}${path}`, { ...init, headers });
}

type Profile = {
  username: string | null;
  mail: string | null;
  phone_verified: boolean;
  membership_type: string;
  bot_invited: boolean;
  linked: boolean;
  settings: { watch_enabled: boolean };
};

type Group = {
  group_id: string;
  group_url: string | null;
  group_name: string | null;
};

type Saved = {
  post_id: string;
  url: string | null;
  message: string | null;
  author_name: string | null;
};

function TelegramIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z" />
    </svg>
  );
}

// "Invite the bot" deep-link button. The backend mints a single-use link token
// so pressing Start in the bot also binds this web account to the Telegram one.
function BotInviteButton({ botUsername }: { botUsername: string | null }) {
  const [busy, setBusy] = useState(false);
  const label = <>Invite {botUsername ? `@${botUsername}` : "KublaiBot"}</>;

  async function inviteWithLinking() {
    setBusy(true);
    try {
      const res = await authedFetch("/telegram/link-url", { method: "POST" });
      if (res.ok) {
        const body = await res.json();
        window.open(body.url, "_blank", "noreferrer");
      } else if (botUsername) {
        window.open(`https://t.me/${botUsername}`, "_blank", "noreferrer");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <button className="tg-btn" onClick={inviteWithLinking} disabled={busy}>
      <TelegramIcon />
      {label}
    </button>
  );
}

export default function Dashboard() {
  const { session } = useAuth();
  const [botUsername, setBotUsername] = useState<string | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [groups, setGroups] = useState<Group[]>([]);
  const [saved, setSaved] = useState<Saved[]>([]);

  const [username, setUsername] = useState("");
  const [groupInput, setGroupInput] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const loadAccount = useCallback(async () => {
    const res = await authedFetch("/account");
    if (!res.ok) return;
    const body = await res.json();
    setProfile(body.profile);
    setGroups(body.groups ?? []);
    setSaved(body.saved ?? []);
  }, []);

  useEffect(() => {
    fetch(`${API_URL}/telegram/bot-info`)
      .then((res) => (res.ok ? res.json() : null))
      .then((body) => setBotUsername(body?.username ?? null))
      .catch(() => setBotUsername(null));
  }, []);

  useEffect(() => {
    if (!session) return;
    loadAccount();
  }, [session, loadAccount]);

  // While the profile is incomplete (no username or unverified phone), poll so
  // the page reflects the user pressing Start / sharing their phone in the bot.
  const complete = !!profile && !!profile.username && profile.phone_verified;
  useEffect(() => {
    if (!session || complete) return;
    const timer = setInterval(loadAccount, 5000);
    return () => clearInterval(timer);
  }, [session, complete, loadAccount]);

  async function submitUsername() {
    setError("");
    setBusy(true);
    try {
      const res = await authedFetch("/account/username", {
        method: "POST",
        body: JSON.stringify({ username: username.trim() }),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) throw new Error(body?.detail ?? "Could not set username");
      setProfile(body.profile);
      setUsername("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not set username");
    } finally {
      setBusy(false);
    }
  }

  async function toggleWatch(next: boolean) {
    const res = await authedFetch("/account/settings", {
      method: "PATCH",
      body: JSON.stringify({ watch_enabled: next }),
    });
    if (res.ok) setProfile((await res.json()).profile);
  }

  async function addGroup() {
    setError("");
    setBusy(true);
    try {
      const res = await authedFetch("/account/groups", {
        method: "POST",
        body: JSON.stringify({ group: groupInput.trim() }),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) throw new Error(body?.detail ?? "Could not add group");
      setGroups(body.groups ?? []);
      setGroupInput("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not add group");
    } finally {
      setBusy(false);
    }
  }

  async function deleteGroup(groupId: string) {
    const res = await authedFetch(`/account/groups/${groupId}`, {
      method: "DELETE",
    });
    if (res.ok) setGroups((await res.json()).groups ?? []);
  }

  async function unsave(postId: string) {
    const res = await authedFetch(`/account/saved/${postId}`, {
      method: "DELETE",
    });
    if (res.ok) setSaved((await res.json()).saved ?? []);
  }

  const name = session?.user.user_metadata?.name ?? profile?.username ?? "";
  const essentials = profile?.membership_type === "essentials";

  return (
    <div className="dash">
      <header className="dash-header">
        <Link to="/dashboard" className="brand">
          KublAI
        </Link>
        <div className="invite">
          {profile && !profile.phone_verified && (
            <span className="muted">Haven't connected the bot yet?</span>
          )}
          <BotInviteButton botUsername={botUsername} />
        </div>
      </header>

      <main className="dash-body">
        {name && <p className="muted">Welcome, {name}.</p>}

        {/* Finish registration: username on the web, phone via the bot. */}
        {profile && !complete && (
          <section className="card">
            <h2>Finish setting up your account</h2>

            {!profile.username ? (
              <div className="field">
                <label>1. Choose a username</label>
                <div className="row">
                  <input
                    placeholder="username"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                  />
                  <button
                    className="primary"
                    onClick={submitUsername}
                    disabled={busy || !username.trim()}
                  >
                    Save
                  </button>
                </div>
              </div>
            ) : (
              <p className="done">✅ Username: @{profile.username}</p>
            )}

            <div className="field">
              <label>2. Verify your phone via the bot</label>
              {profile.phone_verified ? (
                <p className="done">✅ Phone verified</p>
              ) : (
                <>
                  <p className="muted">
                    Invite the bot, press Start, then tap “📱 Share my phone
                    number” inside the chat.
                  </p>
                  <BotInviteButton botUsername={botUsername} />
                </>
              )}
            </div>

            {error && <p className="error">{error}</p>}
          </section>
        )}

        {/* Settings */}
        {profile && (
          <section className="card">
            <h2>Settings</h2>
            <div className="settings-row">
              <div>
                <strong>Morning digest</strong>
                <p className="muted">
                  Fresh buyer posts every morning at 07:00 (Ulaanbaatar).
                </p>
              </div>
              {essentials ? (
                <label className="switch">
                  <input
                    type="checkbox"
                    checked={profile.settings.watch_enabled}
                    onChange={(e) => toggleWatch(e.target.checked)}
                  />
                  <span>{profile.settings.watch_enabled ? "On" : "Off"}</span>
                </label>
              ) : (
                <span className="muted">Essentials only</span>
              )}
            </div>
          </section>
        )}

        {/* Groups */}
        {profile && (
          <section className="card">
            <h2>Your Facebook groups</h2>
            <div className="row">
              <input
                placeholder="facebook.com/groups/… or numeric id"
                value={groupInput}
                onChange={(e) => setGroupInput(e.target.value)}
              />
              <button
                className="primary"
                onClick={addGroup}
                disabled={busy || !groupInput.trim()}
              >
                Add
              </button>
            </div>
            {groups.length === 0 ? (
              <p className="muted">No groups yet.</p>
            ) : (
              <ul className="item-list">
                {groups.map((g) => (
                  <li key={g.group_id}>
                    {g.group_url ? (
                      <a href={g.group_url} target="_blank" rel="noreferrer">
                        {g.group_name ?? g.group_id}
                      </a>
                    ) : (
                      <span>{g.group_name ?? g.group_id}</span>
                    )}
                    <button
                      className="secondary danger"
                      onClick={() => deleteGroup(g.group_id)}
                    >
                      Remove
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}

        {/* Saved posts (Essentials) */}
        {essentials && (
          <section className="card">
            <h2>Saved posts</h2>
            {saved.length === 0 ? (
              <p className="muted">
                No saved posts. Save them from the bot with /save.
              </p>
            ) : (
              <ul className="item-list">
                {saved.map((s) => (
                  <li key={s.post_id}>
                    <div className="saved-text">
                      <strong>{s.author_name ?? "Unknown"}</strong>
                      {s.message && <span> — {s.message.slice(0, 120)}</span>}
                      {s.url && (
                        <>
                          {" "}
                          <a href={s.url} target="_blank" rel="noreferrer">
                            open
                          </a>
                        </>
                      )}
                    </div>
                    <button
                      className="secondary danger"
                      onClick={() => unsave(s.post_id)}
                    >
                      Unsave
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}

        {/* Upgrade CTA hidden while the payment flow is disabled (WIP). */}
      </main>
    </div>
  );
}
