/* eslint-disable @typescript-eslint/no-explicit-any */
import React, { useState, useEffect, useRef } from 'react';
import { useLocation, useParams } from 'react-router';
import {
  LayoutDashboard,
  Search,
  Layers,
  Package,
  Menu,
  ChevronLeft,
  ChevronRight,
  Radar,
  Images,
  Bell,
} from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import { formatTimeGlobal, sortBrands, type BrandItem } from './shared/lib/index';
import { SettingsPage } from './features/settings/SettingsPage';
import { CategoryPage } from './features/category/CategoryPage';
import { BannersPage } from './features/banners/BannersPage';
import { MonitoringPage } from './features/monitor/MonitoringPage';
import { SidebarItem } from './shared/layout/SidebarItem.tsx';
import { ApiClient } from './api/client';
import { notificationRoutePath, useNotificationStore } from './stores/notificationStore';
import './App.css';
import './styles/primitives.css';
import { AppRoutes } from './routing/AppRoutes';
import { NotFoundRoute } from './routing/NotFoundRoute';
import { useRouteTransitionCoordinator } from './routing/RouteTransitionCoordinator';
import { ROUTES, deepSearchPath, resolveRouteMetadata } from './routing/paths';
import { SearchPage } from './features/search/SearchPage';
import { CrossMarketplacePage } from './features/cross/CrossMarketplacePage';

