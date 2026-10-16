/**
 * The role vocabulary, one place.
 *
 * `app_users.role` has spelled these three values out since 0001. This module is
 * deliberately dependency-free: the nav (a client component) and the page/API
 * guards (server) must agree on who counts as an admin, and they can only do
 * that if the rule lives where both may import it.
 */
export const ADMIN_ROLES = ["SUPER_ADMIN", "ADMIN"] as const;

export function isAdminRole(role: string): boolean {
  return (ADMIN_ROLES as readonly string[]).includes(role);
}
