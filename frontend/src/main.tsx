import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { ToastProvider } from './components/toast';
/* Bundled variable fonts: identical rendering on every OS — without
   these, Windows falls back to Segoe UI and (worse) Courier New. */
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import './style.css';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <ToastProvider>
        <App />
      </ToastProvider>
    </BrowserRouter>
  </React.StrictMode>
);

