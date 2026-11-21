import { AdminService } from '@/lib/api/services/admin';
import type { PPQAutoTopupState } from '@/lib/api/services/admin';
import type {
  AdminModel,
  ProviderModels,
  UpstreamProvider,
} from '@/lib/api/services/admin';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  ChevronDown,
  ChevronUp,
  Database,
  Pencil,
  Trash2,
  Key,
  RotateCcw,
  AlertTriangle,
  Unlock,
  Loader2,
  ShieldCheck,
} from 'lucide-react';
import { ProviderBalance } from '@/components/provider-balance';
import { ProviderModelsPanel } from '@/components/provider-models-panel';
import { ProviderCertificationDialog } from '@/components/provider-certification-dialog';
import { RoutstrCreateKeySection } from '@/components/providers/RoutstrCreateKeySection';
import { RefundTokenDialog } from '@/components/providers/RefundTokenDialog';
import { RoutstrProviderService } from '@/lib/api/services/routstr-provider';
import type { RoutstrRefundResult } from '@/lib/api/services/routstr-provider';
import { getErrorStatus } from '@/lib/api/client';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import { useCopyToClipboard } from '@/hooks/use-copy-to-clipboard';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';

interface ProviderCardProps {
  provider: UpstreamProvider;
  isExpanded: boolean;
  canShowBalance: boolean;
  platformUrl: string | null;
  isModelsLoading: boolean;
  providerModels: ProviderModels | null;
  isDeletingModel: boolean;
  onToggleExpansion: () => void;
  onEditProvider: () => void;
  onDeleteProvider: () => void;
  onBatchOverride: () => void;
  onAddModel: () => void;
  onEditModel: (model: AdminModel) => void;
  onDeleteModel: (modelId: string) => void;
  onOverrideModel: (model: AdminModel) => void;
  // Rejects when the new key could not be saved.
  onUpdateApiKey: (newKey: string) => Promise<void>;
  availableMints: string[];
}

interface RefundRequest {
  forKeyReplacement: boolean;
  // Set for the final refund that runs right before the old key is swapped
  // for this already-paid key.
  replacementKey?: string;
  keyDialogGeneration: number;
}

