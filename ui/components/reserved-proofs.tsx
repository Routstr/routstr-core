'use client';

import { useMemo } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, RefreshCw, ShieldCheck } from 'lucide-react';
import { toast } from 'sonner';
import { AdminService } from '@/lib/api/services/admin';
import type {
  ReservedProofGroup,
  ReservedProofReconcileResult,
} from '@/lib/api/services/admin';
import { getApiErrorMessage } from '@/lib/api/errors';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from '@/components/ui/empty';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { cn } from '@/lib/utils';
import type { DisplayUnit } from '@/lib/types/units';
import { formatFromMsat } from '@/lib/currency';
import { format } from 'date-fns';

interface ReservedProofsProps {
  refreshInterval?: number;
  displayUnit: DisplayUnit;
  usdPerSat: number | null;
}

const HINT_LABEL: Record<ReservedProofGroup['hint'], string> = {
  paid_at_mint: 'Paid at mint',
  releasable: 'Releasable',
  check_mint: 'Ask mint',
};

const HINT_VARIANT: Record<
  ReservedProofGroup['hint'],
  'default' | 'secondary' | 'outline'
> = {
  paid_at_mint: 'secondary',
  releasable: 'default',
  check_mint: 'outline',
};

const ACTION_LABEL: Record<ReservedProofReconcileResult['action'], string> = {
  released: 'released',
  settled_paid: 'settled as paid',
  left_reserved: 'still pending',
  checked: 'checked',
  skipped: 'skipped',
  error: 'failed',
};

const toMsat = (amount: number, unit: string | null) =>
  unit === 'sat' ? amount * 1000 : amount;

