import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.tsx'
import './index.css'
import './App.css'
import { Toaster } from 'sonner'
import { MotionConfig } from 'framer-motion'
import { BrowserRouter } from 'react-router'
import { RouteTransitionCoordinator } from './routing/RouteTransitionCoordinator'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter basename={import.meta.env.BASE_URL.replace(/\/+$/, '') || '/'}>
      <RouteTransitionCoordinator>
        <MotionConfig reducedMotion="user">
          <App />
        </MotionConfig>
      </RouteTransitionCoordinator>
    </BrowserRouter>
    <Toaster
      position="top-right"
      theme="dark"
      richColors
      toastOptions={{ className: 'omnipulse-toast' }}
    />
  </React.StrictMode>,
)
