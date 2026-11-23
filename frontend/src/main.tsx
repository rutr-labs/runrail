import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { ToastProvider } from './components/toast';
/* Bundled variable fonts: identical rendering on every OS — without
   these, Windows falls back to Segoe UI and (worse) Courier New. */
/* The opsz build carries the optical-size axis as well as weight, so large
   headings get Inter's display cut and small labels its text cut. */
import '@fontsource-variable/inter/opsz.css';
import '@fontsource-variable/jetbrains-mono';
/* The display face: what the system says to you — verdicts, titles, the big
   numbers. Its optical-size axis draws tighter, sharper forms at these sizes. */
import '@fontsource-variable/bricolage-grotesque/opsz.css';
import './style.css';
/* How RunRail looks and is laid out — see the note at its top. */
import './design.css';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <ToastProvider>
        <App />
      </ToastProvider>
    </BrowserRouter>
  </React.StrictMode>
);

