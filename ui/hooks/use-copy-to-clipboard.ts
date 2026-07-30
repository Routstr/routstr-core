'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';

async function writeToClipboard(text: string): Promise<boolean> {
  // navigator.clipboard is undefined on plain-HTTP deployments (non-secure context)
  if (typeof navigator !== 'undefined' && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to the legacy path
    }
  }
  // execCommand reports that a copy ran, not that anything landed, so write the
  // text from the copy event and treat that firing as the proof of delivery.
  let delivered = false;
  const onCopy = (event: ClipboardEvent) => {
    event.clipboardData?.setData('text/plain', text);
    event.preventDefault();
    delivered = true;
  };
  try {
    document.addEventListener('copy', onCopy);
    return document.execCommand('copy') && delivered;
  } catch {
    return false;
  } finally {
    document.removeEventListener('copy', onCopy);
  }
}

/**
 * Clipboard copy with a success indicator that never lies: `copied` only
 * turns true when the text actually reached the clipboard.
 * `key` distinguishes multiple copy targets sharing one hook instance.
 */
export function useCopyToClipboard(resetMs = 2000) {
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const copy = useCallback(
    async (text: string, key = 'default'): Promise<boolean> => {
      const ok = await writeToClipboard(text);
      if (timer.current) clearTimeout(timer.current);
      if (ok) {
        setCopiedKey(key);
        timer.current = setTimeout(() => setCopiedKey(null), resetMs);
      } else {
        setCopiedKey(null);
        toast.error('Copy failed. Select the text and copy it manually.');
      }
      return ok;
    },
    [resetMs]
  );

  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  return { copied: copiedKey !== null, copiedKey, copy };
}
