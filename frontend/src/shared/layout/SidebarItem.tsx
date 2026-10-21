import React from 'react';
import { NavLink } from 'react-router';

export const SidebarItem = ({ icon: Icon, label, to, onNavigate }: {
  icon: React.ComponentType<{ size?: number; 'aria-hidden'?: boolean | 'true' | 'false' }>;
  label: string;
  to: string;
  onNavigate: (event: React.MouseEvent<HTMLAnchorElement>) => void;
}) => (
  <NavLink
    to={to}
    onClick={(event) => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      onNavigate(event);
    }}
    className={({ isActive }) => `sidebar-item ${isActive ? 'active' : ''}`}
    aria-label={label}
    data-label={label}
  >
    <Icon size={20} aria-hidden="true" />
    <span className="sidebar-item-label">{label}</span>
  </NavLink>
);
