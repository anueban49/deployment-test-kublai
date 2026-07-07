import "../App.css";
import { supabase } from "../lib/supabaseClient";

export default function Landing() {
  const handleGoogleLogin = async () => {
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: `${window.location.origin}/auth/callback`,
      },
    });
    if (error) console.error(error.message);
  };

  return (
    <main className="container">
      <div className="panel">
        <h1>Login/Register to KublAI</h1>
        <div className="plain-auth">
          <button className="google-btn" onClick={handleGoogleLogin}>
            Google sign In
          </button>
        </div>
      </div>
    </main>
  );
}