const shortMint = (mintUrl: string | null) =>
  mintUrl ? mintUrl.replace(/^https?:\/\//, '') : 'unknown mint';

const shortId = (id: string | null) =>
  id ? `${id.slice(0, 8)}…${id.slice(-4)}` : '—';

const formatTime = (value: number | string | null) => {
  if (!value) return '—';
  const date =
    typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : format(date, 'PP p');
};

export function ReservedProofs({
  refreshInterval = 60000,
  displayUnit,
  usdPerSat,
}: ReservedProofsProps) {
  const queryClient = useQueryClient();

  const { data, isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ['reserved-proofs'],
    queryFn: () => AdminService.getReservedProofs(),
    refetchInterval: refreshInterval,
  });

  const reconcile = useMutation({
    mutationFn: (key?: string) => AdminService.reconcileReservedProofs(key),
    onSuccess: (response) => {
      queryClient.setQueryData(['reserved-proofs'], response.inspection);
      queryClient.invalidateQueries({ queryKey: ['detailed-wallet-balance'] });
      const released = response.results.reduce(
        (sum, r) => sum + r.released_amount,
        0
      );
      const pruned = response.results.reduce(
        (sum, r) => sum + r.pruned_amount,
        0
      );
      const failed = response.results.filter((r) => r.action === 'error');
      const summary = `Released ${released}, pruned ${pruned} (in proof units)`;
      if (failed.length > 0) {
        toast.warning(`${summary}. ${failed.length} group(s) failed.`);
      } else {
        toast.success(summary);
      }
    },
    onError: (err) => {
      toast.error(getApiErrorMessage(err, 'Reconciliation failed'));
    },
  });

  const formatAmount = (amount: number, unit: string | null) =>
    formatFromMsat(toMsat(amount, unit), displayUnit, usdPerSat);

  const groups = useMemo(() => data?.groups ?? [], [data]);
  const recoverableMsat = useMemo(
    () =>
      groups
        .filter((g) => g.hint !== 'paid_at_mint')
        .reduce((sum, g) => sum + toMsat(g.proof_amount, g.unit), 0),
    [groups]
  );
  const reservedMsat = useMemo(
    () => groups.reduce((sum, g) => sum + toMsat(g.proof_amount, g.unit), 0),
    [groups]
  );

  const busy = isLoading || isFetching || reconcile.isPending;

  return (
    <Card>
      <CardHeader className='pb-4'>
        <div className='flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between'>
          <div className='space-y-1.5'>
            <CardTitle>Reserved proofs</CardTitle>
            <CardDescription className='max-w-2xl'>
              Ecash the balances above do not count: melts still waiting on the
              mint, and proofs parked after a spend check. Reconcile asks each
              mint how they ended and settles what it confirms.
            </CardDescription>
          </div>
          <div className='flex gap-2'>
            <Button
              variant='ghost'
              size='icon'
              onClick={() => refetch()}
              disabled={busy}
            >
              <RefreshCw
                className={cn('h-4 w-4', isFetching && 'animate-spin')}
              />
              <span className='sr-only'>Refresh reserved proofs</span>
            </Button>
            <Button
              onClick={() => reconcile.mutate(undefined)}
              disabled={busy || groups.length === 0}
            >
              <ShieldCheck className='mr-2 h-4 w-4' />
              Reconcile with mints
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className='space-y-4'>
        {isError && (
          <Alert variant='destructive'>
            <AlertCircle className='h-4 w-4' />
            <AlertDescription>
              {getApiErrorMessage(error, 'Could not load reserved proofs')}
            </AlertDescription>
          </Alert>
        )}

        {isLoading ? (
          <div className='space-y-2'>
            <Skeleton className='h-6 w-1/3' />
            <Skeleton className='h-24 w-full' />
          </div>
        ) : groups.length === 0 ? (
          <Empty>
            <EmptyHeader>
              <EmptyMedia variant='icon'>
                <ShieldCheck />
              </EmptyMedia>
              <EmptyTitle>Nothing reserved</EmptyTitle>
              <EmptyDescription>
                Every proof in the wallet counts toward the balances above.
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        ) : (
          <>
            <div className='grid gap-3 sm:grid-cols-2'>
              <div className='rounded-lg border p-3'>
                <p className='text-muted-foreground text-xs'>Reserved total</p>
                <p className='text-lg font-semibold'>
                  {formatFromMsat(reservedMsat, displayUnit, usdPerSat)}
                </p>
              </div>
              <div className='rounded-lg border p-3'>
                <p className='text-muted-foreground text-xs'>
                  Possibly recoverable
                </p>
                <p className='text-lg font-semibold'>
                  {formatFromMsat(recoverableMsat, displayUnit, usdPerSat)}
                </p>
                <p className='text-muted-foreground text-xs'>
                  Not yet confirmed paid by a mint. Reconcile to find out.
                </p>
              </div>
            </div>

            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Mint</TableHead>
                  <TableHead>Quote</TableHead>
                  <TableHead>Local state</TableHead>
                  <TableHead className='text-right'>Reserved</TableHead>
                  <TableHead className='text-right'>Quote amount</TableHead>
                  <TableHead>Since</TableHead>
                  <TableHead>Next step</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {groups.map((group) => (
                  <TableRow key={group.key}>
                    <TableCell className='font-mono text-xs'>
                      {shortMint(group.mint_url)}
                      {group.unit ? ` · ${group.unit}` : ''}
                    </TableCell>
                    <TableCell className='font-mono text-xs'>
                      {group.kind === 'melt'
                        ? shortId(group.quote_id)
                        : `no quote · ${group.proof_count} proofs`}
                    </TableCell>
                    <TableCell>{group.local_state ?? '—'}</TableCell>
                    <TableCell className='text-right'>
                      {formatAmount(group.proof_amount, group.unit)}
                    </TableCell>
                    <TableCell className='text-right'>
                      {group.quote_amount !== null
                        ? formatAmount(
                            group.quote_amount + (group.fee_reserve ?? 0),
                            group.unit
                          )
                        : '—'}
                    </TableCell>
                    <TableCell className='text-xs'>
                      {formatTime(
                        group.created_time ?? group.oldest_reserved_at
                      )}
                    </TableCell>
                    <TableCell>
                      <Badge variant={HINT_VARIANT[group.hint]}>
                        {HINT_LABEL[group.hint]}
                      </Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </>
        )}

        {reconcile.data && reconcile.data.results.length > 0 && (
          <div className='space-y-1 text-sm'>
            <p className='font-medium'>Last reconcile</p>
            <ul className='text-muted-foreground list-disc space-y-0.5 pl-5'>
              {reconcile.data.results.map((r) => (
                <li key={r.key}>
                  {shortMint(r.mint_url)}{' '}
                  {r.quote_id ? shortId(r.quote_id) : ''}:{' '}
                  {ACTION_LABEL[r.action]}
                  {r.mint_state ? ` (mint says ${r.mint_state})` : ''}
                  {r.released_amount > 0
                    ? `, released ${r.released_amount}`
                    : ''}
                  {r.pruned_amount > 0 ? `, pruned ${r.pruned_amount}` : ''}
                  {r.outstanding_amount > 0
                    ? `, ${r.outstanding_amount} unspent but reserved (possible outstanding token)`
                    : ''}
                  {r.error ? ` — ${r.error}` : ''}
                </li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
