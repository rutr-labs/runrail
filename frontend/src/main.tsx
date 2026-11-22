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
import './style.css';
/* The design refresh: a layer over style.css — see the note at its top. */
import './refresh.css';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <ToastProvider>
        <App />
      </ToastProvider>
    </BrowserRouter>
  </React.StrictMode>
);

