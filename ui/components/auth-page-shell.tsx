'use client';

import type { ReactNode } from 'react';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';

interface AuthPageShellProps {
  title: string;
  description: string;
  children: ReactNode;
}

export function AuthPageShell({
  title,
  description,
  children,
}: AuthPageShellProps) {
  return (
    <div className='bg-background text-foreground flex min-h-dvh items-center justify-center px-4 py-10 sm:py-12'>
      <Card className='w-full max-w-md'>
        <CardHeader className='space-y-1 pb-4'>
          <CardTitle className='text-center text-2xl font-bold'>{title}</CardTitle>
          <CardDescription className='text-center'>{description}</CardDescription>
        </CardHeader>
        <CardContent>{children}</CardContent>
      </Card>
    </div>
  );
}
