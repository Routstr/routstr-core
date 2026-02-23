'use client';

import type { CSSProperties } from 'react';
import { useTheme } from 'next-themes';
import { Toaster as Sonner, type ToasterProps } from 'sonner';
import {
  CircleCheckIcon,
  InfoIcon,
  TriangleAlertIcon,
  OctagonXIcon,
  Loader2Icon,
} from 'lucide-react';

type SonnerTheme = NonNullable<ToasterProps['theme']>;

function normalizeTheme(theme: string | undefined): SonnerTheme {
  if (theme === 'light' || theme === 'dark' || theme === 'system') {
    return theme;
  }
  return 'system';
}

function resolveSystemTheme(
  theme: string | undefined
): Exclude<SonnerTheme, 'system'> {
  return theme === 'light' ? 'light' : 'dark';
}

const Toaster = ({ ...props }: ToasterProps) => {
  const { theme = 'system', resolvedTheme } = useTheme();
  const normalizedTheme = normalizeTheme(theme);
  const toasterTheme: ToasterProps['theme'] =
    normalizedTheme === 'system'
      ? resolveSystemTheme(resolvedTheme)
      : normalizedTheme;

  return (
    <Sonner
      theme={toasterTheme}
      className='toaster group'
      icons={{
        success: <CircleCheckIcon className='size-4' />,
        info: <InfoIcon className='size-4' />,
        warning: <TriangleAlertIcon className='size-4' />,
        error: <OctagonXIcon className='size-4' />,
        loading: <Loader2Icon className='size-4 animate-spin' />,
      }}
      style={
        {
          '--normal-bg': 'var(--popover)',
          '--normal-text': 'var(--popover-foreground)',
          '--normal-border': 'var(--border)',
          '--border-radius': 'var(--radius)',
        } as CSSProperties
      }
      toastOptions={{
        classNames: {
          toast: 'cn-toast',
        },
      }}
      {...props}
    />
  );
};

export { Toaster };
