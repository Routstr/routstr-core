'use client';

import * as React from 'react';
import {
  ExternalLinkIcon,
  FileTextIcon,
  DatabaseIcon,
  LayoutDashboardIcon,
  ServerIcon,
  SettingsIcon,
  WalletIcon,
  ArrowRightLeftIcon,
} from 'lucide-react';
import Image from 'next/image';
import Link from 'next/link';

import { NavSecondary } from '@/components/nav-secondary';
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
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
      title: 'Balances',
      url: '/balances',
      icon: WalletIcon,
    },
    {
      title: 'Transactions',
      url: '/transactions',
      icon: ArrowRightLeftIcon,
    },
    {
      title: 'Logs',
      url: '/logs',
      icon: FileTextIcon,
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
  ],
  documents: [],
};

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  return (
    <Sidebar collapsible='offcanvas' {...props}>
      <SidebarHeader className='px-3 pt-4 pb-3'>
        <SidebarMenu>
          <SidebarMenuItem>
            <div className='flex items-center gap-2 px-2 py-1'>
              <Image
                src='/icon.ico'
                alt='Routstr Node'
                width={24}
                height={24}
                className='rounded'
              />
              <div className='space-y-0.5'>
                <p className='text-sm font-semibold tracking-tight'>
                  Routstr Node
                </p>
                <p className='text-muted-foreground text-[11px]'>
                  Admin dashboard
                </p>
              </div>
            </div>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent className='flex-1 overflow-y-auto px-2 pb-2'>
        <NavSecondary items={data.navSecondary} />
        <SidebarGroup className='mt-auto px-0 pt-2 pb-0'>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton asChild className='h-10 rounded-lg px-3'>
                  <Link
                    href='https://docs.routstr.com'
                    target='_blank'
                    rel='noreferrer'
                  >
                    <span>Docs</span>
                    <ExternalLinkIcon className='ml-auto h-3.5 w-3.5' />
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton asChild className='h-10 rounded-lg px-3'>
                  <Link
                    href='https://chat.routstr.com'
                    target='_blank'
                    rel='noreferrer'
                  >
                    <span>Chat App</span>
                    <ExternalLinkIcon className='ml-auto h-3.5 w-3.5' />
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      {/*
      <SidebarFooter>
        <NavUser />
      </SidebarFooter>
        */}
    </Sidebar>
  );
}
