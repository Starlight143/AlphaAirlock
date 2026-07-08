'use client';

import { useEffect, useRef, useState } from 'react';
import { useIsMutating, useMutation, useQueryClient } from '@tanstack/react-query';
import { Send, Loader2, Paperclip, X } from 'lucide-react';
import { api, cryptoUuid, type ChatSession } from '@/lib/api';
import { queryKeys } from '@/lib/query';

type Props = {
  sessionId: number | null;
  supportsVision: boolean;
  // P-MODEL-SEL — optional per-message model override from the Alpha Lab picker.
  // Omitted/null => the server uses its configured default.
  model?: string | null;
  onSessionCreated: (s: ChatSession) => void;
};

type Attachment = {
  serverPath: string;     // returned by /api/chat/upload
  filename: string;       // uuid32.ext
  size: number;
  mime: string;
  previewUrl: string;     // URL.createObjectURL(file)
};

const MAX_BYTES = 8 * 1024 * 1024;
const ACCEPTED_MIME = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];

export default function ChatComposer({
  sessionId,
  supportsVision,
  model,
  onSessionCreated,
}: Props) {
  const qc = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [text, setText] = useState('');
  const [attachment, setAttachment] = useState<Attachment | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);

  // Cleanup the object URL when the attachment changes or component unmounts.
  useEffect(() => {
    return () => {
      if (attachment?.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
    };
  }, [attachment?.previewUrl]);

  const upload = useMutation({
    mutationFn: (file: File) => api.chatUpload(file),
  });

  // P34-IDEMP: the key is minted once per submit and held in a ref so that
  // TanStack Query retries (up to 3) all carry the SAME idempotency key,
  // allowing the server guard to deduplicate in-flight 5xx retries. A new
  // key is generated in onSuccess so the next submit starts fresh.
  const sendKeyRef = useRef<string>(cryptoUuid());

  // F6-2: track the session that was active when each send was dispatched so
  // that onSuccess does not clear a *different* session's compose box when the
  // user switches sessions while a send is in-flight.  The ref always holds the
  // latest rendered sessionId; capturedSendSessionRef is set to that value
  // immediately before calling send.mutate() and read inside onSuccess.
  const sessionIdRef = useRef<number | null>(sessionId);
  useEffect(() => { sessionIdRef.current = sessionId; }, [sessionId]);
  const capturedSendSessionRef = useRef<number | null>(sessionId);

  const send = useMutation({
    // P29-F9: per-session key so alpha-lab can pause detail-poll during send.
    mutationKey: ['chat-send', sessionId ?? -1],
    mutationFn: () => {
      return api.chatSend(
        {
          session_id: sessionId ?? null,
          user_text: text,
          image_paths: attachment ? [attachment.serverPath] : null,
          model: model ?? null,
        },
        { idempotencyKey: sendKeyRef.current },
      );
    },
    onSuccess: (data) => {
      // Mint a fresh key for the next submit now that this one succeeded.
      sendKeyRef.current = cryptoUuid();
      qc.invalidateQueries({ queryKey: queryKeys.chatSessions });
      // Snapshot ref values once to get stable, narrowable locals.
      const sentSessionId = capturedSendSessionRef.current;
      if (sentSessionId == null) {
        onSessionCreated(data.session);
      } else {
        qc.invalidateQueries({
          queryKey: queryKeys.chatSessionDetail(sentSessionId),
        });
      }
      // F6-2 guard: only wipe the compose box if the user is still looking at
      // the same session that originally triggered this send. If they switched
      // sessions while the request was in-flight, leave the new session's text
      // and attachment intact.
      if (sessionIdRef.current === sentSessionId) {
        setText('');
        setAttachment(null);
      }
    },
  });

  // F6-4: useIsMutating reads directly from the MutationCache (not the
  // MutationObserver), so it stays >0 for as long as the underlying HTTP
  // request is in-flight — even after the observer is reset by the mutationKey
  // change that happens synchronously when sessionId changes.  This prevents
  // the Send button from being re-enabled prematurely on session switch while
  // the previous request is still running.
  //
  // The explicit send.reset() useEffect that was here is removed: TanStack
  // Query v5 MutationObserver.setOptions() already calls this.reset()
  // synchronously whenever mutationKey's hashKey changes (mutationObserver.js
  // lines 32-33), so the useEffect reset was redundant — it fired after the
  // implicit reset, not before.
  const anyChatSendInFlight = useIsMutating({ mutationKey: ['chat-send'], exact: false }) > 0;

  const canSend = text.trim().length >= 1 && !send.isPending && !anyChatSendInFlight && !upload.isPending;

  async function handleFileChosen(file: File) {
    setUploadError(null);
    if (!ACCEPTED_MIME.includes(file.type)) {
      setUploadError(`Unsupported type ${file.type || 'unknown'} (PNG / JPG / WEBP / GIF only)`);
      return;
    }
    if (file.size > MAX_BYTES) {
      setUploadError(`File too large (${(file.size / 1024 / 1024).toFixed(1)}MB) — max 8MB`);
      return;
    }
    try {
      const previewUrl = URL.createObjectURL(file);
      let res;
      try {
        res = await upload.mutateAsync(file);
      } catch (e) {
        // Revoke the object URL immediately so it is not leaked on upload failure.
        URL.revokeObjectURL(previewUrl);
        setUploadError(e instanceof Error ? e.message : String(e));
        return;
      }
      // Replace any earlier preview to avoid leaking.
      if (attachment?.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
      setAttachment({
        serverPath: res.path,
        filename: res.filename,
        size: res.size,
        mime: res.mime,
        previewUrl,
      });
    } catch (e) {
      setUploadError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="border-t border-slate-800 bg-slate-950/60 p-3">
      {attachment && (
        <div className="mb-2 flex items-center gap-3 rounded border border-cyan-700/40 bg-cyan-500/10 p-2">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={attachment.previewUrl}
            alt="attachment preview"
            className="h-12 w-12 rounded border border-cyan-700/40 object-cover"
          />
          <div className="flex-1 text-[10px] text-cyan-200">
            <div className="font-mono">{attachment.filename}</div>
            <div className="text-cyan-400/80">
              {attachment.mime} · {(attachment.size / 1024).toFixed(1)} KB
            </div>
            <div className="font-mono text-[9px] text-slate-500">
              {attachment.serverPath}
            </div>
          </div>
          <button
            onClick={() => {
              URL.revokeObjectURL(attachment.previewUrl);
              setAttachment(null);
            }}
            className="rounded p-1 text-cyan-400 hover:bg-cyan-500/20 hover:text-cyan-200"
            title="Remove attachment"
          >
            <X className="h-3 w-3" />
          </button>
        </div>
      )}
      {uploadError && (
        <div className="mb-2 rounded border border-rose-700 bg-rose-500/10 px-2 py-1 text-[10px] text-rose-300">
          Upload: {uploadError}
        </div>
      )}
      {send.isError && (
        <div className="mb-2 rounded border border-rose-700 bg-rose-500/10 px-2 py-1 text-[10px] text-rose-300">
          {(send.error as Error).message}
        </div>
      )}
      <div className="flex items-end gap-2">
        {supportsVision && (
          <>
            <input
              ref={fileInputRef}
              type="file"
              accept={ACCEPTED_MIME.join(',')}
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void handleFileChosen(f);
                // Reset so re-picking the same file fires onChange again.
                e.target.value = '';
              }}
            />
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={upload.isPending}
              // P11-F4-13 — spell out the accepted formats and the 8MB cap in
              // the tooltip so users don't have to discover the upload limit
              // by hitting the error toast. Also adds an aria-label so screen
              // readers get a meaningful description of the icon-only button.
              title="Attach chart image — PNG / JPG / WEBP / GIF, ≤8MB. Vision-enabled models will analyze the chart."
              aria-label="Attach chart image for vision analysis"
              className="rounded-md border border-slate-700 px-2 py-2 text-slate-400 hover:bg-slate-800 hover:text-cyan-200 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {upload.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Paperclip className="h-4 w-4" />
              )}
            </button>
          </>
        )}
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && canSend) {
              e.preventDefault();
              capturedSendSessionRef.current = sessionIdRef.current;
              send.mutate();
            }
          }}
          placeholder="Ask about alpha ideas, brainstorm factors, or sketch trading strategies..."
          rows={2}
          className="flex-1 resize-none rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-100 outline-none focus:border-cyan-600"
          disabled={send.isPending}
        />
        <button
          onClick={() => { capturedSendSessionRef.current = sessionIdRef.current; send.mutate(); }}
          disabled={!canSend}
          className="flex items-center gap-1.5 rounded-md border border-cyan-700 bg-cyan-500/15 px-3 py-2 text-[11px] font-bold tracking-wide text-cyan-200 hover:bg-cyan-500/25 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {send.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Send className="h-4 w-4" />
          )}
          {send.isPending ? 'Sending' : 'Send'}
        </button>
      </div>
      <div className="mt-1 text-[9px] text-slate-600">
        Enter to send · Shift+Enter for newline
        {supportsVision ? ' · paperclip = attach chart (PNG/JPG/WEBP/GIF, ≤8MB)' : ' · vision off (model lacks support)'}
      </div>
    </div>
  );
}