// --- Componente de Status de Sistema ---
const SystemNotifications = () => {
  const transition = useRouteTransitionCoordinator();
  const [open, setOpen] = useState(false);
  const [bellPulsing, setBellPulsing] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const notifications = useNotificationStore(state => state.notifications);
  const markRead = useNotificationStore(state => state.markRead);
  const markAllRead = useNotificationStore(state => state.markAllRead);
  const syncBackend = useNotificationStore(state => state.syncBackend);
  const lastNewUnreadAt = useNotificationStore(state => state.lastNewUnreadAt);

  // Pulse animation on bell when new unread notifications arrive
  useEffect(() => {
    if (lastNewUnreadAt === 0) return;
    const pulseTimer = setTimeout(() => setBellPulsing(true), 0);
    const stopTimer = setTimeout(() => setBellPulsing(false), 5000);
    return () => {
      clearTimeout(pulseTimer);
      clearTimeout(stopTimer);
    };
  }, [lastNewUnreadAt]);

  useEffect(() => {
    const poll = async () => {
      try {
        const data = await ApiClient.getSystemStatus();
        if (data && data.notifications) {
          syncBackend(data.notifications);
        }
      } catch {
        // ignore
      }
    };
    poll();
    const intervalId = setInterval(poll, 10000);
    return () => clearInterval(intervalId);
  }, [syncBackend]);

  const unreadCount = notifications.filter(n => !n.read).length;

  const closePanel = () => {
    setOpen(false);
    requestAnimationFrame(() => triggerRef.current?.focus());
  };

  useEffect(() => {
    if (!open) return;
    panelRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closePanel();
        return;
      }
      if (event.key === 'Tab' && panelRef.current) {
        const focusable = Array.from(
          panelRef.current.querySelectorAll<HTMLElement>('a[href], button:not([disabled])'),
        ).filter(element => element.getClientRects().length > 0);
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        const activeElement = document.activeElement;
        if (event.shiftKey && (activeElement === first || !panelRef.current.contains(activeElement))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (activeElement === last || !panelRef.current.contains(activeElement))) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [open]);

  const handleRead = (notif: any) => {
    if (!notif.read) {
      markRead(notif.id);
      // Avisa o backend para marcar como lida, evitando que ela volte como não lida
      ApiClient.request(`/system/notifications/${notif.id}/read`, { method: 'POST' }).catch(() => { });
    }
    closePanel();

    if (notif.routeTarget) {
      const targetKind = notif.routeTarget.kind === 'search' || notif.routeTarget.kind === 'cross'
        ? notif.routeTarget.kind
        : undefined;
      transition.navigateWithSnapshot(notificationRoutePath(notif.routeTarget), { targetKind });
      requestAnimationFrame(() => document.getElementById('main-content')?.focus());
    }
  };

  const handleMarkAllRead = () => {
    markAllRead();
    ApiClient.request('/system/notifications/read-all', { method: 'POST' }).catch(() => { });
  };

  const formatTime = (isoString?: string) => formatTimeGlobal(isoString);

  return (
    <div className="notification-center">
      <button
        ref={triggerRef}
        type="button"
        className={`notification-bell ${unreadCount > 0 ? 'has-unread' : ''} ${bellPulsing ? 'bell-pulse' : ''}`}
        onClick={() => setOpen(!open)}
        aria-label={unreadCount > 0 ? `Notificações, ${unreadCount} não lidas` : 'Notificações'}
        aria-expanded={open}
        aria-controls="system-notifications-panel"
      >
        <Bell size={18} aria-hidden="true" />
        {unreadCount > 0 && (
          <span className="notification-badge" aria-hidden="true">
            {unreadCount}
          </span>
        )}
      </button>

      {open && (
        <div
          ref={panelRef}
          id="system-notifications-panel"
          className="notification-panel"
          role="region"
          aria-label="Central de notificações"
          tabIndex={-1}
        >
          <div className="notification-panel-header">
            <div>
              <strong>Notificações</strong>
              <span>{unreadCount > 0 ? `${unreadCount} não lidas` : 'Tudo em dia'}</span>
            </div>
            {unreadCount > 0 && (
              <button
                type="button"
                className="mark-all-read-btn"
                onClick={handleMarkAllRead}
              >
                Marcar tudo como lido
              </button>
            )}
          </div>
          <div className="notification-list">
            {notifications.length === 0 ? (
              <div className="notification-empty">Nenhuma notificação</div>
            ) : (
              notifications.map((n: any) => (
                <button
                  type="button"
                  key={n.id}
                  onClick={() => handleRead(n)}
                  className={`notification-item ${n.read ? '' : 'unread'}`}
                >
                  <span className="notification-item-title">{n.title}</span>
                  <span className="notification-item-message">{n.message}</span>
                  <time className="notification-item-time">{formatTime(n.createdAt || n.timestamp)}</time>
                </button>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
};

// --- Main App ---

const SearchRoute = ({ brands }: { brands: BrandItem[] }) => {
  const { jobId } = useParams<{ jobId?: string }>();
  const transition = useRouteTransitionCoordinator();
  return (
    <SearchPage
      brands={brands}
      jobId={jobId}
      onReopen={(nextJobId: string) =>
        transition.navigate(deepSearchPath('search', nextJobId), { targetKind: 'search' })
      }
    />
  );
};

const CrossRoute = () => {
  const { jobId } = useParams<{ jobId?: string }>();
  const transition = useRouteTransitionCoordinator();
  return (
    <CrossMarketplacePage
      jobId={jobId}
      onReopen={(nextJobId: string) =>
        transition.navigate(deepSearchPath('cross', nextJobId), { targetKind: 'cross' })
      }
    />
  );
};

function App() {
  const [brands, setBrands] = useState<BrandItem[]>([]);
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [isMobileSidebarOpen, setIsMobileSidebarOpen] = useState(false);
  const navigationTriggerRef = useRef<HTMLButtonElement>(null);
  const navigationRef = useRef<HTMLElement>(null);
  const mainContentRef = useRef<HTMLElement>(null);
  const location = useLocation();
  const transition = useRouteTransitionCoordinator();
  const route = resolveRouteMetadata(location.pathname);

  const closeNavigation = (restoreFocus = true) => {
    setIsMobileSidebarOpen(false);
    if (restoreFocus) requestAnimationFrame(() => navigationTriggerRef.current?.focus());
  };

  const openNavigation = () => {
    setIsMobileSidebarOpen(true);
    requestAnimationFrame(() => navigationRef.current?.querySelector<HTMLElement>('.sidebar-item')?.focus());
  };

  useEffect(() => {
    if (!isMobileSidebarOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeNavigation();
        return;
      }
      if (event.key === 'Tab' && navigationRef.current) {
        const focusable = Array.from(
          navigationRef.current.querySelectorAll<HTMLElement>('a[href], button:not([disabled])'),
        ).filter(element => element.getClientRects().length > 0);
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        const activeElement = document.activeElement;
        if (event.shiftKey && (activeElement === first || !navigationRef.current.contains(activeElement))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (activeElement === last || !navigationRef.current.contains(activeElement))) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [isMobileSidebarOpen]);

  const focusRoute = () => requestAnimationFrame(() => mainContentRef.current?.focus());
  const handleRouteNavigation = (
    event: React.MouseEvent<HTMLAnchorElement>,
    targetKind?: 'search' | 'cross',
    resetBase = false,
  ) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    transition.prepareNavigation(targetKind, resetBase);
    if (isMobileSidebarOpen) closeNavigation(false);
    focusRoute();
  };

  const refreshBrands = (retries = 3, delayMs = 1500) => {
    ApiClient.getBrands()
      .then(data => {
        if (Array.isArray(data) && data.length > 0) {
          setBrands(sortBrands(data));
        } else if (retries > 0) {
          setTimeout(() => refreshBrands(retries - 1, delayMs * 1.5), delayMs);
        }
      })
      .catch(err => {
        console.error('Erro ao carregar marcas:', err);
        if (retries > 0) {
          setTimeout(() => refreshBrands(retries - 1, delayMs * 1.5), delayMs);
        }
      });
  };

  useEffect(() => {
    refreshBrands();
  }, []);

  return (
    <>
      <a className="skip-link" href="#main-content">
        Ir para o conteúdo
      </a>
      <div className="app-container">
        {isMobileSidebarOpen && (
          <button
            type="button"
            className="mobile-overlay"
            onClick={() => closeNavigation()}
            aria-label="Fechar navegação"
          />
        )}
        <aside
          ref={navigationRef}
          id="app-navigation"
          aria-label="Navegação principal"
          className={`sidebar ${isSidebarCollapsed ? 'collapsed' : ''} ${isMobileSidebarOpen ? 'mobile-open' : ''}`}
        >
          <button
            type="button"
            className="sidebar-toggle-btn hidden-on-mobile absolute-toggle-btn"
            onClick={() => setIsSidebarCollapsed(!isSidebarCollapsed)}
            aria-label={isSidebarCollapsed ? 'Expandir barra lateral' : 'Recolher barra lateral'}
          >
            {isSidebarCollapsed ? <ChevronRight size={14} aria-hidden="true" /> : <ChevronLeft size={14} aria-hidden="true" />}
          </button>
          <div className="sidebar-header">
            <img className="brand-logo" src="/brand/omnipulse-logo.svg" alt="OmniPulse" />
            <img className="brand-monogram" src="/brand/omnipulse-icon.svg" alt="OmniPulse" />
          </div>
          <nav className="sidebar-nav" aria-label="Áreas da aplicação">
            <SidebarItem
              icon={LayoutDashboard}
              label="Monitores"
              to={ROUTES.monitor}
              onNavigate={handleRouteNavigation}
            />
            <SidebarItem
              icon={Search}
              label="Comparativa"
              to={ROUTES.search}
              onNavigate={(event) => handleRouteNavigation(event, 'search', true)}
            />
            <SidebarItem
              icon={Radar}
              label="SKU"
              to={ROUTES.cross}
              onNavigate={(event) => handleRouteNavigation(event, 'cross', true)}
            />
            <SidebarItem
              icon={Layers}
              label="Categorias"
              to={ROUTES.category}
              onNavigate={handleRouteNavigation}
            />
            <SidebarItem
              icon={Images}
              label="Banners"
              to={ROUTES.banners}
              onNavigate={handleRouteNavigation}
            />
            <div className="sidebar-spacer" />
            <SidebarItem
              icon={Package}
              label="Marcas"
              to={ROUTES.settings}
              onNavigate={handleRouteNavigation}
            />
          </nav>
        </aside>

        <main
          ref={mainContentRef}
          id="main-content"
          className="main-content"
          tabIndex={-1}
          inert={isMobileSidebarOpen || undefined}
        >
          <header className="content-header">
            <div className="content-header-title">
              <button
                ref={navigationTriggerRef}
                type="button"
                className="mobile-header-toggle"
                onClick={openNavigation}
                aria-label="Abrir navegação"
                aria-expanded={isMobileSidebarOpen}
                aria-controls="app-navigation"
              >
                <Menu size={22} aria-hidden="true" />
              </button>
              <img className="mobile-header-brand" src="/brand/omnipulse-icon.svg" alt="" aria-hidden="true" />
              <div className="content-header-text">
                <h1>{route.title}</h1>
                {route.description && (
                  <p className="content-header-description">{route.description}</p>
                )}
              </div>
            </div>
            <SystemNotifications />
          </header>

          <AnimatePresence mode="wait">
            <motion.div
              key={location.pathname}
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={{ duration: 0.15, ease: 'easeOut' }}
            >
              <AppRoutes
                elements={{
                  monitor: <MonitoringPage brands={brands} />,
                  search: <SearchRoute brands={brands} />,
                  cross: <CrossRoute />,
                  category: <CategoryPage brands={brands} />,
                  banners: <BannersPage brands={brands} />,
                  settings: <SettingsPage brands={brands} onRefresh={refreshBrands} />,
                }}
                notFoundElement={<NotFoundRoute />}
              />
            </motion.div>
          </AnimatePresence>
        </main>
      </div>
    </>
  );
}

export default App;

// Re-exports for Vitest route harness compatibility
export { SearchPage } from './features/search/SearchPage';
export { CrossMarketplacePage } from './features/cross/CrossMarketplacePage';
export { SidebarItem } from './shared/layout/SidebarItem.tsx';
