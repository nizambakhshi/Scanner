"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { dateTime } from "@/lib/format";

/**
 * Telegram: where the alerts go, and whether they can get there at all.
 *
 * The token and chat id are **write-only** fields. They are sent once, stored in
 * `ops_settings['telegram']`, and never rendered back — not as a value, not as
 * the last four characters. What the page shows instead is a twelve-hex
 * fingerprint of the `token\nchat_id` pair, which is the identical construction
 * the worker prints in its `telegram_credentials_applied` log line, so the two can
 * be compared without either one being revealed. Leaving a field blank keeps what
 * is stored; clearing them is a button that asks first.
 *
 * Send-test is the point of holding the credential here. Without it the only way
 * to learn that the chat id was one digit short is to wait for a breakout nobody
 * is watching for.
 */

type TelegramState = {
  source: "saved" | "env" | "none";
  configured: boolean;
  enabled: boolean | null;
  hasStoredPair: boolean;
  fingerprint: string | null;
  savedAt: string | null;
};

const SOURCE_WORD: Record<TelegramState["source"], string> = {
  saved: "the database row on this page",
  env: "the worker/app environment",
  none: "nowhere yet",
};

export function TelegramPanel({ state, lastChange }: { state: TelegramState; lastChange: string | null }) {
  const router = useRouter();
  const [token, setToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [enabled, setEnabled] = useState(state.enabled ?? false);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [note, setNote] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  async function save() {
    setBusy("save");
    setNote(null);
    try {
      const response = await fetch("/api/telegram", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ bot_token: token, chat_id: chatId, enabled }),
      });
      const payload = (await response.json()) as { note?: string; state?: TelegramState; error?: string };
      if (!response.ok || payload.error) {
        setNote({ tone: "error", text: payload.error ?? `HTTP ${response.status}` });
        return;
      }
      setToken("");
      setChatId("");
      setNote({ tone: "ok", text: payload.note ?? "Saved." });
      router.refresh();
    } catch (error) {
      setNote({ tone: "error", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(null);
    }
  }

  async function clearStored() {
    if (!window.confirm("Remove the stored bot token and chat id from the database? The worker falls back to its own environment.")) {
      return;
    }
    setBusy("save");
    setNote(null);
    try {
      const response = await fetch("/api/telegram", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ clear: "true", enabled }),
      });
      const payload = (await response.json()) as { note?: string; error?: string };
      if (!response.ok || payload.error) {
        setNote({ tone: "error", text: payload.error ?? `HTTP ${response.status}` });
        return;
      }
      setNote({ tone: "ok", text: payload.note ?? "Cleared." });
      router.refresh();
    } finally {
      setBusy(null);
    }
  }

  async function sendTest() {
    setBusy("test");
    setNote({ tone: "ok", text: "Asking Telegram…" });
    try {
      const response = await fetch("/api/telegram/test", { method: "POST" });
      const payload = (await response.json()) as { ok?: boolean; detail?: string; error?: string };
      const text = payload.detail ?? payload.error ?? `HTTP ${response.status}`;
      setNote({ tone: payload.ok === true ? "ok" : "error", text });
    } catch (error) {
      setNote({ tone: "error", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-3 px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <span
          className={`chip ${
            state.configured
              ? "border-positive/40 bg-positive/10 text-positive"
              : "border-line-2 bg-surface-2 text-muted"
          }`}
          title={
            state.configured
              ? "The worker has a token and a chat id it will push to"
              : "No push destination — alerts still land on the board"
          }
        >
          {state.configured ? "Telegram live" : "Not configured"}
        </span>
        <span className="text-[12px] text-muted">
          Credentials come from <span className="text-ink-2">{SOURCE_WORD[state.source]}</span>
          {state.fingerprint ? (
            <>
              {" "}· fingerprint <span className="num text-ink-2">{state.fingerprint}</span>
            </>
          ) : null}
          {state.savedAt ? (
            <>
              {" "}· saved <span className="num">{dateTime(state.savedAt)}</span>
            </>
          ) : null}
        </span>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <Field
          label="Bot token"
          hint={state.hasStoredPair ? "Stored. Leave blank to keep it." : "From @BotFather: digits, colon, secret."}
        >
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={token}
            onChange={(event) => setToken(event.target.value)}
            placeholder={state.hasStoredPair ? "••••••••••••••••••" : "123456789:AAH…"}
            className="num w-full rounded border border-line-2 bg-surface px-2 py-1.5 text-[13px] focus:border-accent focus:outline-none"
          />
        </Field>
        <Field label="Chat id" hint={state.hasStoredPair ? "Stored. Leave blank to keep it." : "A number; a group starts with a minus."}>
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={chatId}
            onChange={(event) => setChatId(event.target.value)}
            placeholder={state.hasStoredPair ? "••••••••" : "-1001234567890"}
            className="num w-full rounded border border-line-2 bg-surface px-2 py-1.5 text-[13px] focus:border-accent focus:outline-none"
          />
        </Field>
      </div>

      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => setEnabled(event.target.checked)}
          className="h-4 w-4 accent-[var(--color-positive)]"
        />
        <span className="text-[13px] font-medium text-ink-2">Push alerts to Telegram</span>
        <span className="text-[11px] text-muted">
          Off keeps every credential stored and stops every send — including this test.
        </span>
      </label>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy !== null}
          className="rounded border border-positive/40 bg-positive/10 px-3 py-1.5 text-[13px] font-semibold text-positive disabled:opacity-50"
        >
          {busy === "save" ? "Saving…" : "Save credentials"}
        </button>
        <button
          type="button"
          onClick={() => void sendTest()}
          disabled={busy !== null || !state.configured}
          title={state.configured ? "Sends one message to this chat" : "Set a token and chat id first"}
          className="rounded border border-line-2 bg-surface-2 px-3 py-1.5 text-[13px] font-semibold text-ink disabled:opacity-45"
        >
          {busy === "test" ? "Sending…" : "Send test"}
        </button>
        {state.hasStoredPair ? (
          <button
            type="button"
            onClick={() => void clearStored()}
            disabled={busy !== null}
            className="rounded border border-negative/40 px-3 py-1.5 text-[13px] text-negative hover:bg-negative/10 disabled:opacity-50"
          >
            Clear stored
          </button>
        ) : null}
      </div>

      {note ? (
        <p className={`text-[12px] ${note.tone === "ok" ? "text-positive" : "text-negative"}`}>{note.text}</p>
      ) : null}

      <p className="border-t border-line pt-2 text-[11px] leading-snug text-muted">
        Last change: {lastChange ?? "nothing saved from this dashboard yet."} The token is never shown back, written to a
        log line, or stored in an audit row — only its fingerprint is.
      </p>
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="eyebrow">{label}</span>
      {children}
      <span className="text-[10px] leading-snug text-muted">{hint}</span>
    </label>
  );
}
