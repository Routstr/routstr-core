'use client';

import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

interface PageHeaderProps {
  title: string;
  description?: string;
  actions?: ReactNode;
  icon?: ReactNode;
  className?: string;
}

export function PageHeader({
  title,
  description,
  actions,
  icon,
  className,
}: PageHeaderProps) {
  return (
    <div
      className={cn(
        'flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between',
        className
      )}
    >
      <div className='min-w-0 space-y-1'>
        <div className='flex items-center gap-2'>
          {icon ? (
            <span className='text-muted-foreground shrink-0'>{icon}</span>
          ) : null}
          <h2 className='text-xl font-semibold tracking-tight sm:text-2xl'>
            {title}
          </h2>
        </div>
        {description ? (
          <p className='text-muted-foreground text-sm leading-relaxed'>
            {description}
          </p>
        ) : null}
      </div>
      {actions ? (
        <div className='flex w-full shrink-0 flex-wrap items-center gap-2 sm:w-auto sm:justify-end [&>*]:w-full sm:[&>*]:w-auto'>
          {actions}
        </div>
      ) : null}
    </div>
  );
}
