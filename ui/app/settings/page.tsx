'use client';

import * as React from 'react';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ServerConfigSettings } from '@/components/settings/server-config-settings';
import { AdminSettings } from '@/components/settings/admin-settings';
import { AppPageShell } from '@/components/app-page-shell';
import { PageHeader } from '@/components/page-header';

export default function SettingsPage() {
  return (
    <AppPageShell contentClassName='mx-auto w-full max-w-5xl'>
      <div className='space-y-6'>
        <PageHeader
          title='Settings'
          description='Manage admin authentication, service metadata, and upstream forwarding.'
        />
        <Tabs defaultValue='admin' className='w-full'>
          <TabsList variant='line' className='mb-4 w-full'>
            <TabsTrigger value='admin'>Admin Settings</TabsTrigger>
            <TabsTrigger value='server'>Server Config</TabsTrigger>
          </TabsList>
          <TabsContent value='server'>
            <ServerConfigSettings />
          </TabsContent>
          <TabsContent value='admin'>
            <AdminSettings />
          </TabsContent>
        </Tabs>
      </div>
    </AppPageShell>
  );
}
