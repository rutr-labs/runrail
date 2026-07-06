import { createContext, useCallback, useContext, useRef, useState, ReactNode } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { CheckCircle2, XCircle, Info, X } from 'lucide-react';

type ToastType = 'success' | 'error' | 'info';
interface Toast { id: number; type: ToastType; message: string }

const Ctx = createContext<{ toast: (msg: string, type?: ToastType) => void }>({ toast: () => {} });

export function useToast() { return useContext(Ctx); }

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(0);

  const toast = useCallback((message: string, type: ToastType = 'success') => {
    const id = ++nextId.current;
    setToasts(prev => [...prev, { id, type, message }]);
    setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 3800);
  }, []);

  const dismiss = (id: number) => setToasts(prev => prev.filter(t => t.id !== id));

  return (
    <Ctx.Provider value={{ toast }}>
      {children}
      <div className="toaster" aria-live="polite">
        <AnimatePresence>
          {toasts.map(t => (
            <motion.div
              key={t.id}
              className={`toast toast-${t.type}`}
              initial={{ opacity: 0, x: 60, scale: 0.92 }}
              animate={{ opacity: 1, x: 0, scale: 1, transition: { duration: 0.22, ease: [0.16, 1, 0.3, 1] } }}
              exit={{ opacity: 0, x: 60, scale: 0.9, transition: { duration: 0.18 } }}
              layout
            >
              {t.type === 'success' && <CheckCircle2 size={15} className="toast-icon" />}
              {t.type === 'error'   && <XCircle      size={15} className="toast-icon" />}
              {t.type === 'info'    && <Info          size={15} className="toast-icon" />}
              <span className="toast-msg">{t.message}</span>
              <button className="toast-dismiss" onClick={() => dismiss(t.id)} aria-label="Dismiss">
                <X size={13} />
              </button>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </Ctx.Provider>
  );
}
