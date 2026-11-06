/** Shared account-password rules (signup + change password). */

export const PASSWORD_POLICY =
  "Password must be at least 8 characters and include a letter, a number, and a symbol";

export function isStrongPassword(password: string): boolean {
  return (
    password.length >= 8 &&
    password.length <= 128 &&
    /[A-Za-z]/.test(password) &&
    /\d/.test(password) &&
    /[^A-Za-z0-9]/.test(password)
  );
}
