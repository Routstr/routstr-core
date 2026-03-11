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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Database,
  Pencil,
  Trash2,
  ChevronDown,
  ChevronUp,
  RotateCcw,
  AlertTriangle,
  KeyRound,
} from 'lucide-react';
import {
  AdminService,
  UpstreamProvider,
  UpdateUpstreamProvider,
} from '@/lib/api/services/admin';
import { RoutstrProviderService } from '@/lib/api/services/routstr-provider';
import { RoutstrCreateKeySection } from './RoutstrCreateKeySection';
import { toast } from 'sonner';

interface RoutstrProviderCardProps {
  provider: UpstreamProvider;
  expanded: boolean;
  onToggleExpand: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onUpdateKey?: () => void;
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
  const [isKeyDialogOpen, setIsKeyDialogOpen] = useState(false);

  const hasMint = !!provider.provider_settings?.topup_mint_url;
  const hasApiKey = !!provider.api_key;

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
        queryClient.invalidateQueries({ queryKey: ['balances'] });
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

  const updateKeyMutation = useMutation({
    mutationFn: (data: { id: number; data: UpdateUpstreamProvider }) =>
      AdminService.updateUpstreamProvider(data.id, data.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['upstream-providers'] });
      queryClient.invalidateQueries({
        queryKey: ['provider-balance', provider.id],
      });
      setIsKeyDialogOpen(false);
      toast.success('API key saved to provider');
    },
    onError: (error: Error) => {
      toast.error(`Failed to save key: ${error.message}`);
    },
  });

  const handleKeyCreated = async (newApiKey: string) => {
    if (hasApiKey) {
      try {
        const result = await RoutstrProviderService.refundBalance(provider.id);
        if (result.ok) {
          toast.success('Old key refunded', {
            description: result.message,
          });
        } else {
          toast.warning('Refund skipped', {
            description: result.message,
          });
        }
      } catch (error) {
        toast.warning(
          `Could not refund old key: ${error instanceof Error ? error.message : 'Unknown error'}`
        );
      }
    }
    updateKeyMutation.mutate({
      id: provider.id,
      data: { api_key: newApiKey },
    });
  };

  return (
    <>
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
                {!hasApiKey && (
                  <Badge
                    variant='outline'
                    className='flex items-center gap-1 border-red-200 bg-red-50 text-red-700 dark:border-red-900/50 dark:bg-red-900/20 dark:text-red-400'
                  >
                    <AlertTriangle className='h-3 w-3' />
                    No API Key
                  </Badge>
                )}
                {!hasMint && (
                  <Badge
                    variant='outline'
                    className='flex items-center gap-1 border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900/50 dark:bg-amber-900/20 dark:text-amber-400'
                    title='Top-up is not possible because no top-up mint is selected in the provider settings. Please edit settings to select a mint from your node configuration.'
                  >
                    <AlertTriangle className='h-3 w-3' />
                    Top-up Disabled: No Mint Selected
                  </Badge>
                )}
              </div>
              <CardDescription className='mt-1 break-all'>
                {provider.base_url}
              </CardDescription>
            </div>
            <div className='flex flex-wrap items-center gap-2'>
              {hasApiKey && (
                <div className='flex flex-col gap-1'>{balanceComponent}</div>
              )}

              <Button
                variant='outline'
                size='sm'
                onClick={() => setIsKeyDialogOpen(true)}
                className='w-full sm:w-auto'
                title={
                  hasApiKey
                    ? 'Create a new key on the upstream node'
                    : 'Create an API key on the upstream node'
                }
              >
                <KeyRound className='mr-1 h-4 w-4' />
                <span className='hidden sm:inline'>
                  {hasApiKey ? 'New Key' : 'Create Key'}
                </span>
              </Button>

              {hasApiKey && (
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
              )}

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

      <Dialog open={isKeyDialogOpen} onOpenChange={setIsKeyDialogOpen}>
        <DialogContent className='max-h-[90vh] overflow-y-auto sm:max-w-lg'>
          <DialogHeader>
            <DialogTitle>
              {hasApiKey ? 'Create New Key on Upstream Node' : 'Create API Key'}
            </DialogTitle>
            <DialogDescription>
              {hasApiKey
                ? 'Create a new API key on the upstream node. The remaining balance on the current key will be automatically refunded to your local wallet before it is replaced.'
                : 'Create an API key on the upstream Routstr node to enable balance, top-up, and refund operations.'}
            </DialogDescription>
          </DialogHeader>

          <RoutstrCreateKeySection
            baseUrl={provider.base_url}
            onApiKeyCreated={handleKeyCreated}
          />

          <DialogFooter>
            <Button
              variant='outline'
              onClick={() => setIsKeyDialogOpen(false)}
              className='w-full'
            >
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
