import { useState, useCallback, useRef } from 'react';

const UNDO_TTL_MS = 5 * 60 * 1000; // 5 minutes

/**
 * Manages an undo stack for note edits (stores a single undo entry with TTL).
 *
 * @param {{ saveNote: Function, setNote: Function, setProf: Function }} deps
 */
export function useUndoStack({ saveNote, setNote, setProf } = {}) {
    const [undoToast, setUndoToast] = useState(null);
    const undoTimerRef = useRef(null);

    const pushUndo = useCallback((prevNote, prevProf, label, onUndoCleanup = null) => {
        if (undoTimerRef.current) clearTimeout(undoTimerRef.current);
        const expiresAt = Date.now() + UNDO_TTL_MS;
        setUndoToast({ note: prevNote, prof: prevProf, label, expiresAt, onUndoCleanup });
        undoTimerRef.current = setTimeout(() => setUndoToast(null), UNDO_TTL_MS);
    }, []);

    const handleUndoCommit = useCallback(async () => {
        if (!undoToast) return;
        if (undoTimerRef.current) clearTimeout(undoTimerRef.current);
        const { note: prevNote, prof: prevProf, onUndoCleanup } = undoToast;
        setUndoToast(null);
        setNote?.(prevNote);
        setProf?.(prevProf);
        await saveNote?.(prevNote, prevProf);
        if (typeof onUndoCleanup === 'function') {
            try { await onUndoCleanup(); } catch { }
        }
    }, [undoToast, saveNote, setNote, setProf]);

    const dismissUndo = useCallback(() => {
        if (undoTimerRef.current) clearTimeout(undoTimerRef.current);
        setUndoToast(null);
    }, []);

    return { undoToast, setUndoToast, undoTimerRef, pushUndo, handleUndoCommit, dismissUndo };
}
