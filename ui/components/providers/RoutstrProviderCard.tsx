'use client';

import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  Database,
  Pencil,
  Trash2,
  ChevronDown,
  ChevronUp,
  RotateCcw,
} from 'lucide-react';
import { UpstreamProvider } from '@/lib/api/services/admin';
import { RoutstrProviderService } from '@/lib/api/services/routstr-provider';
import { toast } from 'sonner';

interface RoutstrProviderCardProps {
  provider: UpstreamProvider;
  expanded: boolean;
  onToggleExpand: () => void;
  onEdit: () => void;
  onDelete: () => void;
  balanceComponent: React.ReactNode;
  children?: React.ReactNode;
}

export function RoutstrProviderCard({
  provider,
  expanded,
  onToggleExpand,
  onEdit,
  onDelete,
  balanceComponent,
  children,
}: RoutstrProviderCardProps) {
  const queryClient = useQueryClient();

  const refundMutation = useMutation({
    mutationFn: () => RoutstrProviderService.refundBalance(provider.id),
    onSuccess: (data) => {
      if (data.ok) {
        toast.success('Refund successful', {
          description: data.message,
        });
        queryClient.invalidateQueries({
          queryKey: ['provider-balance', provider.id],
        });
        queryClient.invalidateQueries({ queryKey: ['balances'] }); // Global wallet balance
      } else {
        toast.error('Refund failed', {
          description: data.message,
        });
      }
    },
    onError: (error: Error) => {
      toast.error(`Refund error: ${error.message}`);
    },
  });

  return (
    <Card>
      <CardHeader>
        <div className='flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between'>
          <div className='min-w-0 flex-1'>
            <div className='flex flex-col gap-2 sm:flex-row sm:items-center'>
              <CardTitle className='truncate text-lg'>Routstr Node</CardTitle>
              <Badge
                variant={provider.enabled ? 'default' : 'secondary'}
                className='w-fit sm:ml-2'
              >
                {provider.enabled ? 'Enabled' : 'Disabled'}
              </Badge>
              <Badge
                variant='outline'
                className='bg-blue-50 text-blue-700 dark:bg-blue-900/20 dark:text-blue-400'
              >
                NIP-91
              </Badge>
            </div>
            <CardDescription className='mt-1 break-all'>
              {provider.base_url}
            </CardDescription>
          </div>
          <div className='flex flex-wrap items-center gap-2'>
            <div className='flex flex-col gap-1'>{balanceComponent}</div>

            <Button
              variant='outline'
              size='sm'
              onClick={() => refundMutation.mutate()}
              disabled={refundMutation.isPending}
              className='text-orange-600 hover:text-orange-700 dark:text-orange-400'
              title='Refund balance to local wallet'
            >
              <RotateCcw
                className={`mr-1 h-4 w-4 ${refundMutation.isPending ? 'animate-spin' : ''}`}
              />
              <span className='hidden sm:inline'>Refund</span>
            </Button>

            <Button
              variant='outline'
              size='sm'
              onClick={onToggleExpand}
              className='w-full sm:w-auto'
            >
              <Database className='mr-1 h-4 w-4' />
              <span className='hidden sm:inline'>Models</span>
              {expanded ? (
                <ChevronUp className='ml-1 h-4 w-4' />
              ) : (
                <ChevronDown className='ml-1 h-4 w-4' />
              )}
            </Button>
            <Button
              variant='outline'
              size='sm'
              onClick={onEdit}
              className='w-full sm:w-auto'
            >
              <Pencil className='h-4 w-4' />
            </Button>
            <Button
              variant='outline'
              size='sm'
              onClick={onDelete}
              className='w-full sm:w-auto'
            >
              <Trash2 className='h-4 w-4' />
            </Button>
          </div>
        </div>
      </CardHeader>
      {children}
    </Card>
  );
}
