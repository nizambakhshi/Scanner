"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { dateTime } from "@/lib/format";

/**
 * Telegram: where the alerts go, and whether they can get there at all.
 *
 * The token is a **write-only** field. It is sent once, stored in
 * `ops_settings['telegram']`, and never rendered back — not as a value, not as
 * the last four characters. What the page shows instead is a twelve-hex
 * fingerprint of the token and the channel list, which is the identical
 * construction the worker prints in its `telegram_credentials_applied` log line,
 * so the two can be compared without the token being revealed.
 *
 * Channels are a visible, editable list — you cannot delete a row you cannot
 * see. The list is posted **complete** on every save; the token alone keeps the
 * "blank means keep" rule. Leaving the token blank keeps what is stored;
 * clearing everything is a button that asks first.
 *
 * Send-test is the point of holding the credential here. Without it the only way
 * to learn that a chat id was one digit short is to wait for a breakout nobody
 * is watching for — and with several channels, per-channel results are what
 * tell you *which* one is wrong.
 */

type TelegramChat = { chatId: string; label: string | null };

type TelegramState = {
  source: "saved" | "env" | "none";
  configured: boolean;
  enabled: boolean | null;
  hasStoredPair: boolean;
  chats: TelegramChat[];
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
  // Editable copy of the stored list; starts from what the page shows, so a
  // save posts the whole list back even when only the switch moved.
  const [chats, setChats] = useState<TelegramChat[]>(
    state.chats.length > 0 ? state.chats.map((chat) => ({ ...chat })) : [],
  );
  const [enabled, setEnabled] = useState(state.enabled ?? false);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [note, setNote] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  function setChat(index: number, patch: Partial<TelegramChat>) {
    setChats((list) => list.map((chat, i) => (i === index ? { ...chat, ...patch } : chat)));
  }

  async function save() {
    setBusy("save");
    setNote(null);
    try {
      const response = await fetch("/api/telegram", {
        method: "POST",
        headers: { "content-type": "application/json" },
        // The list travels as a JSON string because the API's body reader
        // stringifies every field; the route parses it back.
        body: JSON.stringify({ bot_token: token, chats: JSON.stringify(chats), enabled }),
      });
      const payload = (await response.json()) as { note?: string; state?: TelegramState; error?: string };
      if (!response.ok || payload.error) {
        setNote({ tone: "error", text: payload.error ?? `HTTP ${response.status}` });
        return;
      }
      setToken("");
      setNote({ tone: "ok", text: payload.note ?? "Saved." });
      router.refresh();
    } catch (error) {
      setNote({ tone: "error", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(null);
    }
  }

  async function clearStored() {
    if (!window.confirm("Remove the stored bot token and every channel from the database? The worker falls back to its own environment.")) {
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
      setChats([]);
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
      const payload = (await response.json()) as {
        ok?: boolean;
        detail?: string;
        error?: string;
        results?: { chat_id: string; label: string | null; ok: boolean; detail: string }[];
      };
      const text = payload.detail ?? payload.error ?? `HTTP ${response.status}`;
      const perChannel = (payload.results ?? [])
        .filter((result) => !result.ok)
        .map((result) => `${result.label ?? result.chat_id}: ${result.detail}`);
      setNote({
        tone: payload.ok === true ? "ok" : "error",
        text: perChannel.length > 0 && payload.ok !== true ? `${text} — ${perChannel.join(" · ")}` : text,
      });
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
              ? "The worker has a token and channels it will push to"
              : "No push destination — alerts still land on the board"
          }
        >
          {state.configured ? `Telegram live${state.chats.length > 1 ? ` · ${state.chats.length} channels` : ""}` : "Not configured"}
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
              {/* "saved", not "last saved by you": on a database the page has never
                  been used on, this is the timestamp migration 0010 inserted. */}
              {" "}· row written <span className="num">{dateTime(state.savedAt)}</span>
            </>
          ) : null}
        </span>
      </div>

      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
        <label className="flex flex-col gap-1">
          <span className="eyebrow">Bot token</span>
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={token}
            onChange={(event) => setToken(event.target.value)}
            placeholder={state.hasStoredPair ? "••••••••••••••••••" : "123456789:AAH…"}
            className="num w-full rounded border border-line-2 bg-surface px-2 py-1.5 text-[13px] focus:border-accent focus:outline-none"
          />
          <span className="text-[10px] leading-snug text-muted">
            {state.hasStoredPair ? "Stored. Leave blank to keep it." : "From @BotFather: digits, colon, secret."}
          </span>
        </label>
      </div>

      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="eyebrow">Channels</span>
          <button
            type="button"
            onClick={() => setChats((list) => [...list, { chatId: "", label: "" }])}
            className="rounded border border-line-2 bg-surface-2 px-2 py-1 text-[12px] font-semibold text-ink hover:border-accent"
          >
            Add channel
          </button>
        </div>
        {chats.length === 0 ? (
          <p className="text-[12px] text-muted">
            No channels listed. {state.hasStoredPair ? "Add one to replace the stored set." : "Save a bot token and at least one chat id."}
          </p>
        ) : (
          chats.map((chat, index) => (
            <div key={index} className="flex flex-wrap items-center gap-2">
              <input
                type="text"
                inputMode="numeric"
                autoComplete="off"
                spellCheck={false}
                value={chat.chatId}
                onChange={(event) => setChat(index, { chatId: event.target.value })}
                placeholder="-1001234567890"
                aria-label={`Chat id for channel ${index + 1}`}
                className="num w-44 rounded border border-line-2 bg-surface px-2 py-1.5 text-[13px] focus:border-accent focus:outline-none"
              />
              <input
                type="text"
                autoComplete="off"
                value={chat.label ?? ""}
                onChange={(event) => setChat(index, { label: event.target.value })}
                placeholder="Label (optional)"
                aria-label={`Label for channel ${index + 1}`}
                className="w-40 rounded border border-line-2 bg-surface px-2 py-1.5 text-[13px] focus:border-accent focus:outline-none"
              />
              <button
                type="button"
                onClick={() => setChats((list) => list.filter((_, i) => i !== index))}
                aria-label={`Delete channel ${index + 1}`}
                className="rounded border border-negative/40 px-2 py-1 text-[12px] text-negative hover:bg-negative/10"
              >
                Delete
              </button>
            </div>
          ))
        )}
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
          title={state.configured ? "Sends one message to every channel" : "Set a token and a chat id first"}
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
        log line, or stored in an audit row — only the channel list and the fingerprint of the whole set are.
      </p>
    </div>
  );
}
