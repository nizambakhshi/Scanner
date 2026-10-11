/**
 * Imported for its side effects, first, before anything reads `lib/env.ts`.
 *
 * `lib/env.ts` snapshots `process.env` the moment the module is first evaluated,
 * so a test that imports it statically has to have its variables in place before
 * that evaluation. The values here are throwaway local secrets used only by the
 * test process — they authenticate nothing and are safe to commit, which is why
 * they live in source rather than in a `.env` file this project never reads (§19).
 */
process.env.AUTH_SESSION_SECRET = "0123456789abcdef0123456789abcdef";
process.env.AUTH_SESSION_HOURS = "12";
process.env.AUTH_ADMIN_EMAIL = "operator@example.com";
process.env.DATA_SOURCE = "replay";
