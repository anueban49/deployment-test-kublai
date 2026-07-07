from functools import lru_cache

from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_SECRET_KEY, SUPABASE_KEY


@lru_cache(maxsize=1)
def get_client() -> Client:
    # Prefer the service_role/secret key: this is a trusted backend and it must
    # bypass row-level security to read/write the users table. Fall back to the
    # publishable key only if no secret key is configured.
    key = SUPABASE_SECRET_KEY or SUPABASE_KEY
    if not SUPABASE_URL or not key:
        raise RuntimeError(
            "SUPABASE_URL and SECRET_KEY (or SUPABASE_KEY) must be set in the "
            "environment/.env"
        )
    return create_client(SUPABASE_URL, key)
