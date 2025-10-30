'use client';

import * as React from 'react';
import {
  DatabaseIcon,
  LayoutDashboardIcon,
  ServerIcon,
  SettingsIcon,
} from 'lucide-react';
import Image from 'next/image';

import { NavSecondary } from '@/components/nav-secondary';
import {
  Sidebar,
  SidebarContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuItem,
  SidebarMenuButton,
} from '@/components/ui/sidebar';

const data = {
  navMain: [
    {
      title: 'Dashboard',
      url: '/',
      icon: LayoutDashboardIcon,
    },
  ],
  navClouds: [],
  navSecondary: [
    {
      title: 'Dashboard',
      url: '/',
      icon: LayoutDashboardIcon,
    },
    {
      title: 'Models',
      url: '/model',
      icon: DatabaseIcon,
    },
    {
      title: 'Providers',
      url: '/providers',
      icon: ServerIcon,
    },
    {
      title: 'Settings',
      url: '/settings',
      icon: SettingsIcon,
    },
    // {
    //   title: 'Transactions',
    //   url: '/transactions',
    //   icon: ReceiptIcon,
    // },
    // {
    //   title: 'Credit',
    //   url: '/credits',
    //   icon: FolderIcon,
    // },
    // {
    //   title: 'Users',
    //   url: '/users',
    //   icon: UsersIcon,
    // },
    // {
    //   title: 'Organizations',
    //   url: '/organizations',
    //   icon: FolderIcon,
    // },
  ],
  documents: [],
};

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  return (
    <Sidebar collapsible='offcanvas' {...props}>
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              asChild
              className='data-[slot=sidebar-menu-button]:!p-1.5'
            >
              <div className='flex items-center gap-2'>
                <Image
                  src='/icon.ico'
                  alt='Routstr Node'
                  width={24}
                  height={24}
                  className='rounded'
                />
                <span className='text-base font-semibold'>Routstr Node</span>
              </div>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent className='flex-1 overflow-y-auto'>
        <NavSecondary items={data.navSecondary} className='mt-auto' />
      </SidebarContent>
      {/*
      <SidebarFooter>
        <NavUser />
      </SidebarFooter>
        */}
    </Sidebar>
  );
}
