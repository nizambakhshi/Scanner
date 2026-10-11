/**
 * Credential redaction for anything that reaches a screen or a log (§19).
 *
 * A database driver is entitled to put the connection string it was given inside
 * its own error message, and that message is exactly what a "show the operator why
 * it failed" panel wants to print. So the text is scrubbed before it is truncated:
 * a password inside a DSN, and a named secret in a `key=value` fragment, are both
 * replaced with a bullet run that keeps the shape readable.
 *
 * This is a defence in depth, not a licence to log secrets: the sources still never
 * pass a DSN anywhere near an error path on purpose.
 */
const DSN_CREDENTIAL = /(\b[a-z][a-z0-9+.-]*:\/\/[^:/\s@]*:)([^@\s/]+)(@)/gi;
const NAMED_SECRET = /\b(password|passwd|secret|api[_-]?key|access[_-]?key|token|totp|pin)\b(\s*[=:]\s*)(["']?)([^\s"',;]{4,})(["']?)/gi;

export function redact(value: string): string {
  return value.replace(DSN_CREDENTIAL, "$1•••$3").replace(NAMED_SECRET, "$1$2$3•••$5");
}

/** The first line, redacted, and never long enough to swallow a screen. */
export function safeMessage(error: unknown, max = 200): string {
  const message = error instanceof Error ? error.message : String(error);
  const firstLine = (message.split("\n")[0] ?? message).trim();
  const scrubbed = redact(firstLine);
  return scrubbed.length > max ? `${scrubbed.slice(0, max - 1)}…` : scrubbed;
}