export function ProviderCard({
  provider,
  isExpanded,
  canShowBalance,
  platformUrl,
  isModelsLoading,
  providerModels,
  isDeletingModel,
  onToggleExpansion,
  onEditProvider,
  onDeleteProvider,
  onBatchOverride,
  onAddModel,
  onEditModel,
  onDeleteModel,
  onOverrideModel,
  onUpdateApiKey,
}: ProviderCardProps) {
  const queryClient = useQueryClient();
  const { copy } = useCopyToClipboard();
  const [isKeyModalOpen, setIsKeyModalOpen] = useState(false);
  const [isCertifyOpen, setIsCertifyOpen] = useState(false);
  const [isReleaseDialogOpen, setIsReleaseDialogOpen] = useState(false);
  // The current key is only replaced once its balance has left it: refunded,
  // empty, or handed to the operator as a token.
  const [isCurrentKeyRefunded, setIsCurrentKeyRefunded] = useState(false);
  const [keyRefundError, setKeyRefundError] = useState<string | null>(null);
  const [keyRefundNote, setKeyRefundNote] = useState<string | null>(null);
  const [unreceivedRefund, setUnreceivedRefund] = useState<{
    token: string;
    message: string;
    request: RefundRequest;
  } | null>(null);
  // A paid new key waiting for the old key's final refund. Auto or manual
  // top-ups can refill the old key after step 1, so it is refunded again
  // right before the swap. Kept across dialog reopens so the paid key is not
  // lost when the final refund fails.
  const [pendingNewKey, setPendingNewKey] = useState<string | null>(null);
  // Set when the old key was already emptied but saving the paid key failed;
  // the retry then only saves, it does not refund again.
  const [keySaveError, setKeySaveError] = useState<string | null>(null);
  const [isSavingKey, setIsSavingKey] = useState(false);
  // The admin confirmed replacing the key without refunding it (the upstream
  // permanently refuses, e.g. key not found or refund already swept).
  const [isRefundSkipped, setIsRefundSkipped] = useState(false);
  const [isSkipRefundConfirmOpen, setIsSkipRefundConfirmOpen] = useState(false);
  // Bumped on every New Key open so a step-1 refund started before a
  // close/reopen cannot mark the reopened dialog's key as refunded.
  const keyDialogGenerationRef = useRef(0);
  // The claim as the query cache held it when the admin opened the dialog.
  // The mutation sends this token rather than re-reading the query at submit
  // time: a background refetch after the dialog opened must not swap in a
  // state the admin never saw. The server rejects a stale token with a 409,
  // which is the authoritative guard.
  const [reviewedState, setReviewedState] = useState<PPQAutoTopupState | null>(
    null
  );
  const hasDetails = Boolean(provider.api_version) || isExpanded;
  const isRoutstr = provider.provider_type === 'routstr';
  const isPPQ = provider.provider_type === 'ppqai';

  const { data: ppqAutoTopupState, isError: ppqStateFetchFailed } = useQuery({
    queryKey: ['ppq-auto-topup-state', provider.id],
    queryFn: () => AdminService.getPPQAutoTopupState(provider.id),
    enabled: isPPQ,
    refetchInterval: 30000,
  });

  // A claim the server will not let us release: a worker is between reserving
  // proofs and hearing back from the mint, and sweeping it would let the next
  // cycle pay a second invoice.
  const isPPQPaymentInFlight =
    Boolean(ppqAutoTopupState?.active) &&
    ppqAutoTopupState?.releasable === false;

  const openReleaseDialog = () => {
    setReviewedState(ppqAutoTopupState ?? null);
    setIsReleaseDialogOpen(true);
  };

  const releasePPQMutation = useMutation({
    mutationFn: () =>
      AdminService.releasePPQAutoTopup(
        provider.id,
        reviewedState?.state_token ?? null
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ['ppq-auto-topup-state', provider.id],
      });
      setIsReleaseDialogOpen(false);
      setReviewedState(null);
      toast.success('PPQ auto top-up claim released');
    },
    onError: (error: Error) => {
      queryClient.invalidateQueries({
        queryKey: ['ppq-auto-topup-state', provider.id],
      });
      if (getErrorStatus(error) === 409) {
        // The claim changed since it was reviewed; the stale snapshot is
        // useless, so force a fresh review.
        setIsReleaseDialogOpen(false);
        setReviewedState(null);
        toast.error(
          'PPQ claim changed since it was reviewed; reopen to see the new state'
        );
        return;
      }
      // Transient failure: keep the dialog and the reviewed snapshot so the
      // admin can retry without re-navigating.
      toast.error(`Failed to release PPQ claim: ${error.message}`);
    },
  });

  // The paid key stays in pendingNewKey until the save succeeds, so a failed
  // save leaves it visible with a retry.
  const replaceCurrentKey = async (newKey: string) => {
    setPendingNewKey(newKey);
    setKeySaveError(null);
    setIsSavingKey(true);
    try {
      await onUpdateApiKey(newKey);
    } catch (error) {
      setKeySaveError(
        error instanceof Error ? error.message : 'Failed to save new key'
      );
      return;
    } finally {
      setIsSavingKey(false);
    }
    setPendingNewKey(null);
    setIsCurrentKeyRefunded(false);
    setIsKeyModalOpen(false);
  };

  // A step-1 result from before a dialog reopen is stale; a final refund
  // still applies because its paid key survives the reopen.
  const isStaleKeyRequest = (request: RefundRequest) =>
    !request.replacementKey &&
    request.keyDialogGeneration !== keyDialogGenerationRef.current;

  // The old key's balance has left it (refunded, empty, or saved as a token).
  const onCurrentKeyEmptied = (request: RefundRequest) => {
    if (request.replacementKey) {
      void replaceCurrentKey(request.replacementKey);
    } else if (!isStaleKeyRequest(request)) {
      setIsCurrentKeyRefunded(true);
    }
  };

  const refundMutation = useMutation<RoutstrRefundResult, Error, RefundRequest>(
    {
      mutationFn: () => RoutstrProviderService.refundBalance(provider.id),
      onMutate: (request) => {
        if (request.forKeyReplacement) setKeyRefundError(null);
        if (request.replacementKey) setKeySaveError(null);
      },
      onSuccess: (data, request) => {
        const { forKeyReplacement } = request;
        const updatesKeyDialog =
          forKeyReplacement && !isStaleKeyRequest(request);
        if (data.ok || data.token) {
          queryClient.invalidateQueries({
            queryKey: ['provider-balance', provider.id],
          });
          queryClient.invalidateQueries({ queryKey: ['balances'] });
        }
        if (data.ok) {
          toast.success(
            data.no_balance ? 'Nothing to refund' : 'Refund successful',
            { description: data.message }
          );
          if (forKeyReplacement) {
            if (updatesKeyDialog) {
              setKeyRefundNote(data.no_balance ? data.message : null);
            }
            onCurrentKeyEmptied(request);
          }
        } else if (data.token) {
          setUnreceivedRefund({
            token: data.token,
            message: data.message,
            request,
          });
        } else {
          if (updatesKeyDialog) setKeyRefundError(data.message);
          toast.error(
            forKeyReplacement
              ? 'Refund failed, key not replaced'
              : 'Refund failed',
            { description: data.message }
          );
        }
      },
      onError: (error, request) => {
        const { forKeyReplacement } = request;
        if (forKeyReplacement && !isStaleKeyRequest(request)) {
          setKeyRefundError(error.message);
        }
        toast.error(
          `Refund error${forKeyReplacement ? ', key not replaced' : ''}: ${error.message}`
        );
      },
    }
  );

  const refundForKeyReplacement = (replacementKey?: string) =>
    refundMutation.mutate({
      forKeyReplacement: true,
      replacementKey,
      keyDialogGeneration: keyDialogGenerationRef.current,
    });

  // Every path that installs a paid key runs the old key's final refund
  // first, unless the admin confirmed skipping it.
  const finishKeyReplacement = (newKey: string) =>
    isRefundSkipped
      ? void replaceCurrentKey(newKey)
      : refundForKeyReplacement(newKey);

  // Only one refund token can be shown at a time, so a refund must not start
  // while one is on screen or another refund may still return one.
  const isRefundBlocked = unreceivedRefund !== null || refundMutation.isPending;

  const skipRefund = () => {
    if (isRefundBlocked) return;
    setIsRefundSkipped(true);
    setKeyRefundError(null);
    if (pendingNewKey) {
      void replaceCurrentKey(pendingNewKey);
    } else {
      setIsCurrentKeyRefunded(true);
      setKeyRefundNote(
        'Refund skipped. Any balance left on the current key may be lost.'
      );
    }
  };

  const handleApiKeyCreated = (newApiKey: string) => {
    if (pendingNewKey !== null) {
      // A second paid key (e.g. an invoice from before a dialog reopen)
      // while the first is still pending. Keep the first flow and show this
      // key until the admin dismisses it; it is not stored anywhere else.
      toast.warning('Another paid API key was created', {
        description: `Save this key, it is not stored: ${newApiKey}`,
        duration: Infinity,
        closeButton: true,
        action: {
          label: 'Copy',
          onClick: (event) => {
            event.preventDefault();
            void copy(newApiKey);
          },
        },
      });
      return;
    }
    if (!provider.api_key) {
      void replaceCurrentKey(newApiKey);
      return;
    }
    setPendingNewKey(newApiKey);
    if (isRefundBlocked && !isRefundSkipped) {
      // Kept as pendingNewKey; the admin starts the final refund with the
      // retry button once the other refund has finished.
      toast.info('New key created', {
        description:
          'Finish the pending refund, then use "Retry refund and replace key".',
      });
      return;
    }
    finishKeyReplacement(newApiKey);
  };
  // A Lightning poll outlives the create-key section that started it (dialog
  // closed, or the section swapped for the pending-key view), so it calls
  // through this ref to see the current key state instead of its stale
  // closure.
  const handleApiKeyCreatedRef = useRef(handleApiKeyCreated);
  useEffect(() => {
    handleApiKeyCreatedRef.current = handleApiKeyCreated;
  });

  // Closing or reloading the tab now would lose money only this page holds.
  const holdsUnsavedFunds = unreceivedRefund !== null || pendingNewKey !== null;
  useEffect(() => {
    if (!holdsUnsavedFunds) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warnBeforeUnload);
    return () => window.removeEventListener('beforeunload', warnBeforeUnload);
  }, [holdsUnsavedFunds]);

  return (
    <Card>
      <CardHeader>
        <div className='flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between'>
          <div className='min-w-0 flex-1'>
            <div className='flex flex-col gap-2 sm:flex-row sm:items-center'>
              <CardTitle className='truncate'>
                {provider.provider_type}
              </CardTitle>
              <Badge
                variant={provider.enabled ? 'default' : 'secondary'}
                className='w-fit'
              >
                {provider.enabled ? 'Enabled' : 'Disabled'}
              </Badge>
              {ppqAutoTopupState?.active && (
                <Badge
                  variant='outline'
                  className={cn(
                    'w-fit gap-1',
                    isPPQPaymentInFlight
                      ? 'border-blue-500 text-blue-700 dark:text-blue-400'
                      : 'border-amber-500 text-amber-700 dark:text-amber-400'
                  )}
                >
                  {isPPQPaymentInFlight ? (
                    <Loader2 className='h-3 w-3 animate-spin' />
                  ) : (
                    <AlertTriangle className='h-3 w-3' />
                  )}
                  {isPPQPaymentInFlight
                    ? 'Paying invoice'
                    : 'Auto top-up needs review'}
                </Badge>
              )}
              {isPPQ && ppqStateFetchFailed && (
                <Badge
                  variant='outline'
                  className='border-destructive text-destructive w-fit gap-1'
                >
                  <AlertTriangle className='h-3 w-3' />
                  Top-up status unavailable
                </Badge>
              )}
            </div>
            <CardDescription className='break-all'>
              {provider.base_url}
            </CardDescription>
          </div>

          <div className='grid w-full grid-cols-2 gap-2 sm:flex sm:w-auto sm:flex-wrap sm:items-center sm:justify-end'>
            {canShowBalance && provider.api_key && (
              <div
                className={cn(
                  'col-span-2 sm:col-auto',
                  provider.provider_type === 'routstr' && 'col-span-1'
                )}
              >
                <ProviderBalance
                  providerId={provider.id}
                  platformUrl={platformUrl}
                  isRoutstr={provider.provider_type === 'routstr'}
                  nodeUrl={provider.base_url}
                />
              </div>
            )}

            {isRoutstr && (
              <Button
                variant='outline'
                size='sm'
                onClick={() => {
                  // Refund again on every open: the key may have been topped
                  // up since an earlier refund.
                  keyDialogGenerationRef.current += 1;
                  setIsCurrentKeyRefunded(false);
                  setKeyRefundError(null);
                  setKeyRefundNote(null);
                  setIsRefundSkipped(false);
                  setIsKeyModalOpen(true);
                }}
                className={cn(
                  'justify-center gap-1.5',
                  canShowBalance && provider.api_key
                    ? 'col-span-1'
                    : 'col-span-2 sm:col-auto'
                )}
              >
                <Key className='h-4 w-4' />
                <span>New Key</span>
              </Button>
            )}

            {isPPQ && ppqAutoTopupState?.active && !isPPQPaymentInFlight && (
              <Button
                variant='outline'
                size='sm'
                onClick={openReleaseDialog}
                className='justify-center gap-1.5 border-amber-500 text-amber-700 dark:text-amber-400'
                title='Release only after manually verifying the Lightning payment outcome'
              >
                <Unlock className='h-4 w-4' />
                <span>Release top-up</span>
              </Button>
            )}

            {isRoutstr && provider.api_key && (
              <Button
                variant='outline'
                size='sm'
                onClick={() =>
                  refundMutation.mutate({
                    forKeyReplacement: false,
                    keyDialogGeneration: keyDialogGenerationRef.current,
                  })
                }
                disabled={isRefundBlocked}
                className='justify-center gap-1.5 text-orange-600 hover:text-orange-700 dark:text-orange-400'
                title='Refund balance to local wallet'
              >
                <RotateCcw
                  className={cn(
                    'h-4 w-4',
                    refundMutation.isPending && 'animate-spin'
                  )}
                />
                <span>Refund</span>
              </Button>
            )}

            <Button
              variant='outline'
              size='sm'
              onClick={onToggleExpansion}
              className='col-span-2 justify-between sm:col-auto sm:justify-center'
            >
              <span className='inline-flex items-center gap-1.5'>
                <Database className='h-4 w-4' />
                <span>Models</span>
              </span>
              {isExpanded ? (
                <ChevronUp className='h-4 w-4' />
              ) : (
                <ChevronDown className='h-4 w-4' />
              )}
            </Button>

            <Button
              variant='outline'
              size='sm'
              onClick={() => setIsCertifyOpen(true)}
              className='justify-center gap-1.5'
              title='Probe the upstream and verify usage, pricing, caching and margin'
            >
              <ShieldCheck className='h-4 w-4' />
              <span>Certify</span>
            </Button>

            <Button
              variant='outline'
              size='sm'
              onClick={onEditProvider}
              className='justify-center gap-1.5'
            >
              <Pencil className='h-4 w-4' />
              <span>Edit</span>
            </Button>

            <Button
              variant='outline'
              size='sm'
              onClick={onDeleteProvider}
              className='text-destructive hover:text-destructive justify-center gap-1.5'
            >
              <Trash2 className='h-4 w-4' />
              <span>Delete</span>
            </Button>
          </div>
        </div>
      </CardHeader>

      <AlertDialog
        open={isReleaseDialogOpen}
        onOpenChange={setIsReleaseDialogOpen}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Release PPQ auto top-up?</AlertDialogTitle>
            <AlertDialogDescription>
              Only continue after checking PPQ and the Cashu mint and confirming
              the previous Lightning payment cannot later settle. Releasing an
              ambiguous payment can allow a duplicate top-up.
              {reviewedState?.invoice_id
                ? ` Invoice: ${reviewedState.invoice_id}`
                : ''}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                // Radix closes the dialog on action click by default; the
                // mutation handlers decide whether it closes (kept open on
                // transient errors so the admin can retry).
                e.preventDefault();
                releasePPQMutation.mutate();
              }}
              disabled={releasePPQMutation.isPending}
            >
              {releasePPQMutation.isPending
                ? 'Releasing...'
                : 'I verified it is safe to retry'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={isSkipRefundConfirmOpen}
        onOpenChange={setIsSkipRefundConfirmOpen}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Replace key without refund?</AlertDialogTitle>
            <AlertDialogDescription>
              The current key is replaced without refunding it. Any balance
              still on the current key may be lost. Only continue if the
              upstream permanently refuses the refund (for example, the key is
              unknown or its refund was already swept).
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={skipRefund}>
              Replace without refund
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <ProviderCertificationDialog
        provider={provider}
        open={isCertifyOpen}
        onOpenChange={setIsCertifyOpen}
      />

      <Dialog open={isKeyModalOpen} onOpenChange={setIsKeyModalOpen}>
        <DialogContent className='max-h-[90dvh] overflow-y-auto sm:max-w-[500px]'>
          <DialogHeader>
            <DialogTitle>
              {provider.api_key
                ? 'Create New Key on Upstream Node'
                : 'Create API Key'}
            </DialogTitle>
            <DialogDescription>
              {provider.api_key
                ? 'Create a new API key on the upstream node. The remaining balance on the current key is refunded to your local wallet first; the key is only replaced once that refund succeeds.'
                : 'Create an API key on the upstream Routstr node to enable balance, top-up, and refund operations.'}
            </DialogDescription>
          </DialogHeader>
          <div className='py-4'>
            {pendingNewKey && keySaveError ? (
              <div className='space-y-3 rounded-lg border p-4'>
                <p className='text-muted-foreground text-sm'>
                  New key created, but saving it failed. Save the new key below
                  and retry.
                </p>
                <Textarea
                  readOnly
                  value={pendingNewKey}
                  onFocus={(e) => e.target.select()}
                  className='font-mono text-xs'
                  rows={2}
                />
                <p className='text-destructive text-sm'>{keySaveError}</p>
                <Button
                  onClick={() => finishKeyReplacement(pendingNewKey)}
                  disabled={isRefundBlocked || isSavingKey}
                  className='w-full gap-1.5'
                >
                  <RotateCcw
                    className={cn(
                      'h-4 w-4',
                      (isSavingKey || refundMutation.isPending) &&
                        'animate-spin'
                    )}
                  />
                  Retry saving new key
                </Button>
              </div>
            ) : pendingNewKey ? (
              <div className='space-y-3 rounded-lg border p-4'>
                <p className='text-muted-foreground text-sm'>
                  New key created. The current key is refunded once more before
                  it is replaced, in case it was topped up meanwhile. If that
                  refund fails, the current key is kept; save the new key below
                  and retry.
                </p>
                <Textarea
                  readOnly
                  value={pendingNewKey}
                  onFocus={(e) => e.target.select()}
                  className='font-mono text-xs'
                  rows={2}
                />
                {keyRefundError && (
                  <p className='text-destructive text-sm'>{keyRefundError}</p>
                )}
                <Button
                  onClick={() => refundForKeyReplacement(pendingNewKey)}
                  disabled={isRefundBlocked || isSavingKey}
                  className='w-full gap-1.5'
                >
                  <RotateCcw
                    className={cn(
                      'h-4 w-4',
                      refundMutation.isPending && 'animate-spin'
                    )}
                  />
                  Retry refund and replace key
                </Button>
                {keyRefundError && (
                  <Button
                    variant='outline'
                    onClick={() => setIsSkipRefundConfirmOpen(true)}
                    disabled={isRefundBlocked || isSavingKey}
                    className='text-destructive w-full'
                  >
                    Replace without refund
                  </Button>
                )}
              </div>
            ) : provider.api_key && !isCurrentKeyRefunded ? (
              <div className='space-y-3 rounded-lg border p-4'>
                <p className='text-muted-foreground text-sm'>
                  Step 1: refund the current key. If the refund fails, the
                  current key is kept and you can retry.
                </p>
                {keyRefundError && (
                  <p className='text-destructive text-sm'>{keyRefundError}</p>
                )}
                <Button
                  onClick={() => refundForKeyReplacement()}
                  disabled={isRefundBlocked}
                  className='w-full gap-1.5'
                >
                  <RotateCcw
                    className={cn(
                      'h-4 w-4',
                      refundMutation.isPending && 'animate-spin'
                    )}
                  />
                  {keyRefundError ? 'Retry refund' : 'Refund current key'}
                </Button>
                {keyRefundError && (
                  <Button
                    variant='outline'
                    onClick={() => setIsSkipRefundConfirmOpen(true)}
                    disabled={isRefundBlocked}
                    className='text-destructive w-full'
                  >
                    Replace without refund
                  </Button>
                )}
              </div>
            ) : (
              <>
                {keyRefundNote && (
                  <p className='text-muted-foreground text-sm'>
                    {keyRefundNote}
                  </p>
                )}
                <RoutstrCreateKeySection
                  baseUrl={provider.base_url || ''}
                  onApiKeyCreated={(newApiKey) =>
                    handleApiKeyCreatedRef.current(newApiKey)
                  }
                />
              </>
            )}
          </div>
        </DialogContent>
      </Dialog>

      <RefundTokenDialog
        token={unreceivedRefund?.token ?? null}
        message={unreceivedRefund?.message}
        onSaved={() => {
          // The upstream already debited the key, so the token now holds its
          // balance and the key can be replaced.
          if (unreceivedRefund?.request.forKeyReplacement) {
            onCurrentKeyEmptied(unreceivedRefund.request);
          }
          setUnreceivedRefund(null);
        }}
      />

      {hasDetails ? (
        <CardContent>
          <div className='space-y-3'>
            {provider.api_version && (
              <div className='flex flex-col gap-1 text-sm sm:flex-row sm:items-center sm:justify-between'>
                <span className='text-muted-foreground'>API Version:</span>
                <span className='font-mono break-all'>
                  {provider.api_version}
                </span>
              </div>
            )}

            {isExpanded && (
              <div className='mt-3 border-t pt-3'>
                <ProviderModelsPanel
                  isLoading={isModelsLoading}
                  providerModels={providerModels}
                  onBatchOverride={onBatchOverride}
                  onAddModel={onAddModel}
                  onEditModel={onEditModel}
                  onDeleteModel={onDeleteModel}
                  onOverrideModel={onOverrideModel}
                  isDeletingModel={isDeletingModel}
                />
              </div>
            )}
          </div>
        </CardContent>
      ) : null}
    </Card>
  );
}
