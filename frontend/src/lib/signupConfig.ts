import { useEffect, useState } from "react";
import { fetchAuthConfig } from "./api";

let cached: Promise<boolean> | null = null;

/** Whether open sign-up is enabled (organizations are invite-only otherwise).
 * Null while loading; fails closed to false. */
export function usePublicSignup(): boolean | null {
  const [allowed, setAllowed] = useState<boolean | null>(null);
  useEffect(() => {
    cached ??= fetchAuthConfig()
      .then((c) => c.allow_public_signup)
      .catch(() => {
        cached = null;
        return false;
      });
    let live = true;
    void cached.then((v) => {
      if (live) setAllowed(v);
    });
    return () => {
      live = false;
    };
  }, []);
  return allowed;
}
