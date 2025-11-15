'use client';

import Link from 'next/link';
import { type LucideIcon, CheckCircle2, Circle, Lock, PlugZap, ServerCog, Settings2, Wallet } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { cn } from '@/lib/utils';

interface InitialSetupOnboardingProps {
  hasAdminPassword: boolean;
  hasUpstreamProviders: boolean;
}

type CriticalStep = {
  id: string;
  title: string;
  description: string;
  helper: string;
  href: string;
  actionLabel: string;
  completed: boolean;
  icon: LucideIcon;
};

type FocusArea = {
  id: string;
  title: string;
  description: string;
  bullets: string[];
  href: string;
  actionLabel: string;
  icon: LucideIcon;
};

export function InitialSetupOnboarding({
  hasAdminPassword,
  hasUpstreamProviders,
}: InitialSetupOnboardingProps) {
  const criticalSteps: CriticalStep[] = [
    {
      id: 'admin-password',
      title: 'Secure the dashboard',
      description:
        'Create a unique admin password to mint login tokens and protect the control plane.',
      helper:
        'Navigate to Settings → Change Admin Password. The UI will prompt for the current value if one already exists.',
      href: '/settings',
      actionLabel: hasAdminPassword ? 'Review password policy' : 'Set password',
      completed: hasAdminPassword,
      icon: Lock,
    },
    {
      id: 'providers',
      title: 'Connect an upstream provider',
      description:
        'Add at least one upstream LLM provider so Routstr can relay requests on behalf of your users.',
      helper:
        'Open the Providers workspace to choose OpenAI, Anthropic, Groq, or point to your own inference endpoint.',
      href: '/providers',
      actionLabel: hasUpstreamProviders ? 'Review providers' : 'Add provider',
      completed: hasUpstreamProviders,
      icon: PlugZap,
    },
  ];

  const focusAreas: FocusArea[] = [
    {
      id: 'node-profile',
      title: 'Node identity & endpoints',
      description:
        'Give your node a friendly name and publish both clearnet and Tor URLs so clients know where to connect.',
      bullets: [
        'Fill in Name, Description, HTTP URL, and optional Onion URL under Basic Information.',
        'These fields populate the metadata served to wallets and the public API.',
      ],
      href: '/settings',
      actionLabel: 'Edit profile',
      icon: Settings2,
    },
    {
      id: 'cashu',
      title: 'Cashu mint configuration',
      description:
        'Your node escrows sats per request. Keep the wallet funded and list every mint you plan to settle with.',
      bullets: [
        'Add each mint URL and test a small deposit to verify proofs sync correctly.',
        'Monitor the Detailed Wallet widget to ensure owner balance stays positive.',
      ],
      href: '/settings',
      actionLabel: 'Manage mints',
      icon: Wallet,
    },
    {
      id: 'models',
      title: 'Model catalog & pricing',
      description:
        'Once a provider is connected, import models or define custom SKUs so downstream clients have predictable IDs.',
      bullets: [
        'Use the Providers page to sync remote catalogs or create per-model pricing in sats.',
        'Assign provider fees and availability so routing honors your margins.',
      ],
      href: '/providers',
      actionLabel: 'Curate catalog',
      icon: ServerCog,
    },
  ];

  const completedSteps = criticalSteps.filter((step) => step.completed).length;
  const progress = (completedSteps / criticalSteps.length) * 100;

  return (
    <Card className='border-primary/40 bg-primary/5 shadow-sm dark:bg-primary/10'>
      <CardHeader>
        <Badge variant='warning' className='w-fit text-xs uppercase tracking-wide'>
          Initial setup
        </Badge>
        <CardTitle className='text-xl'>Finish onboarding your Routstr node</CardTitle>
        <CardDescription>
          Lock down the admin dashboard, add an upstream provider, and review the remaining
          configuration so routing works end-to-end.
        </CardDescription>
      </CardHeader>
      <CardContent className='space-y-8'>
        <div>
          <div className='flex items-center justify-between text-sm text-muted-foreground'>
            <span>
              {completedSteps} of {criticalSteps.length} critical steps complete
            </span>
            <span className='font-semibold text-primary'>{Math.round(progress)}%</span>
          </div>
          <Progress value={progress} className='mt-2 h-2' />
        </div>

        <ol className='space-y-4'>
          {criticalSteps.map((step, index) => {
            const Icon = step.icon;
            const StatusIcon = step.completed ? CheckCircle2 : Circle;
            return (
              <li
                key={step.id}
                className='rounded-xl border bg-background/80 p-5 shadow-sm transition hover:border-primary/50'
              >
                <div className='flex flex-col gap-4 md:flex-row md:items-center'>
                  <div className='flex flex-1 items-start gap-4'>
                    <Badge
                      variant='secondary'
                      className='mt-0.5 flex size-8 items-center justify-center rounded-full text-base font-semibold'
                    >
                      {index + 1}
                    </Badge>
                    <div className='space-y-2'>
                      <div className='flex flex-wrap items-center gap-2'>
                        <Icon className='size-5 text-primary' />
                        <p className='text-base font-semibold'>{step.title}</p>
                      </div>
                      <p className='text-sm text-muted-foreground'>{step.description}</p>
                      <p className='text-xs text-muted-foreground'>{step.helper}</p>
                    </div>
                  </div>
                  <div className='flex w-full flex-col gap-2 sm:w-auto'>
                    <div
                      className={cn(
                        'flex items-center gap-2 rounded-full px-3 py-1 text-xs font-medium',
                        step.completed
                          ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10'
                          : 'bg-muted text-muted-foreground'
                      )}
                    >
                      <StatusIcon
                        className={cn(
                          'size-4',
                          step.completed ? 'text-emerald-600' : 'text-muted-foreground'
                        )}
                      />
                      {step.completed ? 'Completed' : 'Action required'}
                    </div>
                    <Button asChild size='sm' variant={step.completed ? 'outline' : 'default'}>
                      <Link href={step.href}>{step.actionLabel}</Link>
                    </Button>
                  </div>
                </div>
              </li>
            );
          })}
        </ol>

        <div className='grid gap-4 md:grid-cols-3'>
          {focusAreas.map((area) => {
            const Icon = area.icon;
            return (
              <div key={area.id} className='rounded-xl border bg-card/70 p-4'>
                <div className='flex items-center gap-2 text-sm font-semibold'>
                  <Icon className='size-4 text-primary' />
                  {area.title}
                </div>
                <p className='mt-2 text-sm text-muted-foreground'>{area.description}</p>
                <ul className='mt-3 space-y-1 text-xs text-muted-foreground'>
                  {area.bullets.map((bullet) => (
                    <li key={bullet} className='flex gap-2'>
                      <span className='text-primary'>•</span>
                      <span>{bullet}</span>
                    </li>
                  ))}
                </ul>
                <Button asChild size='sm' variant='secondary' className='mt-4 w-full'>
                  <Link href={area.href}>{area.actionLabel}</Link>
                </Button>
              </div>
            );
          })}
        </div>

        <div className='rounded-xl border bg-muted/40 p-4 text-sm leading-relaxed text-muted-foreground'>
          <p className='font-semibold text-foreground'>How the flow works</p>
          <p className='mt-2'>
            Routstr reserves the maximum cost for each request up front, relays it to the selected
            upstream provider, and then reconciles the actual token usage when the response returns.
            Keeping an admin password, provider credentials, and Cashu balances in sync ensures every
            client request can be authorized and settled automatically.
          </p>
          <p className='mt-2'>
            As soon as you set the password and register a provider this onboarding guide disappears
            automatically.
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
